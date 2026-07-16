"""Phase 6 W1 — 대표 영상군 결정적 계약(fixture-backed·네트워크 0).

대표군 5종은 아래처럼 커버된다(신규 중복 작성 없이 연결):
- 짧은수동  → exit0 (여기·`tests/fixtures/short_manual.en.vtt` = 합성 짧은 정적 자막·저작권 무관)
- 긴자동    → exit0·롤링 dedup·`<`태그 0·페이징 분리 계약 (여기·합성 롤링 fixture)
- 다국어    → 원어 우선 선택 (`test_extract.py::TestSelectTrack`
              ::test_representative_multilang_prefers_original_over_requested)
- 음악only  → exit6 (`test_orchestration.py::test_music_only_returns_6`)
- 자막없음  → exit2 (`test_orchestration.py::test_no_subtitle_returns_2`)

fixture 배선 = 기존 seam(`download_sub`/`run_ytdlp_json`) 통째 대체(argv 정확일치
assert 금지). 순수함수층 raise 도입·신규 exit code·상수 값 변경 없음.
"""
from __future__ import annotations

import pathlib
import shutil

import extract as E
import paging as P

FIX = pathlib.Path(__file__).parent / "fixtures"
VALID_URL = "https://youtu.be/vidOK"


def _info(vid="vidOK", subs=None, autos=None, lang="en-US"):
    return {
        "id": vid, "language": lang,
        "subtitles": subs or {}, "automatic_captions": autos or {},
        "title": "T", "uploader": "U", "duration": 10,
        "duration_string": "0:10", "upload_date": "20260101",
        "webpage_url": VALID_URL,
    }


def _fixture_dl(fixture_name):
    """fixture-backed fake download_sub — 선정 트랙(vtt)만 fixture 로 채운다.

    기존 `_fake_dl_ok` seam 확장: 인라인 리터럴 대신 `tests/fixtures/*.vtt` 복사.
    json3(best-effort)는 no_file → 파이프라인 영향 없음.
    """
    src = FIX / fixture_name

    def fake(url, tag, outdir, vid, fmt="vtt", retries=3):
        if fmt == "vtt":
            dst = pathlib.Path(outdir) / ("%s.%s.vtt" % (vid, tag))
            shutil.copyfile(src, dst)
            return str(dst), "ok"
        return None, "no_file"

    return fake


def _run(monkeypatch, tmp_path, info, fixture_name):
    monkeypatch.setattr(E, "run_ytdlp_json", lambda u: info)
    monkeypatch.setattr(E, "download_sub", _fixture_dl(fixture_name))
    return E.run_extract(VALID_URL, None, str(tmp_path / "out"))


# ── 대표군 ①: 짧은 수동자막 → exit0·transcript 有 ──────────────────

def test_short_manual_pipeline_exit0(monkeypatch, tmp_path):
    r = _run(monkeypatch, tmp_path, _info(subs={"en": [{}]}),
             "short_manual.en.vtt")
    assert r.exit_code == E.EXIT_OK, r.message
    assert r.transcript and r.transcript.strip()
    assert "<" not in r.transcript                 # 태그 strip 완전
    assert "elephants" in r.transcript             # 발췌 실물 내용 통과


# ── 대표군 ②: 긴 자동 롤링자막 → exit0·dedup·`<`0·페이징 계약 분리 ──

def test_long_auto_rolling_pipeline_and_paging(monkeypatch, tmp_path):
    fixture = FIX / "long_auto_rolling.en.vtt"
    r = _run(monkeypatch, tmp_path,
             _info(subs={}, autos={"en": [{}]}), "long_auto_rolling.en.vtt")
    assert r.exit_code == E.EXIT_OK, r.message
    t = r.transcript
    assert t and t.strip()

    # 구조 불변식: 태그 잔존 0 · 롤링 dedup(라인수 << 원본 큐수)
    assert "<" not in t and "align:" not in t
    raw_cues = fixture.read_text(encoding="utf-8").count("-->")
    assert len(t.splitlines()) < raw_cues          # 3배 그로스 큐가 접힘

    # 계약①: 기본 48KB 상한에서 1파트(실측 <48KB)
    assert P.split_transcript(t) == [t]

    # 계약②: 축소 limit 에서 무손실 다파트(라운드트립 == 원본)
    parts = P.split_transcript(t, 2048)
    assert len(parts) > 1
    assert "".join(parts) == t
    assert all(len(p.encode("utf-8")) <= 2048 for p in parts)
