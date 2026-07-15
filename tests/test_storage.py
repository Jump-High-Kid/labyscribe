"""storage.py 저장 안전 모듈 테스트 (pytest + tmp_path/monkeypatch · 네트워크 무의존).

순수층(is_safe_component·is_within·version_dir_name)은 결정적 · IO층은 실제 fs(tmp_path)
+ 결정적 모킹(os.rename raise·상수 fake). CK-1~16 저장안전 게이트 커버.
"""
import errno
import os
import stat
import subprocess
import sys

import pytest

import storage as ST


# ── 순수층: is_safe_component (CK-6·CK-15) ──────────────────────

@pytest.mark.parametrize("name,ok", [
    ("vidOK", True),
    ("en-orig", True),
    ("a.b", True),                 # 내부 점은 traversal 아님
    ("", False),
    (".", False),
    ("..", False),
    ("a/b", False),
    ("a\x00b", False),
    ("../evil", False),
])
def test_is_safe_component(name, ok):
    assert ST.is_safe_component(name) is ok


# ── 순수층: is_within (CK-6) ────────────────────────────────────

def test_is_within(tmp_path):
    root = str(tmp_path)
    assert ST.is_within(os.path.join(root, "a", "b"), root) is True
    assert ST.is_within(root, root) is True                     # 동일 = 포함
    assert ST.is_within("/other/place", root) is False


# ── 순수층: version_dir_name (CK-1) ─────────────────────────────

def test_version_dir_name():
    assert ST.version_dir_name("en", "abcdef0123456789") == "en-abcdef012345"
    assert ST.version_dir_name("ko-orig", "0" * 64) == "ko-orig-000000000000"


# ── run_capped: stdout 캡처·초과 None·stderr·returncode·timeout (CK-11) ──

def test_run_capped_captures_stdout():
    rc, out, err = ST.run_capped(
        [sys.executable, "-c", "print('hello')"],
        timeout=10, want_stdout=True, stdout_cap=1024, stderr_cap=1024)
    assert rc == 0 and out == b"hello\n" and err == ""


def test_run_capped_stdout_over_cap_returns_none():
    rc, out, _ = ST.run_capped(
        [sys.executable, "-c", "import sys; sys.stdout.write('x'*100)"],
        timeout=10, want_stdout=True, stdout_cap=10, stderr_cap=1024)
    assert rc == 0 and out is None                               # 초과 → 무제한축적 차단


def test_run_capped_captures_stderr_and_returncode():
    rc, out, err = ST.run_capped(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        timeout=10, want_stdout=False, stdout_cap=0, stderr_cap=1024)
    assert rc == 3 and out is None and "boom" in err


def test_run_capped_stderr_capped_keeps_tail():
    # 뒤쪽 cap — 오류·429 표식은 대개 끝에 있으므로 tail 보존이 분류 정확도 좌우
    rc, _out, err = ST.run_capped(
        [sys.executable, "-c", "import sys; sys.stderr.write('x'*995 + '429')"],
        timeout=10, want_stdout=False, stdout_cap=0, stderr_cap=10)
    assert len(err.encode("utf-8")) <= 10                        # cap 만큼만
    assert "429" in err                                          # tail(끝) 보존 확인


def test_run_capped_timeout_propagates():
    with pytest.raises(subprocess.TimeoutExpired):
        ST.run_capped([sys.executable, "-c", "import time; time.sleep(5)"],
                      timeout=0.3, want_stdout=False, stdout_cap=0, stderr_cap=1024)


# ── make_temp: 구조·0700 권한·유일성 (CK-1·CK-13) ─────────────────

def test_make_temp_structure_and_perms(tmp_path):
    root = str(tmp_path / "root")
    temp = ST.make_temp(root)
    assert temp.startswith(os.path.join(root, ".tmp"))          # .tmp 하위(EXDEV 회피)
    assert os.path.isdir(temp) and os.path.isdir(os.path.join(temp, "raw"))
    for p in (root, os.path.join(root, ".tmp"), temp, os.path.join(temp, "raw")):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o700


def test_make_temp_unique(tmp_path):
    root = str(tmp_path / "root")
    assert ST.make_temp(root) != ST.make_temp(root)             # 난수 토큰


# ── write_text_synced / copy_file_synced: 내용·0600·O_EXCL (CK-7) ──

def test_write_text_synced_roundtrip_and_perms(tmp_path):
    p = str(tmp_path / "t.txt")
    ST.write_text_synced(p, "한글 내용 content")
    assert open(p, encoding="utf-8").read() == "한글 내용 content"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_write_text_synced_excl_rejects_existing(tmp_path):
    p = str(tmp_path / "t.txt")
    ST.write_text_synced(p, "a")
    with pytest.raises(OSError):
        ST.write_text_synced(p, "b")                            # O_EXCL


def test_copy_file_synced(tmp_path):
    src = tmp_path / "s"
    src.write_text("payload", encoding="utf-8")
    dst = str(tmp_path / "d")
    ST.copy_file_synced(str(src), dst)
    assert open(dst, encoding="utf-8").read() == "payload"
    assert stat.S_IMODE(os.stat(dst).st_mode) == 0o600


def test_copy_file_synced_symlink_src_rejected(tmp_path):
    # CK-7 대칭: src 가 symlink 면 O_NOFOLLOW 로 거부(외부파일 복사 차단)
    real = tmp_path / "real.txt"
    real.write_text("leak", encoding="utf-8")
    link = tmp_path / "link"
    os.symlink(str(real), str(link))
    with pytest.raises(OSError):
        ST.copy_file_synced(str(link), str(tmp_path / "out"))


# ── fsync_dir: 실제 dir OK · 비-dir best-effort 무raise ──────────

def test_fsync_dir_happy(tmp_path):
    ST.fsync_dir(str(tmp_path))                                 # 무예외

def test_fsync_dir_on_file_no_raise(tmp_path):
    f = tmp_path / "f"
    f.write_text("x")
    ST.fsync_dir(str(f))                                        # O_DIRECTORY 실패 → 조용히 return


# ── atomic_publish: happy·rename경쟁·containment·rename크래시 (CK-2·3·4·6) ──

def _stage(tmp_path, vid="vidX", leaf="en-abc123def456"):
    root = str(tmp_path / "root")
    temp = ST.make_temp(root)
    ST.write_text_synced(os.path.join(temp, "transcript.txt"), "the transcript")
    ST.write_text_synced(os.path.join(temp, "meta.json"), '{"id":"vidX"}')
    with open(os.path.join(temp, "raw", "%s.en.vtt" % vid), "w") as f:
        f.write("WEBVTT\n")
    final = os.path.join(root, vid, leaf)
    return root, temp, final


def test_atomic_publish_happy(tmp_path):
    root, temp, final = _stage(tmp_path)
    assert ST.atomic_publish(temp, final, root) is True
    assert os.path.isfile(os.path.join(final, "transcript.txt"))
    assert os.path.isfile(os.path.join(final, "raw", "vidX.en.vtt"))
    assert not os.path.exists(temp)                             # 디렉토리째 이동
    assert stat.S_IMODE(os.stat(os.path.dirname(final)).st_mode) == 0o700


def test_atomic_publish_race_eexist_returns_false(tmp_path):
    root, temp, final = _stage(tmp_path)
    os.makedirs(final)
    with open(os.path.join(final, "prior.txt"), "w") as f:      # non-empty → ENOTEMPTY
        f.write("peer")
    assert ST.atomic_publish(temp, final, root) is False        # 경쟁패자 idempotent
    assert os.path.isdir(temp)                                  # 내 temp 보존


def test_atomic_publish_symlink_containment_rejects(tmp_path):
    root, temp, final = _stage(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    # <root>/<vid> 를 root 밖으로 향하는 symlink 로 선점
    os.symlink(str(outside), os.path.join(root, "vidX"))
    with pytest.raises(OSError):
        ST.atomic_publish(temp, final, root)
    assert os.listdir(str(outside)) == []                       # 외부 쓰기 0


def test_atomic_publish_rename_crash_no_final(tmp_path, monkeypatch):
    root, temp, final = _stage(tmp_path)

    def boom(src, dst):
        raise OSError(errno.EXDEV, "cross-device crash")        # EEXIST/ENOTEMPTY 아님
    monkeypatch.setattr(ST.os, "rename", boom)
    with pytest.raises(OSError):
        ST.atomic_publish(temp, final, root)
    assert not os.path.exists(final)                            # 부분세트 0


# ── find_cached: 히트·불완전 skip·다중버전 최신·containment (CK-5·7) ──

def _publish_set(root, vid, tag, hashhex, transcript="cached transcript"):
    temp = ST.make_temp(root)
    ST.write_text_synced(os.path.join(temp, "transcript.txt"), transcript)
    ST.write_text_synced(os.path.join(temp, "meta.json"), '{"id":"%s"}' % vid)
    final = os.path.join(root, vid, ST.version_dir_name(tag, hashhex))
    ST.atomic_publish(temp, final, root)
    return final


def test_find_cached_hit(tmp_path):
    root = str(tmp_path / "root")
    final = _publish_set(root, "vidX", "en", "a" * 12)
    assert ST.find_cached(root, "vidX", "en") == final


def test_find_cached_incomplete_skipped(tmp_path):
    root = str(tmp_path / "root")
    d = os.path.join(root, "vidX", "en-incomplete0")
    os.makedirs(os.path.join(d, "raw"))
    with open(os.path.join(d, "meta.json"), "w") as f:          # transcript.txt 없음
        f.write("{}")
    assert ST.find_cached(root, "vidX", "en") is None


def test_find_cached_newest_of_multiple(tmp_path):
    root = str(tmp_path / "root")
    old = _publish_set(root, "vidX", "en", "1" * 12)
    new = _publish_set(root, "vidX", "en", "2" * 12)
    os.utime(old, (1000, 1000))                                 # old 를 과거로
    assert ST.find_cached(root, "vidX", "en") == new


def test_find_cached_none_when_absent(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    assert ST.find_cached(root, "vidX", "en") is None


def test_find_cached_glob_metachar_literal(tmp_path):
    # 4단계 codex MED: video_id/tag 의 glob 메타문자([·*)가 다른 캐시 오매칭 하지 않음
    root = str(tmp_path / "root")
    other = _publish_set(root, "vidA", "en", "a" * 12)          # 무관한 실제 캐시
    # 리터럴 "vid[A]" 조회는 glob class "[A]"→"A" 로 오해석되면 안 됨(glob.escape)
    assert ST.find_cached(root, "vid[A]", "en") is None
    assert ST.find_cached(root, "vidA", "en") == other          # 정상 조회는 히트


# ── read_published: 정상·symlink 거부·bad json (CK-7) ────────────

def test_read_published_ok(tmp_path):
    root = str(tmp_path / "root")
    final = _publish_set(root, "vidX", "en", "a" * 12, transcript="body text")
    r = ST.read_published(final)
    assert r is not None and r[0] == "body text" and r[1]["id"] == "vidX"


def test_read_published_symlink_transcript_rejected(tmp_path):
    root = str(tmp_path / "root")
    final = _publish_set(root, "vidX", "en", "a" * 12)
    secret = tmp_path / "secret.txt"
    secret.write_text("leak", encoding="utf-8")
    tp = os.path.join(final, "transcript.txt")
    os.remove(tp)
    os.symlink(str(secret), tp)                                 # transcript.txt → 외부
    assert ST.read_published(final) is None                     # O_NOFOLLOW 거부


def test_read_published_bad_json(tmp_path):
    root = str(tmp_path / "root")
    d = os.path.join(root, "vidX", "en-x")
    os.makedirs(d)
    with open(os.path.join(d, "transcript.txt"), "w") as f:
        f.write("t")
    with open(os.path.join(d, "meta.json"), "w") as f:
        f.write("{not json")
    assert ST.read_published(d) is None


# ── disk_usage: 합산·symlink 미추종 (CK-8) ───────────────────────

def test_disk_usage_sums(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "sub"))
    with open(os.path.join(root, "a"), "w") as f:
        f.write("12345")
    with open(os.path.join(root, "sub", "b"), "w") as f:
        f.write("678")
    assert ST.disk_usage(root) == 8


# ── cleanup_stale_temp: age-based(오래된 것만·라이브 보존) (CK-12) ──

def test_cleanup_stale_temp_age_based(tmp_path):
    root = str(tmp_path / "root")
    stale = ST.make_temp(root)
    live = ST.make_temp(root)
    os.utime(stale, (1000, 1000))                               # 아주 오래됨
    ST.cleanup_stale_temp(root, max_age_sec=3600)
    assert not os.path.exists(stale)                            # 오래된 것 삭제
    assert os.path.exists(live)                                 # 라이브 보존


def test_cleanup_stale_temp_no_tmp_dir(tmp_path):
    ST.cleanup_stale_temp(str(tmp_path / "missing"), max_age_sec=10)   # 무예외
