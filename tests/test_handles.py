"""handles.py 순수 단위 테스트 — 핸들 결속·거부·축출·불변 투영 (SDK·네트워크 무의존).

게이트: CK-29(결속)·CK-30(거부·경로성분)·CK-38(순수·불변 투영).
"""
import dataclasses

import pytest

import handles as H


def _meta(**kw):
    base = {"title": "T", "uploader": "U", "duration": 10, "orig_lang": "en",
            # 경로키 — 투영에서 제외돼야 함(allowlist)
            "raw_vtt": "raw/x.vtt", "transcript": "transcript.txt",
            "raw_json3": "raw/x.json3", "url": "https://youtu.be/x"}
    base.update(kw)
    return base


# ── CK-29 결속: 발급 핸들로만·자기 엔트리만 ──────────────────────

def test_issue_returns_opaque_token_and_get_roundtrips():
    r = H.HandleRegistry()
    tok = r.issue("vid1", "en", H.content_hash("hello"), ("p1", "p2"), _meta())
    assert isinstance(tok, str) and len(tok) >= 32
    entry = r.get(tok)
    assert entry is not None
    assert entry.video_id == "vid1" and entry.lang == "en"
    assert entry.parts == ("p1", "p2")


def test_distinct_handles_do_not_cross_bind():
    # 다른 video/lang 로 발급된 핸들은 서로의 엔트리를 반환하지 않음(1:1 매핑)
    r = H.HandleRegistry()
    a = r.issue("vidA", "en", H.content_hash("a"), ("A1",), _meta(title="A"))
    b = r.issue("vidB", "ko", H.content_hash("b"), ("B1",), _meta(title="B"))
    assert a != b
    assert r.get(a).video_id == "vidA" and r.get(a).parts == ("A1",)
    assert r.get(b).video_id == "vidB" and r.get(b).parts == ("B1",)


# ── CK-30 거부: 미발급·조작·축출 → None ─────────────────────────

def test_unknown_or_tampered_token_returns_none():
    r = H.HandleRegistry()
    tok = r.issue("v", "en", H.content_hash("x"), ("p",), _meta())
    assert r.get("never-issued") is None
    assert r.get(tok + "tamper") is None
    assert r.get("../../etc/passwd") is None   # 경로성분 주입도 그냥 미존재 키
    assert r.get("") is None


def test_lru_eviction_makes_oldest_invalid():
    r = H.HandleRegistry(max_handles=2)
    t1 = r.issue("v1", "en", H.content_hash("1"), ("1",), _meta())
    t2 = r.issue("v2", "en", H.content_hash("2"), ("2",), _meta())
    t3 = r.issue("v3", "en", H.content_hash("3"), ("3",), _meta())   # t1 축출
    assert r.get(t1) is None          # 축출 → 거부
    assert r.get(t2) is not None
    assert r.get(t3) is not None


def test_get_refreshes_lru_recency():
    r = H.HandleRegistry(max_handles=2)
    t1 = r.issue("v1", "en", H.content_hash("1"), ("1",), _meta())
    t2 = r.issue("v2", "en", H.content_hash("2"), ("2",), _meta())
    assert r.get(t1) is not None      # t1 을 최근으로 갱신
    t3 = r.issue("v3", "en", H.content_hash("3"), ("3",), _meta())   # 이제 t2 가 최오래
    assert r.get(t2) is None
    assert r.get(t1) is not None and r.get(t3) is not None


# ── CK-38 불변 투영·순수 ────────────────────────────────────────

def test_meta_projected_to_allowlist_only():
    r = H.HandleRegistry()
    tok = r.issue("v", "en", H.content_hash("x"), ("p",), _meta())
    m = r.get(tok).meta
    assert set(m) == set(H._META_ALLOWLIST)
    for pathkey in ("raw_vtt", "transcript", "raw_json3", "url"):
        assert pathkey not in m       # 경로키 미노출


def test_mutating_source_meta_does_not_leak_into_entry():
    r = H.HandleRegistry()
    src = _meta(title="orig")
    tok = r.issue("v", "en", H.content_hash("x"), ("p",), src)
    src["title"] = "MUTATED"          # 발급 후 원본 변경
    src["raw_vtt"] = "hacked"
    assert r.get(tok).meta["title"] == "orig"   # 엔트리는 불변


def test_handle_entry_is_frozen():
    e = H.HandleEntry("v", "en", "h", ("p",), {"title": "T"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.video_id = "other"


def test_content_hash_deterministic_and_hex():
    h1 = H.content_hash("동일 텍스트 hello")
    h2 = H.content_hash("동일 텍스트 hello")
    assert h1 == h2 and len(h1) == 64
    assert h1 != H.content_hash("다른 텍스트")
