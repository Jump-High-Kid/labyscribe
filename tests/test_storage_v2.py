"""storage v2 함수 계약 테스트 (결정적·네트워크 0).

CK-12 containment · CK-13 v1 무손상 증분 · CK-14 파이프라인 원자성 · CK-15 완료마커.
"""
import json
import os

import pytest

import storage


def _make_v1_published(root, vid="vidOK", tag="en"):
    """v1 완결 저장본(transcript.txt·raw/·meta.json, parts 없음) 디렉토리 생성."""
    d = os.path.join(root, vid, "%s-abc123" % tag)
    os.makedirs(os.path.join(d, "raw"))
    with open(os.path.join(d, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write("hello\nworld")
    with open(os.path.join(d, "raw", "%s.%s.vtt" % (vid, tag)), "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nhello")
    meta = {"id": vid, "lang": tag, "status": "ok", "transcript": "transcript.txt",
            "raw_vtt": "raw/%s.%s.vtt" % (vid, tag)}
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    return d, meta


# ── stage_v2_files (신규추출 staging) ──────────────────────

def test_stage_v2_files_writes_parts(tmp_path):
    temp = storage.make_temp(str(tmp_path))
    storage.stage_v2_files(temp, ("# part1", "# part2"), "combined")
    assert os.path.exists(os.path.join(temp, "parts", "part-01.md"))
    assert os.path.exists(os.path.join(temp, "parts", "part-02.md"))
    with open(os.path.join(temp, "transcript.md"), encoding="utf-8") as f:
        assert f.read() == "combined"


# ── read_v2_parts (완료 마커·CK-15) ────────────────────────

def test_read_v2_parts_complete(tmp_path):
    d, meta = _make_v1_published(str(tmp_path))
    os.makedirs(os.path.join(d, "parts"))
    with open(os.path.join(d, "parts", "part-01.md"), "w", encoding="utf-8") as f:
        f.write("md-body")
    meta["parts"] = [{"part_no": 1, "chapter_no": 0, "title": None, "bytes": 7}]
    records = storage.read_v2_parts(d, meta)
    assert records is not None and len(records) == 1
    assert records[0]["markdown"] == "md-body"
    assert records[0]["part_no"] == 1


def test_read_v2_parts_no_field_is_none(tmp_path):
    # meta 에 parts 필드 없음(v1 캐시) → None(미완결·재생성 트리거)
    d, meta = _make_v1_published(str(tmp_path))
    assert storage.read_v2_parts(d, meta) is None


def test_read_v2_parts_missing_file_is_none(tmp_path):
    # meta.parts 는 있으나 파일 부재(중간종료) → None (완료 마커 미충족·CK-15)
    d, meta = _make_v1_published(str(tmp_path))
    meta["parts"] = [{"part_no": 1, "chapter_no": 0, "title": None, "bytes": 7}]
    assert storage.read_v2_parts(d, meta) is None


# ── add_v2_artifacts (증분·CK-12·13·14) ────────────────────

def test_add_v2_artifacts_preserves_v1(tmp_path):
    root = str(tmp_path)
    d, meta = _make_v1_published(root)
    before_txt = open(os.path.join(d, "transcript.txt"), encoding="utf-8").read()
    before_raw = open(os.path.join(d, "raw", "vidOK.en.vtt"), encoding="utf-8").read()

    merged = dict(meta)
    merged["parts"] = [{"part_no": 1, "chapter_no": 0, "title": None, "bytes": 5}]
    merged["transcript_md"] = "transcript.md"
    storage.add_v2_artifacts(d, ("hello",), "hello-combined", merged, root)

    # v1 파일 무손상(CK-13)
    assert open(os.path.join(d, "transcript.txt"), encoding="utf-8").read() == before_txt
    assert open(os.path.join(d, "raw", "vidOK.en.vtt"), encoding="utf-8").read() == before_raw
    # .md 증분 생성
    assert os.path.exists(os.path.join(d, "transcript.md"))
    assert os.path.exists(os.path.join(d, "parts", "part-01.md"))
    # meta append-merge — parts 필드 + v1 핵심필드 보존
    m2 = json.load(open(os.path.join(d, "meta.json"), encoding="utf-8"))
    assert m2["parts"] and m2["id"] == "vidOK" and m2["status"] == "ok"
    # 완결 판정(read_v2_parts) 통과
    assert storage.read_v2_parts(d, m2) is not None
    # 스테이징 잔여 0
    assert not [n for n in os.listdir(d) if n.startswith(".v2tmp")]


def test_add_v2_artifacts_containment_rejects_outside(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(root)
    outside = str(tmp_path / "outside")
    os.makedirs(outside)
    with pytest.raises(OSError):
        storage.add_v2_artifacts(outside, ("x",), "x", {"parts": []}, root)


def test_add_v2_artifacts_reruns_idempotent(tmp_path):
    # 재실행(기존 parts/ 존재) → 스왑 후에도 완결
    root = str(tmp_path)
    d, meta = _make_v1_published(root)
    merged = dict(meta)
    merged["parts"] = [{"part_no": 1, "chapter_no": 0, "title": None, "bytes": 3}]
    storage.add_v2_artifacts(d, ("aaa",), "c1", merged, root)
    storage.add_v2_artifacts(d, ("bbb",), "c2", merged, root)   # 재실행
    with open(os.path.join(d, "parts", "part-01.md"), encoding="utf-8") as f:
        assert f.read() == "bbb"                                 # 최신으로 스왑
