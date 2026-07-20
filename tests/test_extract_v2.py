"""run_extract(emit_markdown=True) 통합 테스트 (seam fake·네트워크 0).

CK-13 v1 무손상 증분·CK-14 캐시 완료마커·CK-20 v1 격리(emit_markdown=False 바이트동일).
seam = run_ytdlp_json / download_sub 고수준 대체(test_orchestration 패턴 계승).
"""
import glob
import json
import os

import extract as E

VALID_URL = "https://youtu.be/vidOK"

VTT = """WEBVTT

00:00:00.000 --> 00:00:05.000
introduction to the topic covered here

00:10:30.000 --> 00:10:35.000
main body content of the video section

00:20:00.000 --> 00:20:05.000
ending remarks and the final conclusion
"""

CHAPTERS = [{"start_time": 0, "title": "Intro"},
            {"start_time": 600, "title": "Body"},
            {"start_time": 1200, "title": "End"}]


def _info(chapters=None):
    d = {"id": "vidOK", "language": "en-US", "subtitles": {"en": [{}]},
         "automatic_captions": {}, "title": "My Video", "uploader": "U",
         "duration": 1210, "duration_string": "20:10", "upload_date": "20260101",
         "webpage_url": VALID_URL}
    if chapters is not None:
        d["chapters"] = chapters
    return d


def _dl_ok(url, tag, outdir, vid, fmt="vtt", retries=3):
    if fmt != "vtt":
        return None, "no_file"                      # json3 skip
    path = os.path.join(outdir, "%s.%s.vtt" % (vid, tag))
    with open(path, "w", encoding="utf-8") as f:
        f.write(VTT)
    return path, "ok"


def _setup(monkeypatch, info, dl=_dl_ok):
    monkeypatch.setattr(E, "run_ytdlp_json", lambda url: info)
    monkeypatch.setattr(E, "download_sub", dl)


def _leaf(root):
    return glob.glob(os.path.join(root, "vidOK", "*"))[0]


# ── 신규 추출: 챕터 有 → 챕터 경계 분할 ────────────────────

def test_new_extract_with_chapters(monkeypatch, tmp_path):
    _setup(monkeypatch, _info(CHAPTERS))
    root = str(tmp_path / "out")
    r = E.run_extract(VALID_URL, None, root, emit_markdown=True)
    assert r.exit_code == E.EXIT_OK
    assert r.parts is not None and len(r.parts) == 3
    assert [p["title"] for p in r.parts] == ["Intro", "Body", "End"]
    # 저장 세트 = v1(txt·meta·raw) + v2(transcript.md·parts/)
    assert set(os.listdir(_leaf(root))) == {
        "transcript.txt", "meta.json", "raw", "transcript.md", "parts"}
    m = json.load(open(os.path.join(_leaf(root), "meta.json"), encoding="utf-8"))
    assert len(m["parts"]) == 3 and m["transcript_md"] == "transcript.md"


# ── 신규 추출: 챕터 無 → 폴백 ──────────────────────────────

def test_new_extract_no_chapters_fallback(monkeypatch, tmp_path):
    _setup(monkeypatch, _info(None))
    root = str(tmp_path / "out")
    r = E.run_extract(VALID_URL, None, root, emit_markdown=True)
    assert r.exit_code == E.EXIT_OK
    assert r.parts is not None and len(r.parts) >= 1
    assert all(p["chapter_no"] == 0 for p in r.parts)        # 폴백 = 챕터 0


# ── v1 격리: emit_markdown=False → 파트 0·transcript.md 미생성 ──

def test_v1_isolation_no_markdown(monkeypatch, tmp_path):
    _setup(monkeypatch, _info(CHAPTERS))
    root = str(tmp_path / "out")
    r = E.run_extract(VALID_URL, None, root, emit_markdown=False)
    assert r.exit_code == E.EXIT_OK
    assert r.parts is None
    assert set(os.listdir(_leaf(root))) == {"transcript.txt", "meta.json", "raw"}


# ── v1 캐시 증분: 재호출 → 다운로드 0·v1 무손상·.md 추가 ────

def test_v1_cache_incremental(monkeypatch, tmp_path):
    root = str(tmp_path / "out")
    _setup(monkeypatch, _info(CHAPTERS))
    E.run_extract(VALID_URL, None, root, emit_markdown=False)     # v1 저장본
    leaf = _leaf(root)
    before_txt = open(os.path.join(leaf, "transcript.txt"), encoding="utf-8").read()

    called = []
    def _dl_spy(*a, **k):
        called.append(1)
        return _dl_ok(*a, **k)
    _setup(monkeypatch, _info(CHAPTERS), dl=_dl_spy)

    r = E.run_extract(VALID_URL, None, root, emit_markdown=True)  # 캐시 히트 → 증분
    assert r.exit_code == E.EXIT_OK
    assert r.parts is not None
    assert called == []                                          # 무네트워크(다운로드 0)
    assert open(os.path.join(leaf, "transcript.txt"), encoding="utf-8").read() == before_txt
    assert os.path.exists(os.path.join(leaf, "transcript.md"))
    m = json.load(open(os.path.join(leaf, "meta.json"), encoding="utf-8"))
    assert m["parts"] and storage_read_ok(leaf, m)


def storage_read_ok(leaf, meta):
    import storage
    return storage.read_v2_parts(leaf, meta) is not None


# ── v2 캐시 히트: 재호출 → read_v2_parts(증분 안 함) ────────

def test_v2_cache_hit(monkeypatch, tmp_path):
    root = str(tmp_path / "out")
    _setup(monkeypatch, _info(CHAPTERS))
    E.run_extract(VALID_URL, None, root, emit_markdown=True)      # v2 저장(parts 有)

    called = []
    def _dl_spy(*a, **k):
        called.append(1)
        return _dl_ok(*a, **k)
    _setup(monkeypatch, _info(CHAPTERS), dl=_dl_spy)

    r = E.run_extract(VALID_URL, None, root, emit_markdown=True)  # v2 캐시 히트
    assert r.exit_code == E.EXIT_OK
    assert r.parts is not None and len(r.parts) == 3
    assert called == []                                          # 무네트워크


# ── v1 캐시 증분이 info.chapters 활용(폴백 아님·codex #6 재작업) ──

def test_v1_cache_incremental_uses_chapters(monkeypatch, tmp_path):
    root = str(tmp_path / "out")
    _setup(monkeypatch, _info(CHAPTERS))
    E.run_extract(VALID_URL, None, root, emit_markdown=False)     # v1 저장본(챕터 메타 없음)
    _setup(monkeypatch, _info(CHAPTERS))
    r = E.run_extract(VALID_URL, None, root, emit_markdown=True)  # 증분 — info.chapters 활용
    assert r.parts is not None
    assert any(p["chapter_no"] >= 1 for p in r.parts)            # 챕터 분할(폴백 0 아님)


# ── 폴백 cue 확보 시 마커 줄 배제(가짜 cue 차단·reviewer P1) ──

def test_cues_from_cached_excludes_marker_lines():
    c_transcript = "[00:10:00]\nreal line\n[00:20:00]\nanother"
    cues = E._cues_from_cached("/nonexistent-dir", {}, c_transcript)   # raw 없음 → 폴백
    texts = [t for _, t in cues]
    assert "[00:10:00]" not in texts and "[00:20:00]" not in texts
    assert "real line" in texts and "another" in texts


def test_cues_from_cached_falls_back_on_empty_parse(tmp_path):
    """열리지만 cue 0 개인 raw vtt 는 transcript 폴백(빈 파트 '성공' 차단·HIGH)."""
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "x.en.vtt").write_text("GARBAGE not a vtt\nno cues here\n")
    cues = E._cues_from_cached(str(tmp_path), {"raw_vtt": "raw/x.en.vtt"},
                               "실제 transcript 본문\n둘째 줄")
    assert len(cues) > 0   # 빈 파싱도 transcript 폴백으로 본문 보존(빈 파트 차단)
