"""chapters.split / _assign_cues 계약 테스트 (결정적·네트워크 0).

CK-1 무손실·무중복 귀속(반개구간 [start,end)) · CK-2 정규화·raise 0 ·
CK-3 챕터無 폴백 · CK-4 과대 챕터 재분할·식별자 분리 · CK-5 body 상한 · CK-19 순수층.
"""
import chapters
from chapters import HEADER_RESERVE_BYTES, Part, split


def _cue_texts(cues):
    return [t for _, t in cues]


def _seg_texts(segs):
    """세그먼트들의 cue 텍스트를 순서 보존 flatten."""
    out = []
    for seg in segs:
        out.extend(t for _, t in seg.cues)
    return out


# ── CK-1 무손실·무중복 귀속 ────────────────────────────────

def test_all_cues_assigned_exactly_once():
    # Arrange
    cues = [(0.0, "a"), (10.0, "b"), (650.0, "c"), (1300.0, "d")]
    meta = [{"start_time": 0, "title": "Intro"},
            {"start_time": 600, "title": "Body"},
            {"start_time": 1200, "title": "End"}]
    # Act
    segs = chapters._assign_cues(cues, meta)
    # Assert — 모든 cue 텍스트가 정확히 한 번, 순서 보존(무손실·무중복)
    assert _seg_texts(segs) == _cue_texts(cues)


def test_half_open_interval_boundary():
    # cue.start == 챕터 경계 → 그 경계의 챕터(뒤)에 귀속 (반개구간 [start,end))
    cues = [(600.0, "x")]
    meta = [{"start_time": 0, "title": "A"}, {"start_time": 600, "title": "B"}]
    segs = chapters._assign_cues(cues, meta)
    owning = [s for s in segs if s.cues]
    assert len(owning) == 1
    assert owning[0].title == "B"


def test_cue_before_first_chapter_absorbed():
    # 첫 챕터 start 이전 cue → seg 0(최근접·직전) 흡수
    cues = [(0.0, "pre"), (700.0, "in")]
    meta = [{"start_time": 500, "title": "Only"}]
    segs = chapters._assign_cues(cues, meta)
    assert _seg_texts(segs) == ["pre", "in"]  # 무손실


# ── CK-2 정규화·raise 0 ────────────────────────────────────

def test_overlapping_reversed_chapters_normalized():
    # 역순·중첩 start → 정규화 후에도 모든 cue 귀속(무손실·무중복)
    cues = [(0.0, "a"), (300.0, "b"), (900.0, "c")]
    meta = [{"start_time": 600, "title": "Late"},
            {"start_time": 0, "title": "Early"},
            {"start_time": 600, "title": "Dup"}]  # 역순 + 중복 start
    segs = chapters._assign_cues(cues, meta)
    assert _seg_texts(segs) == ["a", "b", "c"]


def test_corrupt_meta_no_raise():
    # 손상 메타(숫자 아닌 start·필드 결손) → raise 0, 폴백 귀속
    cues = [(0.0, "a"), (10.0, "b")]
    for bad in (
        [{"start_time": "NaN", "title": "x"}],
        [{"title": "no start"}],
        [{"start_time": None, "title": None}],
        "garbage",
        123,
    ):
        segs = chapters._assign_cues(cues, bad)  # must not raise
        assert _seg_texts(segs) == ["a", "b"]


def test_empty_inputs():
    assert split((), None) == ()
    assert split([], []) == ()
    segs = chapters._assign_cues([], [{"start_time": 0, "title": "x"}])
    assert _seg_texts(segs) == []


# ── CK-3 챕터 無 폴백 ──────────────────────────────────────

def test_no_chapters_fallback():
    cues = [(0.0, "a"), (10.0, "b")]
    for meta in (None, [], "garbage"):
        parts = split(cues, meta)
        assert len(parts) >= 1
        assert all(p.chapter_no == 0 for p in parts)   # 폴백 = 챕터 0
        assert all(p.title is None for p in parts)


# ── CK-4 과대 챕터 재분할·식별자 분리 ──────────────────────

def test_oversized_chapter_resplit_keeps_chapter_no():
    # 한 챕터가 상한 초과 → 내부 재분할, part_no 연속·chapter_no 동일
    big = "가" * 40000                    # 1 cue ~120KB(한글 3B) > body_budget
    cues = [(0.0, "intro"), (10.0, big), (700.0, "tail")]
    meta = [{"start_time": 0, "title": "Big"}, {"start_time": 600, "title": "Small"}]
    parts = split(cues, meta)
    # part_no 1..N 연속
    assert [p.part_no for p in parts] == list(range(1, len(parts) + 1))
    # "Big" 챕터가 여러 파트로 쪼개졌어도 chapter_no 동일
    big_parts = [p for p in parts if p.title == "Big"]
    assert len(big_parts) >= 2
    assert len({p.chapter_no for p in big_parts}) == 1


def test_part_no_contiguous_across_chapters():
    cues = [(0.0, "a"), (650.0, "b"), (1300.0, "c")]
    meta = [{"start_time": 0, "title": "A"},
            {"start_time": 600, "title": "B"},
            {"start_time": 1200, "title": "C"}]
    parts = split(cues, meta)
    assert [p.part_no for p in parts] == list(range(1, len(parts) + 1))


# ── CK-5 body 상한(렌더 헤더 예약 차감) ────────────────────

def test_body_within_budget():
    big = "나" * 40000
    cues = [(0.0, big), (5.0, "x" * 40000)]
    meta = None
    parts = split(cues, meta, byte_cap=48 * 1024)
    budget = 48 * 1024 - HEADER_RESERVE_BYTES
    for p in parts:
        assert len(p.body.encode("utf-8")) <= budget


# ── 마커 렌더: 세그먼트 시작 기준·비유한 가드(codex/reviewer 재작업) ──

def test_marker_starts_at_segment_boundary_no_repeat():
    # 20분(1200초~) 시작 챕터 → 파트에 이전 구간 마커([00:10:00]·[00:20:00]) 반복 없음
    cues = [(1210.0, "a"), (1900.0, "b")]      # 20:10, 31:40
    meta = [{"start_time": 1200, "title": "Late"}]
    body = split(cues, meta)[0].body
    assert "[00:10:00]" not in body            # 이전 구간 마커 미반복
    assert "[00:20:00]" not in body
    assert "[00:30:00]" in body                # 1900 ≥ 1800 → 세그먼트 내 다음 경계
    assert "a" in body and "b" in body         # cue 무손실


def test_non_finite_cue_no_infinite_loop():
    # inf/NaN cue start → 무한루프 없이 완료·raise 0·텍스트 보존
    cues = [(float("inf"), "x"), (float("nan"), "y"), (0.0, "z")]
    parts = split(cues, None)                  # 반환되면 무한루프 아님
    body = "\n".join(p.body for p in parts)
    assert "x" in body and "y" in body and "z" in body


def test_non_finite_chapter_start_skipped():
    cues = [(0.0, "a"), (700.0, "b")]
    meta = [{"start_time": float("inf"), "title": "bad"},
            {"start_time": float("nan"), "title": "worse"},
            {"start_time": 600, "title": "ok"}]
    parts = split(cues, meta)                   # raise 0
    assert parts and _cue_texts([(0.0, "a"), (700.0, "b")]) == ["a", "b"]


def test_byte_cap_below_reserve_no_crash():
    # byte_cap ≤ HEADER_RESERVE_BYTES → 음수 예산 방어(paging 무한루프/예외 0)
    cues = [(0.0, "가" * 5000)]
    parts = split(cues, None, byte_cap=100)     # reserve(2KB)보다 작음
    assert parts                                # 완료(raise·무한루프 0)


# ── CK-19 순수층: 타입·불변 ────────────────────────────────

def test_part_is_frozen():
    cues = [(0.0, "a")]
    p = split(cues, None)[0]
    assert isinstance(p, Part)
    try:
        p.part_no = 99          # frozen → raise
        raised = False
    except Exception:
        raised = True
    assert raised
