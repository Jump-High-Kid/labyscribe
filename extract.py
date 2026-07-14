#!/usr/bin/env python3
"""labyscribe extraction core — deterministic (no LLM). python3 stdlib only.

Phase 0: yt-dlp track selection -> native subtitle capture (.vtt + a .json3
sample) into <out>/raw/. No post-conversion tool required. Transcript parsing
(parse_vtt) lands in Phase 1.

Reference: README.md (Phase 0 scope).
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

# 재시도 가능한 일시 오류: rate-limit·네트워크
_RETRYABLE = re.compile(r"429|Too Many Requests|Temporary failure|timed out|Connection",
                        re.IGNORECASE)

# 종료 코드 체계 (Phase 0)
EXIT_OK = 0               # transcript 산출 — Phase 1에서만
EXIT_NO_SUBTITLE = 2      # 자막 트랙 없음 / 다운로드 파일 없음
EXIT_DOWNLOAD_FAILED = 3  # 429/네트워크 재시도 소진
EXIT_UNAVAILABLE = 4      # 비공개/삭제/지역/연령 — 정보 수집 비재시도 실패
EXIT_BAD_INPUT = 5        # validate_url 실패
EXIT_NOT_IMPLEMENTED = 20 # Phase 0 sentinel — raw 확보됨·transcript는 Phase 1

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
        if _norm(tag) == lang:
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
            return (t, True, bool(orig) and lang != orig)
    return None


_TS = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
_TAG = re.compile(r"<[^>]+>")


def _to_sec(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


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


def quality_ok(text, min_chars=200):
    """정제 후 최소 문자수 게이트. 미달 시 자막 무효 신호."""
    return len(text.strip()) >= min_chars


def safe_filename(title, max_len=100):
    """파일명 allowlist(영숫자·한글·space·- _) · 나머지 치환 · 길이 상한."""
    out = [ch if (ch.isalnum() or ch in " -_") else " " for ch in title]
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    return s[:max_len].strip()


def parse_vtt(raw):
    """WebVTT → transcript. Phase 1에서 구현(과삭제 방지 golden fixture 동반)."""
    raise NotImplementedError("parse_vtt: Phase 1")


# ── 오케스트레이션 (subprocess — 단위테스트 밖, e2e 검증) ──────────

def run_ytdlp_json(url):
    r = subprocess.run(["yt-dlp", "-J", "--no-warnings", "--", url],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("yt-dlp 정보 수집 실패: " + r.stderr.strip()[:300])
    return json.loads(r.stdout)


def download_sub(url, tag, outdir, vid, fmt="vtt", retries=3):
    """선정 트랙을 네이티브 fmt로 다운로드. 반환 (sub_path, status).

    status: 'ok'(파일 확보) · 'failed'(429/네트워크로 재시도 소진 — 트랙은 있음) ·
    'no_file'(비재시도성으로 파일 없음). 'failed'는 일시 오류이므로 재시도 대상.
    지수 백오프 재시도(최대 retries회·상한 60s).
    """
    delay, last_err = 5, ""
    for attempt in range(retries + 1):
        r = subprocess.run(
            ["yt-dlp", "--write-subs", "--write-auto-subs", "--skip-download",
             "--sub-langs", tag, "--sub-format", fmt,
             "-o", os.path.join(outdir, "%(id)s.%(ext)s"), "--", url],
            capture_output=True, text=True)
        hits = glob.glob(os.path.join(outdir, "%s*.%s" % (vid, fmt)))
        if hits:
            return hits[0], "ok"
        last_err = (r.stderr or "").strip()
        if attempt < retries and _RETRYABLE.search(last_err):
            time.sleep(min(delay, 60))
            delay *= 3
            continue
        break
    return None, ("failed" if _RETRYABLE.search(last_err) else "no_file")


def _dump_meta(outdir, meta):
    with open(os.path.join(outdir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description="유튜브 자막 추출 (Phase 0)")
    ap.add_argument("url")
    ap.add_argument("--lang", default=None,
                    help="자막 선호 언어(쉼표구분·원어 우선 후 폴백). 미지정 시 원본 언어 자동감지·우선")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    # ── URL 검증 (SSRF allowlist) — 실패는 크래시가 아닌 분류 종료 ──
    try:
        url = validate_url(a.url)
    except ValueError as e:
        print("BAD_INPUT %s" % e)
        return EXIT_BAD_INPUT

    os.makedirs(a.out, exist_ok=True)
    rawdir = os.path.join(a.out, "raw")
    os.makedirs(rawdir, exist_ok=True)

    # ── 영상 정보 수집 (크래시0: RuntimeError를 재시도성/불가로 분류) ──
    try:
        info = run_ytdlp_json(url)
    except RuntimeError as e:
        if _RETRYABLE.search(str(e)):
            print("DOWNLOAD_FAILED 정보 수집 일시 실패(rate-limit/네트워크). "
                  "잠시 후 재시도 요망.")
            return EXIT_DOWNLOAD_FAILED
        print("UNAVAILABLE 영상 접근 불가(비공개/삭제/지역/연령 등).")
        return EXIT_UNAVAILABLE

    vid = info.get("id")
    langs = [l.strip() for l in a.lang.split(",") if l.strip()] if a.lang else None
    orig = detect_orig_lang(info)
    meta = {
        "id": vid, "title": info.get("title"), "uploader": info.get("uploader"),
        "duration": info.get("duration"), "duration_string": info.get("duration_string"),
        "upload_date": info.get("upload_date"),
        "url": info.get("webpage_url") or url,
        "orig_lang": orig,
    }

    sel = select_track(info, langs)
    if not sel:
        meta["status"] = "no-subtitle"
        _dump_meta(a.out, meta)
        print("NO_SUBTITLE 자막 트랙 없음. id=%s" % vid)
        return EXIT_NO_SUBTITLE

    tag, is_auto, translated = sel

    # ── 본경로: 네이티브 vtt 확보 ──
    sub_path, status = download_sub(url, tag, a.out, vid, fmt="vtt")
    if status == "failed":
        meta["status"] = "download-failed"
        _dump_meta(a.out, meta)
        print("DOWNLOAD_FAILED 자막 트랙 존재·다운로드 실패(rate-limit/네트워크). "
              "잠시 후 재시도 요망. id=%s" % vid)
        return EXIT_DOWNLOAD_FAILED
    if status == "no_file" or not sub_path:
        meta["status"] = "no-subtitle"
        _dump_meta(a.out, meta)
        print("NO_SUBTITLE 자막 다운로드 파일 없음. id=%s" % vid)
        return EXIT_NO_SUBTITLE

    # ok → 원본 vtt를 raw/에 불변 보존
    raw_vtt = os.path.join(rawdir, "%s.%s.vtt" % (vid, tag))
    shutil.copyfile(sub_path, raw_vtt)
    meta["lang"] = tag
    meta["is_auto"] = is_auto
    meta["translated"] = translated
    meta["raw_vtt"] = os.path.relpath(raw_vtt, a.out)

    # ── D2 비교자료: json3 best-effort (실패 무시·비치명) ──
    json3_path, json3_status = download_sub(url, tag, a.out, vid, fmt="json3", retries=0)
    if json3_status == "ok" and json3_path:
        raw_json3 = os.path.join(rawdir, "%s.%s.json3" % (vid, tag))
        shutil.copyfile(json3_path, raw_json3)
        meta["raw_json3"] = os.path.relpath(raw_json3, a.out)

    meta["status"] = "raw-captured"
    _dump_meta(a.out, meta)

    # ── transcript 생성 = Phase 1. silent-failure 차단: 명시 신호로 종료 ──
    # (clean_srt를 vtt에 호출하면 그럴듯한 오답이 나옴 → Phase 0은 미호출)
    raw = open(raw_vtt, encoding="utf-8", errors="replace").read()
    try:
        parse_vtt(raw)
    except NotImplementedError:
        print("RAW_CAPTURED raw/*.vtt 확보 완료(id=%s lang=%s). "
              "TRANSCRIPT_NOT_IMPLEMENTED(Phase1)." % (vid, tag))
        return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
