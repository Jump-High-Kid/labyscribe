#!/usr/bin/env python3
"""labyscribe extraction core — deterministic (no LLM). python3 stdlib only.

yt-dlp track selection -> native .vtt capture -> transcript
(parse_vtt: rolling dedup + tag strip + 10-min markers) into <out>/. A .json3
sample is captured to raw/ for comparison only. No post-conversion tool required.

Reference: README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import storage
from handles import content_hash

# 재시도 가능한 일시 오류: rate-limit·네트워크
_RETRYABLE = re.compile(r"429|Too Many Requests|Temporary failure|timed out|Connection",
                        re.IGNORECASE)

# 종료 코드 체계
EXIT_OK = 0               # transcript 산출 완료
EXIT_NO_SUBTITLE = 2      # 자막 트랙 없음 / 다운로드 파일 없음
EXIT_DOWNLOAD_FAILED = 3  # 429/네트워크 재시도 소진
EXIT_UNAVAILABLE = 4      # 비공개/삭제/지역/연령 — 정보 수집 비재시도 실패
EXIT_BAD_INPUT = 5        # validate_url 실패
EXIT_EMPTY_TRANSCRIPT = 6 # 자막 트랙은 받았으나 정제 결과 무효(빈/과소)
EXIT_STORAGE_LIMIT = 7    # 저장 디스크 총량 HARD 상한 초과(신규추출 거부·기존본 삭제 0)
EXIT_SUBTITLE_TOO_LARGE = 8  # 다운로드 자막 파일이 하드캡 초과(읽기 전 삭제·메모리 백스톱)

# 다운로드 subprocess 타임아웃(초) — 무한 블로킹 차단(D-K·CK-37). 잠정값·실측 Phase 6.
DOWNLOAD_TIMEOUT_SEC = 300

# 정책 상한(잠정 고정 상수·YAGNI·실측 Phase 6). server MAX_SUBTITLE_BYTES(transcript 후처리)
# 와 구분 — 이건 raw 자막 파일 자체의 stat 상한.
SUBTITLE_FILE_MAX_BYTES = 32 * 1024 * 1024   # raw 자막 파일 하드캡(다운로드 후 stat·32MB)
DISK_SOFT_BYTES = 2 * 1024 * 1024 * 1024     # 저장 루트 총량 경고선(2GB)
DISK_HARD_BYTES = 5 * 1024 * 1024 * 1024     # 저장 루트 총량 거부선(5GB·신규추출 차단)

# run_ytdlp_json 의 -J stdout 캡(초과 시 정보 과대로 중단·메모리 축적 차단). 잠정·Phase 6.
_INFO_STDOUT_CAP_BYTES = 64 * 1024 * 1024
_STDERR_CAP_BYTES = 8 * 1024


@dataclass(frozen=True)
class ExtractResult:
    """run_extract 반환 계약 — exit_code 가 유일 판별자(main·server 매핑 SSOT).

    frozen = immutability. main 은 `print(message); return exit_code` 어댑터,
    server 는 exit_code 로 성공/에러 분기 후 구조화 스키마로 매핑.
    """
    exit_code: int
    message: str
    meta: dict                    # 성공 시 전체 meta·실패 시 부분(또는 {})
    transcript: Optional[str]     # 성공 시 정제 transcript·실패 시 None


# ── 순수함수 (단위 테스트 대상) ────────────────────────────────

ALLOWED_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "youtu.be",
}


def validate_url(url):
    """https + 유튜브 도메인만 허용. 위반 시 ValueError (SSRF 차단)."""
    p = urlparse(url)
    if p.scheme != "https":
        raise ValueError("https 스킴만 허용: %r" % url)
    if p.hostname not in ALLOWED_HOSTS:
        raise ValueError("유튜브 도메인만 허용(SSRF 차단): host=%r" % p.hostname)
    return url


def _norm(tag):
    """BCP-47 기본언어: 'en-US'→'en', 'en-orig'→'en'."""
    return tag.split("-")[0]


def _match_lang(track_dict, lang):
    """정확일치 우선, 없으면 기본언어 prefix 폴백. 매칭 태그 또는 None."""
    if lang in track_dict:
        return lang
    for tag in track_dict:
        if _norm(tag) == _norm(lang):   # 요청언어도 정규화(en-US → en 트랙 매칭)
            return tag
    return None


def detect_orig_lang(info):
    """영상 원본 언어 감지: info.language → -orig 접미사 → 첫 수동자막. 없으면 None."""
    lang = info.get("language")
    if lang:
        return _norm(lang)
    autos = info.get("automatic_captions") or {}
    for tag in autos:
        if tag.endswith("-orig"):
            return _norm(tag)
    subs = info.get("subtitles") or {}
    if subs:
        return _norm(next(iter(subs)))
    return None


def _orig_auto_tag(autos, orig):
    """원어 자동자막 태그: 기본 태그 > -orig > 같은 기본언어 첫 태그."""
    if orig in autos:
        return orig
    if orig + "-orig" in autos:
        return orig + "-orig"
    for tag in autos:
        if _norm(tag) == orig:
            return tag
    return None


def select_track(info, prefer_langs=None):
    """(tag, is_auto, translated) 또는 None. 원어 자막 우선.

    우선순위: ①수동 원어 ②수동 선호/임의 ③**원어 자동자막**(덜 손실·Claude가 번역)
    ④선호 언어 자동자막(자동번역됐을 수 있음·최후). translated=True면 요약 시 번역 개입.
    """
    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    orig = detect_orig_lang(info)

    if orig:                                   # ① 수동 원어
        t = _match_lang(subs, orig)
        if t:
            return (t, False, False)
    for lang in (prefer_langs or list(subs.keys())):   # ② 수동 선호/임의
        t = _match_lang(subs, lang)
        if t:
            return (t, False, bool(orig) and _norm(t) != orig)
    if orig:                                   # ③ 원어 자동자막(최우선 자동)
        t = _orig_auto_tag(autos, orig)
        if t:
            return (t, True, False)
    for lang in (prefer_langs or []):          # ④ 선호 언어 자동자막(번역 가능)
        t = _match_lang(autos, lang)
        if t:
            return (t, True, bool(orig) and _norm(lang) != orig)  # 요청언어 정규화
    return None


_TS = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})")   # 시 optional(WebVTT MM:SS 허용)
_TAG = re.compile(r"<[^>]+>")

# 접두 dedup 시간가드(초): 이 간격 초과 접두 반복은 정상발화로 보존(과삭제 방지).
# 근거 = 롤링 자동자막 큐 간격 실측(median 0.3s·95%ile ~5s) — 인접 큐에만 접두 적용.
_ROLLING_GAP_SEC = 5.0


def _to_sec(h, m, s, ms):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _fmt_ts(sec):
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _strip_tags(t):
    t = _TAG.sub("", t)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t).strip()


def parse_srt(text):
    """SRT → [{'start','end','text'}]. 타임스탬프 라인 기준 블록 파싱."""
    cues = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        ts_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_idx is None:
            continue
        stamps = _TS.findall(lines[ts_idx])
        if not stamps:
            continue
        start = _to_sec(*stamps[0])
        end = _to_sec(*stamps[1]) if len(stamps) > 1 else start
        txt = " ".join(lines[ts_idx + 1:]).strip()
        if txt:
            cues.append({"start": start, "end": end, "text": txt})
    return cues


def clean_srt(text, marker_interval=600):
    """정제 transcript: 태그제거 · 보수적 dedup · 10분 마커.

    dedup은 보수적 — 인접 정확중복, 또는 직전이 현재의 '단어경계 접두'일 때만
    (자동자막 롤링 빌드). 원본 자막은 raw/에 보존되므로 과삭제도 복구 가능.
    """
    items = [(c["start"], _strip_tags(c["text"])) for c in parse_srt(text)]
    items = [(s, t) for s, t in items if t]

    kept = []  # [[start, text], ...]
    for start, text in items:
        if kept:
            prev = kept[-1][1]
            if text == prev:
                continue                       # 인접 정확중복
            if text.startswith(prev + " "):    # 롤링 빌드 → 더 긴 것으로 대체
                kept[-1][1] = text             # 시작시각은 최초 것 유지
                continue
        kept.append([start, text])

    lines = []
    next_marker = marker_interval
    for start, text in kept:
        while start >= next_marker:
            lines.append("[%s]" % _fmt_ts(next_marker))
            next_marker += marker_interval
        lines.append(text)
    return "\n".join(lines)


def quality_ok(text, min_chars=30):
    """정제 후 최소 문자수 게이트 — 빈/노이즈 자막 감지(길이 검열 아님).

    하한을 낮게(30) 둬 짧은 정상 영상도 통과시킨다. 태그 잔존 쓰레기는
    parse_vtt strip 완전성이 별도 차단하므로 대상은 순수 텍스트.
    """
    return len(text.strip()) >= min_chars


# 대괄호-only 줄 = 10분 마커([00:10:00])·음향 이벤트([Music]·[Applause]).
_BRACKET_ONLY_RE = re.compile(r"^\[[^\]]*\]$")


def _speech_text(transcript):
    """마커·음향 이벤트([Music] 등)를 뺀 실질 발화 텍스트 — quality 판정 대상.

    순수 음악/박수만 있는 영상이 가짜 성공(silent-failure)하지 않게 한다.
    """
    return "\n".join(l for l in transcript.splitlines()
                     if l.strip() and not _BRACKET_ONLY_RE.match(l.strip()))


def safe_filename(title, max_len=100):
    """파일명 allowlist(영숫자·한글·space·- _) · 나머지 치환 · 길이 상한."""
    out = [ch if (ch.isalnum() or ch in " -_") else " " for ch in title]
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    return s[:max_len].strip()


def _parse_vtt_cues(raw):
    """WebVTT → [(start, end, line)]. 큐 body 각 줄이 개별 항목.

    `-->` 포함 첫 줄만 타임스탬프로 파싱(본문 시간표기 오인 방지) · start<=end 검증 ·
    태그 strip · 빈 줄 제거. `-->` 없는 블록(WEBVTT 헤더·NOTE·STYLE)·손상 타이밍은 skip.
    raise 안 함(손상 입력은 부분 복구).
    """
    cues = []
    for block in re.split(r"\n\s*\n", raw.strip()):
        lines = block.splitlines()
        ts_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_idx is None:
            continue                                # 헤더·NOTE·STYLE
        stamps = _TS.findall(lines[ts_idx])
        if len(stamps) < 2:
            continue                                # 손상 타이밍
        start, end = _to_sec(*stamps[0]), _to_sec(*stamps[1])
        if end < start:
            continue                                # start>end 손상
        body = lines[ts_idx + 1:]
        # 인라인 타이밍 태그(<00:..>) = 롤링 자동자막 → 줄별 항목(2줄창 dedup 위해).
        # 없으면 정적 자막 → 큐 내 줄 병합(단어 래핑 복원).
        if any(re.search(r"<\d\d:\d\d:\d\d", l) for l in body):
            parts = [_strip_tags(l) for l in body]
        else:
            parts = [_strip_tags(" ".join(body))]
        for t in parts:
            if t:
                cues.append((start, end, t))
    return cues


def _dedup_rolling(cues):
    """롤링 중복 제거 → [(start, text)]. 인접 kept 마지막과만 비교(원거리 보존).

    ① 정확중복 → skip ② 시간인접 + kept가 현재의 단어경계 접두 → 대체(성장)
    ③ 시간인접 + 현재가 kept의 접두 → skip ④ else → append(충돌 시 보존 우선).
    접두 규칙 ②③은 시간 인접(gap<=_ROLLING_GAP_SEC)에만 — 원거리 정상반복 보존.
    """
    kept = []  # [[start, end, text], ...]
    for start, end, text in cues:
        if kept:
            p_start, p_end, prev = kept[-1]
            # 시간 인접 = |gap| ≤ GAP. 겹침(작은 음수)은 롤링으로 허용,
            # 큰 역행(원거리)은 배제. 원거리 정상반복 보존.
            near = abs(start - p_end) <= _ROLLING_GAP_SEC
            if near and text == prev:
                continue                            # ① 정확중복(시간 인접만·원거리 후렴 보존)
            if near and text.startswith(prev + " "):
                kept[-1] = [p_start, end, text]     # ② 전방 성장(시작시각 유지)
                continue
            if near and prev.startswith(text + " "):
                continue                            # ③ 역접두(더 긴 것 이미 있음)
        kept.append([start, end, text])
    return [(s, t) for s, _, t in kept]


def parse_vtt(raw, marker_interval=600):
    """WebVTT → 정제 transcript(롤링 dedup·태그 strip·10분 마커). 순수·raise 안 함."""
    lines, next_marker = [], marker_interval
    for start, text in _dedup_rolling(_parse_vtt_cues(raw)):
        while start >= next_marker:
            lines.append("[%s]" % _fmt_ts(next_marker))
            next_marker += marker_interval
        lines.append(text)
    return "\n".join(lines)


# ── 오케스트레이션 (subprocess — 단위테스트 밖, e2e 검증) ──────────

def run_ytdlp_json(url):
    # --no-playlist: 단일 영상만(플레이리스트/채널 자원 고갈 차단·D-K).
    # run_capped 경유(-J stdout tempfile 리다이렉트·64MB 캡 초과 시 None·메모리 축적 차단).
    # timeout: 무한 블로킹 차단 → TimeoutExpired 를 RuntimeError 로 승격하면
    # main/run_extract 의 _RETRYABLE("timed out") 분류로 DOWNLOAD_FAILED 처리.
    argv = ["yt-dlp", "-J", "--no-warnings", "--no-playlist", "--", url]
    try:
        rc, out, err = storage.run_capped(
            argv, timeout=DOWNLOAD_TIMEOUT_SEC, want_stdout=True,
            stdout_cap=_INFO_STDOUT_CAP_BYTES, stderr_cap=_STDERR_CAP_BYTES)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("yt-dlp 정보 수집 timed out: %s" % e)
    if rc != 0:
        raise RuntimeError("yt-dlp 정보 수집 실패: " + err.strip()[:300])
    if out is None:
        raise RuntimeError("yt-dlp 정보 과대(%d bytes 초과)" % _INFO_STDOUT_CAP_BYTES)
    try:                                   # 비-JSON stdout → 크래시 아닌 분류종료(계약)
        return json.loads(out)
    except ValueError as e:
        raise RuntimeError("yt-dlp 정보 JSON 파싱 실패: %s" % e)


def download_sub(url, tag, outdir, vid, fmt="vtt", retries=3):
    """선정 트랙을 네이티브 fmt로 다운로드. 반환 (sub_path, status).

    status: 'ok'(파일 확보) · 'failed'(429/네트워크로 재시도 소진 — 트랙은 있음) ·
    'no_file'(비재시도성으로 파일 없음). 'failed'는 일시 오류이므로 재시도 대상.

    판정: returncode==0 **먼저** + 정확 파일명(`<vid>.<tag>.<fmt>`) 존재. glob 와일드카드
    금지(부분/타언어 파일 성공 오인 0·CK-10). run_capped 경유(stderr tempfile 캡·stdout
    DEVNULL). `--max-filesize` 로 다운로드중 1차 disk 캡. 재시도 전 부분/잔존 파일 정리.
    지수 백오프 재시도(최대 retries회·상한 60s).
    """
    expected = os.path.join(outdir, "%s.%s.%s" % (vid, tag, fmt))
    argv = ["yt-dlp", "--write-subs", "--write-auto-subs", "--skip-download",
            "--no-playlist", "--sub-langs", tag, "--sub-format", fmt,
            "--max-filesize", str(SUBTITLE_FILE_MAX_BYTES),
            "-o", os.path.join(outdir, "%(id)s.%(ext)s"), "--", url]
    delay, last_err = 5, ""
    for attempt in range(retries + 1):
        if os.path.exists(expected):
            os.remove(expected)             # 재시도 전 부분/잔존 정리(성공 오인 차단)
        try:
            rc, _out, err = storage.run_capped(
                argv, timeout=DOWNLOAD_TIMEOUT_SEC, want_stdout=False,
                stdout_cap=0, stderr_cap=_STDERR_CAP_BYTES)
        except subprocess.TimeoutExpired:
            # 타임아웃 = 일시 오류로 취급(재시도 대상·최종엔 'failed').
            last_err = "download timed out"
            if attempt < retries:
                time.sleep(min(delay, 60))
                delay *= 3
                continue
            break
        if rc == 0 and os.path.exists(expected):
            return expected, "ok"           # returncode 우선 + 정확 파일명
        last_err = err.strip()
        if attempt < retries and _RETRYABLE.search(last_err):
            time.sleep(min(delay, 60))
            delay *= 3
            continue
        break
    return None, ("failed" if _RETRYABLE.search(last_err) else "no_file")


def run_extract(url, lang, output_root):
    """오케스트레이션 SSOT — main·server 공통. URL검증→정보수집→트랙선정→캐시조회→
    temp확보→raw보존→json3 best-effort→parse_vtt→quality 게이트→atomic 발행.

    output_root = 저장 루트(D3-D). video_id 는 추출 후에야 알 수 있어 run_extract 가
    temp/final 을 스스로 관리한다. 반환 = ExtractResult(exit_code·message·meta·transcript).
    실패경로는 temp 를 finally 정리해 디스크 흔적 0(완결세트만 published·AC-11).
    (⚠ `shutil.which` preflight 는 여기 두지 않음 — server 경계에서·테스트 결정성.)
    """
    # ① URL 검증 (SSRF allowlist) — 실패는 크래시가 아닌 분류 종료
    try:
        url = validate_url(url)
    except ValueError as e:
        return ExtractResult(EXIT_BAD_INPUT, "BAD_INPUT %s" % e, {}, None)

    root = os.path.abspath(output_root)

    # ② 영상 정보 수집 (크래시0: RuntimeError를 재시도성/불가로 분류) — 캐시히트에도 1회(D3-C)
    try:
        info = run_ytdlp_json(url)
    except RuntimeError as e:
        if _RETRYABLE.search(str(e)):
            return ExtractResult(
                EXIT_DOWNLOAD_FAILED,
                "DOWNLOAD_FAILED 정보 수집 일시 실패(rate-limit/네트워크). "
                "잠시 후 재시도 요망.", {}, None)
        return ExtractResult(
            EXIT_UNAVAILABLE,
            "UNAVAILABLE 영상 접근 불가(비공개/삭제/지역/연령 등).", {}, None)

    # ③ vid·langs·orig·meta 조립
    vid = info.get("id")
    if not vid:                            # id 누락 = 정보 계약 위반 → None/ 저장본 생성 차단
        return ExtractResult(EXIT_UNAVAILABLE,
                             "UNAVAILABLE 영상 id 없음(정보 수집 이상).", {}, None)
    langs = [l.strip() for l in lang.split(",") if l.strip()] if lang else None
    orig = detect_orig_lang(info)
    meta = {
        "id": vid, "title": info.get("title"), "uploader": info.get("uploader"),
        "duration": info.get("duration"), "duration_string": info.get("duration_string"),
        "upload_date": info.get("upload_date"),
        "url": info.get("webpage_url") or url,
        "orig_lang": orig,
    }

    # ④ 트랙 선정 → 없으면 NO_SUBTITLE(인메모리 meta·디스크쓰기 0)
    sel = select_track(info, langs)
    if not sel:
        meta["status"] = "no-subtitle"
        return ExtractResult(EXIT_NO_SUBTITLE,
                             "NO_SUBTITLE 자막 트랙 없음. id=%s" % vid, meta, None)
    tag, is_auto, translated = sel

    # ⑤ 경로 성분 안전성(traversal 필수차단) — 위반은 OSError→server OUTPUT_WRITE_FAILED
    if not (storage.is_safe_component(str(vid)) and storage.is_safe_component(tag)):
        raise OSError("안전하지 않은 경로 성분: vid=%r tag=%r" % (vid, tag))

    # ⑥ 캐시조회(다운로드 전) — 완결 저장본 있으면 자막 다운로드 스킵(D3-C)
    cached = storage.find_cached(root, str(vid), tag)
    if cached:
        r = storage.read_published(cached)
        if r:
            c_transcript, c_meta = r
            return ExtractResult(
                EXIT_OK,
                "OK transcript 캐시 히트(id=%s lang=%s chars=%d) → %s"
                % (vid, tag, len(c_transcript), cached), c_meta, c_transcript)

    # ⑦ 디스크 총량 상한(D3-E) — HARD 거부 · SOFT 경고 · 캐시 히트는 면제(위에서 반환)
    used = storage.disk_usage(root)
    if used > DISK_HARD_BYTES:
        return ExtractResult(
            EXIT_STORAGE_LIMIT,
            "STORAGE_LIMIT 저장 디스크 총량 상한 초과(used=%d bytes). 정리 후 재시도." % used,
            meta, None)
    if used > DISK_SOFT_BYTES:
        print("경고: 저장 디스크 사용량 %d bytes (soft 상한 %d 초과)"
              % (used, DISK_SOFT_BYTES), file=sys.stderr)

    # ⑧ temp 확보 → 완결 후 atomic 발행. 실패/예외는 finally 로 temp 정리(흔적 0)
    temp = storage.make_temp(root)
    try:
        # a 본경로: 네이티브 vtt 확보
        sub_path, status = download_sub(url, tag, temp, vid, fmt="vtt")
        if status == "failed":
            meta["status"] = "download-failed"
            return ExtractResult(
                EXIT_DOWNLOAD_FAILED,
                "DOWNLOAD_FAILED 자막 트랙 존재·다운로드 실패(rate-limit/네트워크). "
                "잠시 후 재시도 요망. id=%s" % vid, meta, None)
        if status == "no_file" or not sub_path:
            meta["status"] = "no-subtitle"
            return ExtractResult(
                EXIT_NO_SUBTITLE,
                "NO_SUBTITLE 자막 다운로드 파일 없음. id=%s" % vid, meta, None)

        # b 하드캡 백스톱: 읽기 전 stat → 초과 시 삭제 + SUBTITLE_TOO_LARGE(전량 메모리읽기 0)
        if os.stat(sub_path).st_size > SUBTITLE_FILE_MAX_BYTES:
            os.remove(sub_path)
            return ExtractResult(
                EXIT_SUBTITLE_TOO_LARGE,
                "SUBTITLE_TOO_LARGE 자막 파일이 최대 크기(%d bytes)를 초과했습니다. id=%s"
                % (SUBTITLE_FILE_MAX_BYTES, vid), meta, None)

        # c 원본 vtt를 raw/에 불변 보존(fsync). scratch 정리는 발행 전 sweep(g′)에 일임
        raw_vtt = os.path.join(temp, "raw", "%s.%s.vtt" % (vid, tag))
        storage.copy_file_synced(sub_path, raw_vtt)
        meta["lang"] = tag
        meta["is_auto"] = is_auto
        meta["translated"] = translated
        meta["raw_vtt"] = os.path.relpath(raw_vtt, temp)

        # d D2 비교자료: json3 best-effort (실패·캡초과 시 json3만 skip·전체실패 아님)
        json3_path, json3_status = download_sub(url, tag, temp, vid, fmt="json3", retries=0)
        if json3_status == "ok" and json3_path:
            try:
                if os.stat(json3_path).st_size <= SUBTITLE_FILE_MAX_BYTES:
                    raw_json3 = os.path.join(temp, "raw", "%s.%s.json3" % (vid, tag))
                    storage.copy_file_synced(json3_path, raw_json3)
                    meta["raw_json3"] = os.path.relpath(raw_json3, temp)
            except OSError:
                pass                        # json3 = 비치명(best-effort)

        # e transcript 생성 (Phase 1). silent-failure 차단 = quality_ok 게이트
        with open(raw_vtt, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        transcript = parse_vtt(raw)
        if not quality_ok(_speech_text(transcript)):
            meta["status"] = "empty-transcript"
            return ExtractResult(
                EXIT_EMPTY_TRANSCRIPT,
                "EMPTY_TRANSCRIPT 자막은 받았으나 정제 결과 무효(빈/과소). "
                "id=%s lang=%s" % (vid, tag), meta, None)

        # f meta 확정 + temp 안에 완결세트 기록(fsync)
        meta["status"] = "ok"
        meta["transcript"] = "transcript.txt"
        storage.write_text_synced(os.path.join(temp, "transcript.txt"), transcript)
        storage.write_text_synced(os.path.join(temp, "meta.json"),
                                  json.dumps(meta, ensure_ascii=False, indent=2))

        # g′ 발행 전 sweep — 완결세트(transcript.txt·meta.json·raw/) 외 최상위 잔여
        #    scratch 파일 무조건 제거 → 리프 형태 불변 보장(CK-1·정규화 미스 stray 포함)
        for name in os.listdir(temp):
            p = os.path.join(temp, name)
            if os.path.isfile(p) and name not in ("transcript.txt", "meta.json"):
                os.remove(p)

        # g 불변 버전 디렉토리로 atomic 발행. 경쟁패자는 재조회(idempotent·D3-B)
        final = os.path.join(root, str(vid),
                             storage.version_dir_name(tag, content_hash(transcript)))
        if not storage.atomic_publish(temp, final, root):
            r = storage.read_published(final)
            if r:
                p_transcript, p_meta = r
                return ExtractResult(
                    EXIT_OK,
                    "OK transcript 동시발행 재조회(id=%s lang=%s chars=%d) → %s"
                    % (vid, tag, len(p_transcript), final), p_meta, p_transcript)
            # 경쟁 패자인데 기존본 읽기 실패(손상 가능) → false success 금지·재시도성 분류
            return ExtractResult(
                EXIT_DOWNLOAD_FAILED,
                "DOWNLOAD_FAILED 발행 경쟁 후 기존 저장본 읽기 실패(손상 가능). "
                "잠시 후 재시도 요망. id=%s" % vid, meta, None)
        return ExtractResult(
            EXIT_OK,
            "OK transcript 생성(id=%s lang=%s chars=%d) → %s"
            % (vid, tag, len(transcript), final), meta, transcript)
    finally:
        shutil.rmtree(temp, ignore_errors=True)   # 발행성공=no-op·실패=흔적 정리


def main(argv=None):
    ap = argparse.ArgumentParser(description="유튜브 자막 추출 → transcript")
    ap.add_argument("url")
    ap.add_argument("--lang", default=None,
                    help="자막 선호 언어(쉼표구분·원어 우선 후 폴백). 미지정 시 원본 언어 자동감지·우선")
    ap.add_argument("--out", required=True,
                    help="저장 루트 — <root>/<video_id>/<tag>-<hash>/ 에 완결세트 발행")
    a = ap.parse_args(argv)
    result = run_extract(a.url, a.lang, a.out)
    print(result.message)
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
