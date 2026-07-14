"""paging.py 순수 단위 테스트 — 바이트 상한·무손실·UTF-8 무절단·마커 경계.

게이트: CK-28(바이트 상한·10분 마커·라인/문자 재분할·무손실·1-based)·CK-38(순수).
"""
import paging as P


def _bytes(s):
    return len(s.encode("utf-8"))


def _assert_invariants(transcript, parts, limit):
    # 무손실 재구성
    assert "".join(parts) == transcript
    # 각 파트 바이트 상한 이하
    for p in parts:
        assert _bytes(p) <= limit, "part %r = %d bytes > %d" % (p[:20], _bytes(p), limit)
    # 각 파트는 유효 UTF-8(코드포인트 무절단) — str 이므로 재인코딩 왕복으로 확인
    for p in parts:
        assert p.encode("utf-8").decode("utf-8") == p


# ── CK-28 패스트패스: 전량 이하 → 파트 1개 ──────────────────────

def test_fast_path_single_part():
    t = "짧은 transcript\n두 번째 줄"
    parts = P.split_transcript(t, limit_bytes=P.PART_LIMIT_BYTES)
    assert parts == [t]
    assert len("".join(parts).encode()) <= P.PART_LIMIT_BYTES


def test_default_limit_used():
    parts = P.split_transcript("hello world")
    assert parts == ["hello world"]


# ── CK-28 마커 경계 우선 분할 ──────────────────────────────────

def test_splits_on_marker_boundary_when_over_limit():
    # 두 개의 10분 구간, 각 구간이 상한에 가깝게 → 마커 경계에서 분할
    sec0 = "a" * 40 + "\n"
    sec1 = "[00:10:00]\n" + "b" * 40 + "\n"
    sec2 = "[00:20:00]\n" + "c" * 40
    t = sec0 + sec1 + sec2
    parts = P.split_transcript(t, limit_bytes=60)   # 한 섹션(~41B)만 파트에 맞음
    _assert_invariants(t, parts, 60)
    assert len(parts) >= 3
    # 각 파트는 마커로 시작하거나(구간) 선행 블록 — 마커가 파트 중간에서 시작되지 않음
    for p in parts[1:]:
        assert p.startswith("[")


def test_music_bracket_not_treated_as_marker():
    # [Music] 는 마커가 아니므로 별도 파트 경계를 만들지 않음(그리디로 함께 묶임 가능)
    t = "[Music]\n" + "x" * 30 + "\n[Applause]\n" + "y" * 30
    parts = P.split_transcript(t, limit_bytes=200)
    assert parts == [t]   # 전량이 상한 이하 → 한 파트(마커 없음)


# ── CK-28 단일 초과 세그먼트: 라인→문자 재분할 ──────────────────

def test_single_long_line_char_resplit():
    # 개행 없는 초장문 한 줄 → 문자 경계로 재분할
    t = "가나다라마바사아자차카타파하" * 100   # 한글(3B) × ~1400자
    parts = P.split_transcript(t, limit_bytes=100)
    _assert_invariants(t, parts, 100)
    assert len(parts) > 1


def test_korean_no_codepoint_split_at_tight_limit():
    # 상한을 한글 경계와 어긋나게(限 10B = 3자+1B) 잡아도 코드포인트 절단 없음
    t = "한국어자막테스트" * 20
    parts = P.split_transcript(t, limit_bytes=10)
    _assert_invariants(t, parts, 10)


# ── CK-28 무손실(라인 경계) ────────────────────────────────────

def test_lossless_multi_line_reconstruction():
    lines = ["line %d 내용 텍스트" % i for i in range(200)]
    t = "\n".join(lines)
    parts = P.split_transcript(t, limit_bytes=128)
    _assert_invariants(t, parts, 128)
    assert len(parts) > 1


def test_trailing_newline_preserved():
    t = "첫 줄\n둘째 줄\n"      # 끝 개행
    parts = P.split_transcript(t, limit_bytes=8)
    assert "".join(parts) == t   # 끝 개행까지 무손실


def test_marker_only_over_limit_still_lossless():
    # 마커가 많고 각 구간이 커서 여러 파트로 갈려도 무손실
    secs = ["[%02d:%02d:00]\n" % (i // 6, (i % 6) * 10) + ("말 " * 50) for i in range(5)]
    t = "\n".join(secs)
    parts = P.split_transcript(t, limit_bytes=120)
    _assert_invariants(t, parts, 120)
