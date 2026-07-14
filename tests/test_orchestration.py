"""main() 실패분류 + AC-5 + download_sub 오케스트레이션 테스트 (pytest + monkeypatch).

seam = 고수준: run_ytdlp_json / download_sub 를 통째로 대체(argv 정확일치 assert 금지
= 후속 Phase 가 argv 를 바꿔도 분류결과 테스트는 안 깨짐). download_sub 자체는
subprocess.run / time.sleep 모킹으로 단위 검증.
"""
import glob
import os
import types

import pytest

import extract as E

VALID_URL = "https://youtu.be/vidOK"


def _info(vid="vidOK", subs=None, autos=None, lang="en-US"):
    return {
        "id": vid, "language": lang,
        "subtitles": subs or {}, "automatic_captions": autos or {},
        "title": "T", "uploader": "U", "duration": 10,
        "duration_string": "0:10", "upload_date": "20260101",
        "webpage_url": VALID_URL,
    }


def _run_main(monkeypatch, tmp_path, info=None, info_exc=None, dl=None):
    """main 을 모킹된 seam 으로 실행하고 (exit_code, out_dir) 반환."""
    def fake_json(url):
        if info_exc is not None:
            raise info_exc
        return info
    monkeypatch.setattr(E, "run_ytdlp_json", fake_json)
    if dl is not None:
        monkeypatch.setattr(E, "download_sub", dl)
    out = str(tmp_path / "out")
    code = E.main([VALID_URL, "--out", out])
    return code, out


# ── main 분류: BAD_INPUT (CK-5) ────────────────────────────────

def test_bad_input_url_returns_5(tmp_path):
    # SSRF/비유튜브 URL → validate_url ValueError → 크래시 없이 exit 5
    code = E.main(["https://evil.com/youtube.com", "--out", str(tmp_path / "o")])
    assert code == E.EXIT_BAD_INPUT


# ── main 분류: 자막없음 → 2 ────────────────────────────────────

def test_no_subtitle_returns_2(monkeypatch, tmp_path):
    code, out = _run_main(monkeypatch, tmp_path, info=_info(subs={}, autos={}))
    assert code == E.EXIT_NO_SUBTITLE
    assert os.path.exists(os.path.join(out, "meta.json"))


# ── main 분류: 비공개/삭제/지역/연령 → 4 (info fetch 비재시도 실패) ──

@pytest.mark.parametrize("stderr", [
    "ERROR: Private video. Sign in if you've been granted access.",
    "ERROR: Video unavailable. This video has been removed by the uploader.",
    "ERROR: The uploader has not made this video available in your country.",
    "ERROR: Sign in to confirm your age. This video may be inappropriate.",
])
def test_unavailable_returns_4(monkeypatch, tmp_path, stderr):
    code, _ = _run_main(monkeypatch, tmp_path, info_exc=RuntimeError(stderr))
    assert code == E.EXIT_UNAVAILABLE


# ── main 분류: 네트워크(정보수집 일시실패) → 3 ────────────────────

@pytest.mark.parametrize("stderr", [
    "ERROR: Unable to download webpage: Temporary failure in name resolution",
    "ERROR: Unable to download API page: HTTP Error 429: Too Many Requests",
    "ERROR: [Errno 54] Connection reset by peer",
])
def test_network_info_failure_returns_3(monkeypatch, tmp_path, stderr):
    code, _ = _run_main(monkeypatch, tmp_path, info_exc=RuntimeError(stderr))
    assert code == E.EXIT_DOWNLOAD_FAILED


# ── main 분류: 429 지속(자막 다운로드 재시도 소진) → 3 ────────────

def test_download_persistent_429_returns_3(monkeypatch, tmp_path):
    def fake_dl(url, tag, outdir, vid, fmt="vtt", retries=3):
        return None, "failed"
    code, out = _run_main(monkeypatch, tmp_path,
                          info=_info(subs={"en": [{}]}), dl=fake_dl)
    assert code == E.EXIT_DOWNLOAD_FAILED
    assert os.path.exists(os.path.join(out, "meta.json"))


# ── AC-5: 정상경로 → transcript.txt + exit 0 / 과소 → exit 6(silent 차단) ──

def _fake_dl_ok(url, tag, outdir, vid, fmt="vtt", retries=3):
    """vtt 성공(quality_ok 통과 자막), json3 best-effort 미확보 시뮬."""
    if fmt == "vtt":
        p = os.path.join(outdir, "%s.%s.vtt" % (vid, tag))
        with open(p, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
                    "this is a real transcript with plenty of words to pass quality\n")
        return p, "ok"
    return None, "no_file"


def test_success_path_writes_transcript_returns_0(monkeypatch, tmp_path):
    code, out = _run_main(monkeypatch, tmp_path,
                          info=_info(subs={"en": [{}]}), dl=_fake_dl_ok)
    assert code == E.EXIT_OK
    assert glob.glob(os.path.join(out, "raw", "*.vtt"))
    tpath = os.path.join(out, "transcript.txt")
    assert os.path.exists(tpath)
    assert "real transcript" in open(tpath, encoding="utf-8").read()
    assert os.path.exists(os.path.join(out, "meta.json"))


def _fake_dl_empty(url, tag, outdir, vid, fmt="vtt", retries=3):
    """vtt 는 받아지나 정제 결과가 과소(빈 transcript 시뮬)."""
    if fmt == "vtt":
        p = os.path.join(outdir, "%s.%s.vtt" % (vid, tag))
        with open(p, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n")
        return p, "ok"
    return None, "no_file"


def test_empty_transcript_returns_6_no_file(monkeypatch, tmp_path):
    code, out = _run_main(monkeypatch, tmp_path,
                          info=_info(subs={"en": [{}]}), dl=_fake_dl_empty)
    assert code == E.EXIT_EMPTY_TRANSCRIPT
    # silent-failure 차단: 과소 transcript 는 파일 안 씀
    assert not os.path.exists(os.path.join(out, "transcript.txt"))
    assert os.path.exists(os.path.join(out, "meta.json"))


def _fake_dl_music(url, tag, outdir, vid, fmt="vtt", retries=3):
    """음향 이벤트([Music]/[Applause])만 있는 영상 시뮬 — 실질 발화 0."""
    if fmt == "vtt":
        p = os.path.join(outdir, "%s.%s.vtt" % (vid, tag))
        with open(p, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n[Music]\n\n"
                    "00:02:00.000 --> 00:02:01.000\n[Applause]\n\n"
                    "00:04:00.000 --> 00:04:01.000\n[Music]\n")
        return p, "ok"
    return None, "no_file"


def test_music_only_returns_6(monkeypatch, tmp_path):
    # 순수 음향 영상이 exit0 가짜 성공하지 않음 (codex HIGH-3/P1-2)
    code, out = _run_main(monkeypatch, tmp_path,
                          info=_info(subs={"en": [{}]}), dl=_fake_dl_music)
    assert code == E.EXIT_EMPTY_TRANSCRIPT
    assert not os.path.exists(os.path.join(out, "transcript.txt"))


# ── download_sub 단위: subprocess.run / time.sleep 모킹 ──────────

def _fake_run_factory(tmp_path, filename, returncode=0, stderr=""):
    def fake_run(argv, **kw):
        if filename:
            (tmp_path / filename).write_text("WEBVTT\n", encoding="utf-8")
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)
    return fake_run


def test_download_sub_success_vtt(monkeypatch, tmp_path):
    monkeypatch.setattr(E.subprocess, "run",
                        _fake_run_factory(tmp_path, "vidX.en.vtt"))
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX")
    assert status == "ok"
    assert path.endswith("vidX.en.vtt")


def test_download_sub_fmt_json3_glob(monkeypatch, tmp_path):
    # fmt 파라미터화 → glob 확장자가 fmt 를 따른다(CK-12)
    monkeypatch.setattr(E.subprocess, "run",
                        _fake_run_factory(tmp_path, "vidX.en.json3"))
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX",
                                  fmt="json3", retries=0)
    assert status == "ok"
    assert path.endswith(".json3")


def test_download_sub_retry_exhausted_failed(monkeypatch, tmp_path):
    calls, sleeps = [], []

    def fake_run(argv, **kw):
        calls.append(1)
        return types.SimpleNamespace(returncode=1, stdout="",
                                     stderr="HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(E.subprocess, "run", fake_run)
    monkeypatch.setattr(E.time, "sleep", lambda s: sleeps.append(s))
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX", retries=3)
    assert status == "failed"
    assert path is None
    assert len(calls) == 4          # 최초 1 + 재시도 3
    assert len(sleeps) == 3
    assert max(sleeps) <= 60        # 백오프 상한 60s (CK-12)


def test_download_sub_non_retryable_no_file(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, **kw):
        calls.append(1)
        return types.SimpleNamespace(
            returncode=1, stdout="",
            stderr="There are no subtitles for the requested languages")
    monkeypatch.setattr(E.subprocess, "run", fake_run)
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX", retries=3)
    assert status == "no_file"
    assert path is None
    assert len(calls) == 1          # 비재시도 → 1회만
