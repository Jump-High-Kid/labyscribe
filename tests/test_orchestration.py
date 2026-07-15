"""main() 실패분류 + AC-5 + download_sub 오케스트레이션 테스트 (pytest + monkeypatch).

seam = 고수준: run_ytdlp_json / download_sub 를 통째로 대체(argv 정확일치 assert 금지
= 후속 Phase 가 argv 를 바꿔도 분류결과 테스트는 안 깨짐). download_sub 자체는
subprocess.run / time.sleep 모킹으로 단위 검증.
"""
import glob
import os

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
    # 실패경로 디스크 흔적 0(AC-11) — 완결세트만 published, 실패는 인메모리 분류
    assert not glob.glob(os.path.join(out, "**", "meta.json"), recursive=True)


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
    # 실패경로 디스크 흔적 0 · temp 정리(AC-11)
    assert not glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)


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
    # CK-1: <root>/<vid>/<tag>-<hash>/ 완결세트(nested layout) · .tmp 잔존 0
    tset = glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)
    assert tset and "real transcript" in open(tset[0], encoding="utf-8").read()
    assert glob.glob(os.path.join(out, "**", "raw", "*.vtt"), recursive=True)
    assert glob.glob(os.path.join(out, "**", "meta.json"), recursive=True)
    # 발행 디렉토리명 = <tag>-<hash[:12]>
    vid_dir = os.path.join(out, "vidOK")
    assert os.path.isdir(vid_dir)
    leaves = os.listdir(vid_dir)
    assert len(leaves) == 1 and leaves[0].startswith("en-")
    # CK-1: 리프 완결세트 = {transcript.txt, meta.json, raw/} 정확 — scratch 중복 없음
    leaf = os.path.join(vid_dir, leaves[0])
    assert set(os.listdir(leaf)) == {"transcript.txt", "meta.json", "raw"}
    assert not os.path.exists(os.path.join(leaf, "vidOK.en.vtt"))   # scratch 미잔존
    tmp_dir = os.path.join(out, ".tmp")
    assert not os.path.isdir(tmp_dir) or os.listdir(tmp_dir) == []


def _fake_dl_ok_with_json3(url, tag, outdir, vid, fmt="vtt", retries=3):
    """vtt + json3 둘 다 확보 시뮬 — json3 scratch 도 published 세트에서 제거되는지 검증용."""
    p = os.path.join(outdir, "%s.%s.%s" % (vid, tag, fmt))
    if fmt == "vtt":
        with open(p, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
                    "this is a real transcript with plenty of words to pass quality\n")
    else:
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"events": []}\n')
    return p, "ok"


def test_success_published_set_excludes_scratch_incl_json3(monkeypatch, tmp_path):
    # CK-1: vtt·json3 scratch 모두 raw/ 로만 들어가고 리프 최상위엔 안 실림
    code, out = _run_main(monkeypatch, tmp_path,
                          info=_info(subs={"en": [{}]}), dl=_fake_dl_ok_with_json3)
    assert code == E.EXIT_OK
    leaf = glob.glob(os.path.join(out, "vidOK", "en-*"))[0]
    assert set(os.listdir(leaf)) == {"transcript.txt", "meta.json", "raw"}
    assert set(os.listdir(os.path.join(leaf, "raw"))) == {"vidOK.en.vtt", "vidOK.en.json3"}


def _fake_dl_vtt_ok_json3_stray(url, tag, outdir, vid, fmt="vtt", retries=3):
    """vtt 성공 · json3 는 yt-dlp 정규화로 다른 이름 씀 → exact 미스 no_file(stray 잔존)."""
    if fmt == "vtt":
        p = os.path.join(outdir, "%s.%s.vtt" % (vid, tag))
        with open(p, "w", encoding="utf-8") as f:
            f.write("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n"
                    "this is a real transcript with plenty of words to pass quality\n")
        return p, "ok"
    with open(os.path.join(outdir, "%s.%s-orig.json3" % (vid, tag)), "w") as f:
        f.write('{"events": []}')           # 정규화된 다른 이름 stray
    return None, "no_file"


def test_json3_stray_swept_before_publish(monkeypatch, tmp_path):
    # 4단계 MED: json3 정규화-미스 stray 도 발행 전 sweep 로 제거(CK-1 무조건)
    code, out = _run_main(monkeypatch, tmp_path,
                          info=_info(subs={"en": [{}]}), dl=_fake_dl_vtt_ok_json3_stray)
    assert code == E.EXIT_OK
    leaf = glob.glob(os.path.join(out, "vidOK", "en-*"))[0]
    assert set(os.listdir(leaf)) == {"transcript.txt", "meta.json", "raw"}   # stray 미포함
    assert set(os.listdir(os.path.join(leaf, "raw"))) == {"vidOK.en.vtt"}    # json3 raw 없음


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
    # silent-failure 차단 + 실패경로 흔적 0: 과소 transcript 는 어디에도 안 씀
    assert not glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)


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
    assert not glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)


# ── download_sub 단위: storage.run_capped / time.sleep 모킹 (CK-10·11) ──
# seam 이동: download_sub 는 subprocess.run 직접 대신 storage.run_capped 경유.

def _fake_capped_factory(tmp_path, filename, rc=0, stderr=""):
    """run_capped 대체 — 지정 파일을 outdir(=tmp_path)에 쓰고 (rc, None, stderr) 반환."""
    def fake(argv, **kw):
        if filename:
            (tmp_path / filename).write_text("WEBVTT\n", encoding="utf-8")
        return (rc, None, stderr)
    return fake


def test_download_sub_success_vtt(monkeypatch, tmp_path):
    monkeypatch.setattr(E.storage, "run_capped",
                        _fake_capped_factory(tmp_path, "vidX.en.vtt"))
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX")
    assert status == "ok"
    assert path.endswith("vidX.en.vtt")


def test_download_sub_fmt_json3_exact(monkeypatch, tmp_path):
    # fmt 파라미터화 → 정확 파일명 확장자가 fmt 를 따른다
    monkeypatch.setattr(E.storage, "run_capped",
                        _fake_capped_factory(tmp_path, "vidX.en.json3"))
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX",
                                  fmt="json3", retries=0)
    assert status == "ok"
    assert path.endswith("vidX.en.json3")


def test_download_sub_returncode_zero_but_wrong_filename_no_file(monkeypatch, tmp_path):
    # CK-10: rc==0 이라도 정확 파일명(<vid>.<tag>.<fmt>) 아니면 성공 오인 0(glob 금지)
    def fake(argv, **kw):
        (tmp_path / "vidX.ko.vtt").write_text("WEBVTT\n")   # 다른 언어 파일
        return (0, None, "")
    monkeypatch.setattr(E.storage, "run_capped", fake)
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX")
    assert path is None and status == "no_file"


def test_download_sub_retry_exhausted_failed(monkeypatch, tmp_path):
    calls, sleeps = [], []

    def fake(argv, **kw):
        calls.append(1)
        return (1, None, "HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(E.storage, "run_capped", fake)
    monkeypatch.setattr(E.time, "sleep", lambda s: sleeps.append(s))
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX", retries=3)
    assert status == "failed"
    assert path is None
    assert len(calls) == 4          # 최초 1 + 재시도 3
    assert len(sleeps) == 3
    assert max(sleeps) <= 60        # 백오프 상한 60s


def test_download_sub_non_retryable_no_file(monkeypatch, tmp_path):
    calls = []

    def fake(argv, **kw):
        calls.append(1)
        return (1, None, "There are no subtitles for the requested languages")
    monkeypatch.setattr(E.storage, "run_capped", fake)
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX", retries=3)
    assert status == "no_file"
    assert path is None
    assert len(calls) == 1          # 비재시도 → 1회만


def test_download_sub_cleans_stale_before_retry(monkeypatch, tmp_path):
    # CK-10: 첫 시도서 부분파일 남고 재시도성 실패 → 재시도 전 remove → 잔존파일 성공 오인 0
    expected = tmp_path / "vidX.en.vtt"
    calls = []

    def fake(argv, **kw):
        calls.append(1)
        if len(calls) == 1:
            expected.write_text("partial 429 leftover")     # 부분파일 남김
        return (1, None, "HTTP Error 429: Too Many Requests")
    monkeypatch.setattr(E.storage, "run_capped", fake)
    monkeypatch.setattr(E.time, "sleep", lambda *a: None)
    path, status = E.download_sub(VALID_URL, "en", str(tmp_path), "vidX", retries=2)
    assert path is None and status == "failed"              # 잔존파일 ok 오인 0
    assert not expected.exists()                            # 마지막 시도 전 정리됨


# ── run_extract 저장 안전: 캐시·원자성·경쟁·디스크·하드캡·containment ──

def _fake_info(monkeypatch, info):
    monkeypatch.setattr(E, "run_ytdlp_json", lambda u: info)


def test_cache_hit_skips_download(monkeypatch, tmp_path):
    # CK-5/AC-2: 2회차 동일 URL → find_cached 히트로 자막 다운로드 스킵(info 1회 허용)
    out = str(tmp_path / "out")
    calls = {"n": 0}

    def counting_dl(url, tag, outdir, vid, fmt="vtt", retries=3):
        calls["n"] += 1
        return _fake_dl_ok(url, tag, outdir, vid, fmt, retries)
    _fake_info(monkeypatch, _info(subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", counting_dl)

    r1 = E.run_extract(VALID_URL, None, out)
    assert r1.exit_code == E.EXIT_OK
    first = calls["n"]
    assert first >= 1
    r2 = E.run_extract(VALID_URL, None, out)
    assert r2.exit_code == E.EXIT_OK
    assert calls["n"] == first                              # 추가 다운로드 0(캐시 히트)
    assert r2.transcript == r1.transcript


def test_partial_failure_no_final_set(monkeypatch, tmp_path):
    # CK-3/AC-3: rename 직전 크래시 → final 부분세트 0 · .tmp finally 정리
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, _info(subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)

    def boom(src, dst):
        raise OSError("rename crash")
    monkeypatch.setattr(E.storage.os, "rename", boom)
    with pytest.raises(OSError):
        E.run_extract(VALID_URL, None, out)
    assert not glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)
    tmp_dir = os.path.join(out, ".tmp")
    assert not os.path.isdir(tmp_dir) or os.listdir(tmp_dir) == []


def test_rename_race_idempotent_requery(monkeypatch, tmp_path):
    # CK-4/AC-4: final 선점(EEXIST) → 에러 아님 → read_published 재조회
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, _info(subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)
    r1 = E.run_extract(VALID_URL, None, out)
    assert r1.exit_code == E.EXIT_OK
    # 캐시 무시 강제 → publish 경쟁 유발(동일 content_hash → 동일 leaf → EEXIST)
    monkeypatch.setattr(E.storage, "find_cached", lambda *a: None)
    r2 = E.run_extract(VALID_URL, None, out)
    assert r2.exit_code == E.EXIT_OK
    assert r2.transcript == r1.transcript


def test_disk_hard_limit_rejects(monkeypatch, tmp_path):
    # CK-8/AC-7: disk_usage HARD 초과 → STORAGE_LIMIT · 기존본 삭제 0
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, _info(subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)
    monkeypatch.setattr(E, "DISK_HARD_BYTES", 0)
    monkeypatch.setattr(E.storage, "disk_usage", lambda r: 1)
    r = E.run_extract(VALID_URL, None, out)
    assert r.exit_code == E.EXIT_STORAGE_LIMIT
    assert not glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)


def test_subtitle_too_large_rejects(monkeypatch, tmp_path):
    # CK-9/AC-8: 자막 파일 stat > 하드캡 → 삭제 + SUBTITLE_TOO_LARGE(읽기 전)
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, _info(subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)     # 작은 파일 씀
    monkeypatch.setattr(E, "SUBTITLE_FILE_MAX_BYTES", 5)    # 그보다 큰 캡
    r = E.run_extract(VALID_URL, None, out)
    assert r.exit_code == E.EXIT_SUBTITLE_TOO_LARGE
    assert not glob.glob(os.path.join(out, "**", "transcript.txt"), recursive=True)


def test_unsafe_video_id_raises(monkeypatch, tmp_path):
    # CK-6/AC-6: is_safe_component 사전거름 — 경로이탈 vid → OSError(→OUTPUT_WRITE_FAILED)
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, _info(vid="../evil", subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)
    with pytest.raises(OSError):
        E.run_extract(VALID_URL, None, out)
    assert not os.path.isdir(os.path.join(out, "..", "evil"))


def test_missing_video_id_returns_unavailable(monkeypatch, tmp_path):
    # 4단계 p3: info 에 id 없음 → None/ 저장본 생성 안 하고 UNAVAILABLE 분류
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, {"subtitles": {"en": [{}]}, "automatic_captions": {}})
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)
    r = E.run_extract(VALID_URL, None, out)
    assert r.exit_code == E.EXIT_UNAVAILABLE
    assert not os.path.isdir(os.path.join(out, "None"))


def test_run_ytdlp_json_bad_json_raises_runtimeerror(monkeypatch):
    # 4단계 codex MED: rc==0 이나 비-JSON stdout → RuntimeError(크래시 아닌 분류종료 계약)
    monkeypatch.setattr(E.storage, "run_capped",
                        lambda argv, **kw: (0, b"<not json>", ""))
    with pytest.raises(RuntimeError):
        E.run_ytdlp_json(VALID_URL)


def test_race_lost_unreadable_peer_not_false_ok(monkeypatch, tmp_path):
    # 4단계 codex/reviewer MED: 발행 경쟁 패자 + 기존본 읽기 실패 → false OK 금지·재시도성
    out = str(tmp_path / "out")
    _fake_info(monkeypatch, _info(subs={"en": [{}]}))
    monkeypatch.setattr(E, "download_sub", _fake_dl_ok)
    monkeypatch.setattr(E.storage, "atomic_publish", lambda *a: False)   # 경쟁 패
    monkeypatch.setattr(E.storage, "read_published", lambda d: None)     # 기존본 손상
    r = E.run_extract(VALID_URL, None, out)
    assert r.exit_code == E.EXIT_DOWNLOAD_FAILED                         # OK 아님
    assert r.transcript is None
