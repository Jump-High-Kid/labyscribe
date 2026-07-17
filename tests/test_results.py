"""results.py 레지스트리 계약 테스트 (순수·네트워크 0).

result_id/capability_id 불투명·미존재 None·LRU 축출·video_meta allowlist 투영·
capability root 내부전용(외부 display_name 만).
"""
import results


def test_result_issue_get_roundtrip():
    reg = results.ResultRegistry()
    parts = ({"part_no": 1, "chapter_no": 0, "title": None, "markdown": "m", "bytes": 1},)
    rid = reg.issue("T", parts, "PROMPT", {"title": "T", "uploader": "U"})
    e = reg.get(rid)
    assert e is not None and e.title == "T" and e.parts == parts
    assert e.summary_prompt == "PROMPT"


def test_result_missing_id_none():
    reg = results.ResultRegistry()
    assert reg.get("nope") is None
    assert reg.get(None) is None


def test_result_video_meta_allowlist_projection():
    reg = results.ResultRegistry()
    rid = reg.issue("T", (), "P", {"title": "T", "secret_path": "/abs/x", "cookies": "c"})
    e = reg.get(rid)
    assert "secret_path" not in e.video_meta and "cookies" not in e.video_meta
    assert e.video_meta["title"] == "T"


def test_result_lru_eviction():
    reg = results.ResultRegistry(max_results=2)
    a = reg.issue("A", (), "", {})
    b = reg.issue("B", (), "", {})
    c = reg.issue("C", (), "", {})       # a 축출
    assert reg.get(a) is None
    assert reg.get(b) is not None and reg.get(c) is not None


def test_result_id_unpredictable_unique():
    reg = results.ResultRegistry()
    ids = {reg.issue("t", (), "", {}) for _ in range(20)}
    assert len(ids) == 20                # 충돌 0
    assert all(len(i) > 20 for i in ids)  # 난수 길이


def test_capability_register_hides_path():
    reg = results.CapabilityRegistry()
    e = reg.register("/Users/me/Obsidian/Vault")
    assert e.display_name == "Vault"      # basename 만 표시
    assert e.root == "/Users/me/Obsidian/Vault"   # 내부엔 절대경로 보존
    assert reg.get(e.capability_id).root == e.root


def test_capability_missing_none():
    reg = results.CapabilityRegistry()
    assert reg.get("nope") is None
