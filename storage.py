"""labyscribe 저장 안전 모듈 (Phase 3) — 순수/IO 분리 (stdlib only · mcp/network import 0).

extract.py 의 2층 규율을 계승한다:
- **순수층**(fs 무의존·raise 금지·빈/False 반환): `is_safe_component`·`is_within`·
  `version_dir_name` — 결정적 단위테스트 대상.
- **IO층**(subprocess·파일시스템): `run_capped`·`make_temp`·`write_text_synced`·
  `copy_file_synced`·`fsync_dir`·`atomic_publish`·`find_cached`·`read_published`·
  `disk_usage`·`cleanup_stale_temp`.

저장 안전 = 불변 버전 디렉토리(`<root>/<video_id>/<tag>-<hash>/`) + temp→디렉토리째
atomic rename(부분 손상 창 0) + 0700 + O_NOFOLLOW + realpath containment + fsync.
fsync 수준 = os.fsync(크래시 일관성). F_FULLFSYNC(전원손실 완전내구)=내구성 belt·비례적 잔여로 보류(미구현·타임아웃/총량캡 백스톱 有).
"""
from __future__ import annotations

import errno
import glob
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from typing import Optional, Tuple


# ── 순수층 (fs 무의존 · raise 금지 · 빈/False 반환) ─────────────────

def is_safe_component(name: str) -> bool:
    """단일 경로 성분의 traversal 필수차단 검증(Phase 4 allowlist 아님).

    빈·"."·".."·경로구분자("/"·os.sep)·"\\x00" 포함 → False. raise 안 함.
    """
    if not name or name in (".", ".."):
        return False
    if "\x00" in name:
        return False
    if "/" in name or os.sep in name:
        return False
    return True


def is_within(child: str, parent: str) -> bool:
    """이미 realpath 처리된 절대경로의 포함관계(child ⊂ parent 또는 동일).

    `os.path.commonpath` 비교. 서로 다른 드라이브 등 ValueError → False.
    """
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def version_dir_name(tag: str, content_hash_hex: str) -> str:
    """불변 버전 디렉토리 리프명 SSOT — `<tag>-<hash[:12]>`."""
    return "%s-%s" % (tag, content_hash_hex[:12])


# ── IO층 (subprocess · 파일시스템) ─────────────────────────────────

def run_capped(argv, *, timeout, want_stdout, stdout_cap, stderr_cap
               ) -> Tuple[int, Optional[bytes], str]:
    """subprocess 실행 + stdout/stderr 를 tempfile 로 리다이렉트(파이프 데드락 원천차단).

    반환 (returncode, stdout_bytes|None, stderr_text).
    - want_stdout=False → stdout=DEVNULL(위험원 제거)·반환 stdout None.
    - want_stdout=True → stat-before-read: 크기 ≤ stdout_cap 이면 전량 반환, 초과 시 None.
    - stderr 는 tempfile 에서 stderr_cap 바이트만 읽어 텍스트로(메모리 미축적).
    - TimeoutExpired 는 그대로 전파(호출자가 재시도/승격).
    """
    stderr_f = tempfile.TemporaryFile()
    stdout_f = tempfile.TemporaryFile() if want_stdout else None
    try:
        proc = subprocess.run(argv, stdout=stdout_f or subprocess.DEVNULL,
                              stderr=stderr_f, timeout=timeout)
        # stderr 는 **뒤쪽 stderr_cap 바이트** — 오류·429 표식은 대개 끝에 있어 분류 정확도↑.
        err_size = stderr_f.seek(0, os.SEEK_END)
        stderr_f.seek(max(0, err_size - stderr_cap))
        stderr_text = stderr_f.read().decode("utf-8", "replace")
        stdout_bytes = None
        if stdout_f is not None:            # want_stdout 참 ⟺ 파일 존재 — mypy 파일 타입 내로잉
            size = os.fstat(stdout_f.fileno()).st_size
            if size <= stdout_cap:
                stdout_f.seek(0)
                stdout_bytes = stdout_f.read()
            # size > stdout_cap → None(무제한 메모리 축적 차단)
        return proc.returncode, stdout_bytes, stderr_text
    finally:
        stderr_f.close()
        if stdout_f is not None:
            stdout_f.close()


def make_temp(root: str, *, chmod_root: bool = True) -> str:
    """`<root>/.tmp/<token_hex(8)>/`(0700) + raw/(0700) 생성. 절대경로 반환.

    root·.tmp 도 0700 보장(makedirs mode 는 umask 종속이라 명시 chmod). temp 는
    `<root>/.tmp/` 하위 = root 와 동일 fs → atomic rename 시 cross-fs(EXDEV) 회피.

    chmod_root=False → root 자체는 chmod 하지 않음(사용자가 고른 외부 저장폴더 등
    앱 비소유 경로의 권한을 강제 축소하지 않기 위함 · webapp 저장경로 전용).
    """
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    if chmod_root:
        os.chmod(root, 0o700)
    tmp_parent = os.path.join(root, ".tmp")
    os.makedirs(tmp_parent, exist_ok=True)
    os.chmod(tmp_parent, 0o700)
    temp = os.path.join(tmp_parent, secrets.token_hex(8))
    os.makedirs(temp, mode=0o700)          # 신규 난수 dir
    os.chmod(temp, 0o700)
    os.makedirs(os.path.join(temp, "raw"), mode=0o700)
    os.chmod(os.path.join(temp, "raw"), 0o700)
    return temp


def write_text_synced(path: str, text: str) -> None:
    """O_EXCL|O_NOFOLLOW 로 새 파일(0600) 생성 → write → flush → os.fsync."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
    finally:
        os.close(fd)


def copy_file_synced(src: str, dst: str) -> None:
    """src → dst 복사. **양쪽 O_NOFOLLOW**(대칭 방어·CK-7) · dst 는 O_CREAT|O_EXCL(0600) + fsync."""
    src_fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:                                   # dst open 실패해도 src_fd 누수 0(중첩 try)
        dst_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(src_fd, "rb", closefd=False) as sf, \
                    os.fdopen(dst_fd, "wb", closefd=False) as df:
                shutil.copyfileobj(sf, df)
                df.flush()
                os.fsync(df.fileno())
        finally:
            os.close(dst_fd)
    finally:
        os.close(src_fd)


def fsync_dir(path: str) -> None:
    """디렉토리 엔트리 fsync — best-effort(일부 fs EINVAL). O_DIRECTORY|O_NOFOLLOW."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass                                # best-effort(fs 미지원 EINVAL)
    finally:
        os.close(fd)


def atomic_publish(temp: str, final: str, root: str, *, chmod_parent: bool = True) -> bool:
    """temp 디렉토리를 final 로 원자 발행. True=발행 · False=이미존재(경쟁패자 idempotent).

    순서(load-bearing · codex CRITICAL 반영):
    ① containment 먼저(생성/chmod 전) — 기존 성분 realpath ⊂ root 검증(외부 symlink 차단)
    ② parent 생성 → 재검증(TOCTOU belt) → chmod 0700(chmod_parent=True 일 때만)
    ③ 파일데이터는 write 시점 fsync 완료 → dir 엔트리 fsync
    ④ os.rename(원자 스왑) — EEXIST/ENOTEMPTY 는 경쟁패자(False), ENOTDIR+final 존재는 동명
       파일 충돌(False → 접미 재시도), 그 외 전파(EXDEV/perm·상위성분 TOCTOU 는 오인 없이 raise).
    ⑤ parent + root fsync(신규 video_id 엔트리는 root 에)
    chmod_parent=False → parent 를 chmod 하지 않음(사용자 지정 외부 저장폴더 권한 보존).
    containment/symlink/EXDEV → OSError(호출자가 OUTPUT_WRITE_FAILED 로).
    """
    root_real = os.path.realpath(root)
    parent = os.path.dirname(final)                 # <root>/<video_id>/

    # ① 존재하는 최심 조상 realpath containment (생성 전)
    anc = parent
    while not os.path.exists(anc):
        anc = os.path.dirname(anc)
    if not is_within(os.path.realpath(anc), root_real):
        raise OSError("containment 위반: %r 이 %r 밖" % (anc, root))

    # ② parent 생성 + 재검증 + chmod
    os.makedirs(parent, exist_ok=True)
    if not is_within(os.path.realpath(parent), root_real):
        raise OSError("containment 위반(생성 후): %r 이 %r 밖" % (parent, root))
    if chmod_parent:
        os.chmod(parent, 0o700)

    # ③ dir 엔트리 fsync (파일데이터는 이미 fsync 완료)
    raw = os.path.join(temp, "raw")
    if os.path.isdir(raw):
        fsync_dir(raw)
    fsync_dir(temp)

    # ④ 원자 rename
    try:
        os.rename(temp, final)
    except OSError as e:
        if e.errno in (errno.EEXIST, errno.ENOTEMPTY):
            return False                            # 경쟁패자(이미 발행) → 호출자 재조회
        if e.errno == errno.ENOTDIR and os.path.exists(final):
            return False                            # final 자리 동명 '파일' 확정 → 접미 재시도
        raise                                       # 상위성분 TOCTOU·EXDEV/perm → OUTPUT_WRITE_FAILED

    # ⑤ parent + root fsync
    fsync_dir(parent)
    fsync_dir(root)
    return True


def find_cached(root: str, video_id: str, tag: str) -> Optional[str]:
    """glob `<root>/<video_id>/<tag>-*/` 중 완결세트 + containment 통과한 최신 mtime 디렉토리.

    완결 = meta.json·transcript.txt 존재 & size>0. 후보 realpath 가 root 밖이면 skip
    (codex MED·변조 symlink 차단). 없으면 None(호출자 miss 폴백).
    """
    root_real = os.path.realpath(root)
    # glob.escape: video_id·tag 의 메타문자(*?[])가 다른 캐시 오매칭 하지 않게(codex MED).
    pattern = os.path.join(root, glob.escape(video_id), "%s-*" % glob.escape(tag))
    candidates = []
    for d in glob.glob(pattern):
        if not os.path.isdir(d):
            continue
        if not is_within(os.path.realpath(d), root_real):
            continue                                # 이탈 후보 skip
        try:
            if (os.path.getsize(os.path.join(d, "meta.json")) <= 0 or
                    os.path.getsize(os.path.join(d, "transcript.txt")) <= 0):
                continue
        except OSError:
            continue                                # 불완전(파일 부재 등) skip
        candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda d: os.stat(d).st_mtime)


def read_published(directory: str) -> Optional[Tuple[str, dict]]:
    """완결 저장본 읽기 → (transcript, meta) 또는 None.

    transcript.txt·meta.json 을 O_NOFOLLOW 로 열어 읽는다(변조 symlink 외부파일 반환
    차단·codex MED). 어떤 오류(OSError/JSON 파싱)든 None(호출자 miss 폴백).
    """
    try:
        transcript = _read_file_nofollow(os.path.join(directory, "transcript.txt"))
        meta = json.loads(_read_file_nofollow(os.path.join(directory, "meta.json")))
        return transcript, meta
    except (OSError, ValueError):
        return None


def _read_file_nofollow(path: str) -> str:
    """O_NOFOLLOW 로 파일 열어 UTF-8 텍스트 반환(마지막 성분 symlink 거부)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read().decode("utf-8", "replace")
    finally:
        os.close(fd)


def disk_usage(root: str) -> int:
    """root 하위 파일 크기 합(bytes). symlink 미추종(os.walk followlinks=False·lstat)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return total


def cleanup_stale_temp(root: str, max_age_sec: float) -> None:
    """`<root>/.tmp/*` 중 mtime 이 max_age_sec 보다 오래된 것만 rmtree(ignore_errors).

    age-based(codex HIGH) — 라이브 temp(최근 mtime) 보존 → 동시 인스턴스 진행중 temp
    오삭제 방지. 서버 시작 시 호출.
    """
    tmp_parent = os.path.join(root, ".tmp")
    if not os.path.isdir(tmp_parent):
        return
    cutoff = time.time() - max_age_sec
    for name in os.listdir(tmp_parent):
        path = os.path.join(tmp_parent, name)
        try:
            if os.stat(path).st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            continue


# ── v2 저장(Markdown 파트) — v1 계약 보존·find_cached 무변경 ─────────

def stage_v2_files(temp: str, part_mds, transcript_md: str) -> None:
    """신규추출 temp 에 `parts/part-NN.md` + `transcript.md` 기록(O_EXCL|O_NOFOLLOW+fsync).

    이후 atomic_publish 가 디렉토리째 단일 커밋(AC-13) — 여기선 staging 만.
    """
    parts_dir = os.path.join(temp, "parts")
    os.makedirs(parts_dir, mode=0o700)
    os.chmod(parts_dir, 0o700)
    for i, md in enumerate(part_mds, 1):
        write_text_synced(os.path.join(parts_dir, "part-%02d.md" % i), md)
    write_text_synced(os.path.join(temp, "transcript.md"), transcript_md)
    fsync_dir(parts_dir)


def read_v2_parts(directory: str, meta: dict) -> Optional[tuple]:
    """완결 v2 세트(meta.parts 필드 + `parts/*.md` 파일) → part 레코드 튜플, 미완결 None.

    완결 신호 = meta 의 `parts` 필드 존재 AND 각 파일 존재(완료 마커·AC-14). 파일 부재·
    필드 손상 = None(호출자가 증분 재생성). O_NOFOLLOW 로 읽음(변조 symlink 차단).
    """
    part_list = meta.get("parts")
    if not part_list:
        return None
    md_name = meta.get("transcript_md")          # 완결 = parts 필드 + transcript.md 파일 존재
    if md_name:
        try:
            os.close(os.open(os.path.join(directory, md_name),
                             os.O_RDONLY | os.O_NOFOLLOW))
        except OSError:
            return None                          # transcript.md 부재(증분 커밋 중) → 미완결
    records = []
    for entry in part_list:
        try:
            pn = int(entry["part_no"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            md = _read_file_nofollow(os.path.join(directory, "parts", "part-%02d.md" % pn))
        except OSError:
            return None                             # 미완결(파일 부재)
        records.append({
            "part_no": pn,
            "chapter_no": entry.get("chapter_no"),
            "title": entry.get("title"),
            "markdown": md,
            "bytes": len(md.encode("utf-8")),
        })
    return tuple(records)


def add_v2_artifacts(published_dir: str, part_mds, transcript_md: str,
                     meta_merged: dict, root: str) -> None:
    """v1 캐시 증분(AC-12/14) — 완결 디렉토리에 `.md` 세트 원자 추가.

    `raw/`·`transcript.txt` 무접촉. meta 는 chapters/parts 만 병합된 `meta_merged` 로
    **원자 replace = 완결 커밋 포인트**(마지막). 중간종료 → meta 에 parts 없어 미완결
    (read_v2_parts None → 재생성·멱등). 실패 시 staging 만 정리(기존 디렉터리 무손상).
    """
    root_real = os.path.realpath(root)
    if not is_within(os.path.realpath(published_dir), root_real):
        raise OSError("containment 위반: %r 이 %r 밖" % (published_dir, root))

    staging = os.path.join(published_dir, ".v2tmp-" + secrets.token_hex(8))
    os.makedirs(staging, mode=0o700)
    os.chmod(staging, 0o700)
    try:
        st_parts = os.path.join(staging, "parts")
        os.makedirs(st_parts, mode=0o700)
        os.chmod(st_parts, 0o700)
        for i, md in enumerate(part_mds, 1):
            write_text_synced(os.path.join(st_parts, "part-%02d.md" % i), md)
        st_md = os.path.join(staging, "transcript.md")
        st_meta = os.path.join(staging, "meta.json")
        write_text_synced(st_md, transcript_md)
        write_text_synced(st_meta, json.dumps(meta_merged, ensure_ascii=False, indent=2))
        fsync_dir(st_parts)
        fsync_dir(staging)

        # 커밋 순서: transcript.md → parts/ 스왑 → (마지막) meta.json replace.
        # os.replace = 기존 파일 원자 대체(Windows 재실행 포함 · os.rename 은 Win 에서 대상
        # 존재 시 실패). parts/ 디렉토리 스왑은 rmtree→rename(짧은 부재창) — 단일 사용자 로컬
        # 위협모델상 수용, 완결 마커=meta.parts 로 미완결 세대는 read_v2_parts None(재생성).
        os.replace(st_md, os.path.join(published_dir, "transcript.md"))
        dst_parts = os.path.join(published_dir, "parts")
        if os.path.isdir(dst_parts):
            shutil.rmtree(dst_parts)
        os.rename(st_parts, dst_parts)
        os.replace(st_meta, os.path.join(published_dir, "meta.json"))  # 원자 replace = 완결
        fsync_dir(published_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
