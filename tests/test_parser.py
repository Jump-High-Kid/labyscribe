"""parse_vtt (WebVTT 파서) 테스트 — golden fixture + 합성 엣지.

실행: pytest   (repo 루트에서 · pythonpath=["."])
"""
import pathlib
import unittest
import extract as E

FIX = pathlib.Path(__file__).parent / "fixtures"


def _fix(name):
    return (FIX / name).read_text(encoding="utf-8")


def _vtt(*cues):
    """(start_sec, end_sec, body) → WEBVTT 문자열. body는 개행 포함 가능."""
    parts = []
    for s, e, body in cues:
        hdr = "%02d:%02d:%02d.000 --> %02d:%02d:%02d.000" % (
            s // 3600, s % 3600 // 60, s % 60,
            e // 3600, e % 3600 // 60, e % 60)
        parts.append(hdr + "\n" + body)
    return "WEBVTT\n\n" + "\n\n".join(parts) + "\n"


def _auto(*cues):
    """롤링(자동자막) 흉내 — 인라인 타이밍 태그를 붙여 줄 분리 경로를 탄다."""
    tagged = []
    for s, e, body in cues:
        lines = body.split("\n")
        # 마지막 줄에 인라인 타이밍 태그만 삽입(롤링 판정 트리거·strip되어 텍스트 불변)
        lines[-1] = lines[-1] + "<%02d:%02d:%02d.500>" % (s // 3600, s % 3600 // 60, s % 60)
        tagged.append((s, e, "\n".join(lines)))
    return _vtt(*tagged)


class TestParseVttGolden(unittest.TestCase):
    """실물 슬라이스 — 육안 검증 후 고정(AC-1)."""
    def test_rolling_slice_exact(self):
        self.assertEqual(E.parse_vtt(_fix("rolling.vtt")), _fix("rolling.expected.txt"))

    def test_static_exact(self):
        self.assertEqual(E.parse_vtt(_fix("static.vtt")), _fix("static.expected.txt"))

    def test_no_tag_residue(self):
        for name in ("rolling.vtt", "static.vtt"):
            out = E.parse_vtt(_fix(name))
            for bad in ("<", "-->", "align:", "position:"):
                self.assertNotIn(bad, out, "%s in %s" % (bad, name))


class TestStaticWrapMerge(unittest.TestCase):
    """정적 자막 큐 내 줄바꿈(래핑) 병합 — 태그 없음."""
    def test_wrapped_lines_merged(self):
        v = _vtt((1, 3, "in front of the\nelephants"))
        self.assertEqual(E.parse_vtt(v), "in front of the elephants")


class TestRollingDedup(unittest.TestCase):
    def test_exact_adjacent_collapsed(self):
        v = _vtt((1, 2, "hello"), (2, 3, "hello"), (3, 4, "hello"))
        self.assertEqual(E.parse_vtt(v), "hello")

    def test_prefix_growth_near_replaced(self):
        v = _auto((1, 3, "so today"), (3, 5, "so today we will"),
                  (5, 7, "so today we will talk"))
        self.assertEqual(E.parse_vtt(v), "so today we will talk")

    def test_distinct_lines_preserved(self):
        v = _vtt((1, 2, "the cat"), (2, 3, "the dog"))
        self.assertEqual(E.parse_vtt(v), "the cat\nthe dog")

    def test_normal_word_repeat_preserved(self):
        # "really really" 같은 정상 반복은 한 줄이라 보존
        v = _vtt((1, 2, "really really long trunks"))
        self.assertEqual(E.parse_vtt(v), "really really long trunks")


class TestOverDeletion(unittest.TestCase):
    """과삭제 방지(codex HIGH-1·AC-2) — 원거리·시간 떨어진 접두 보존."""
    def test_distant_exact_repeat_preserved(self):
        v = _vtt((1, 2, "yes"), (2, 3, "moving on"), (3, 4, "yes"))
        self.assertEqual(E.parse_vtt(v).split("\n").count("yes"), 2)

    def test_distant_prefix_preserved(self):
        # "go"(1s) ... "go home"(100s) → 시간 멀어 접두 dedup 안 됨(둘 다 보존)
        v = _vtt((1, 2, "go"), (2, 3, "other line here"), (100, 101, "go home"))
        lines = E.parse_vtt(v).split("\n")
        self.assertIn("go", lines)
        self.assertIn("go home", lines)

    def test_near_prefix_still_collapses(self):
        # 시간 인접 접두는 정상 롤링 성장으로 대체
        v = _auto((1, 2, "go"), (2, 3, "go home"))
        self.assertEqual(E.parse_vtt(v), "go home")

    def test_distant_direct_exact_repeat_preserved(self):
        # 긴 침묵 뒤 같은 후렴 직접 반복 → 시간 멀어 보존 (codex HIGH-1/P1-1)
        v = _vtt((1, 2, "chorus"), (60, 61, "chorus"))
        self.assertEqual(E.parse_vtt(v).split("\n").count("chorus"), 2)

    def test_reverse_order_preserves_both(self):
        # 큰 역행 타임스탬프(원거리 음수 gap) → 롤링 오인 삭제 안 함 (codex HIGH-2/P2-1)
        v = _vtt((100, 200, "go home"), (1, 2, "go"))
        out = E.parse_vtt(v).split("\n")
        self.assertIn("go home", out)
        self.assertIn("go", out)

    def test_overlapping_rolling_collapsed(self):
        # 겹치는 롤링 큐(start < 이전 end·작은 음수 gap)도 접힘 (codex 재감사 HIGH-2)
        v = _auto((1, 5, "hello"), (3, 7, "hello world"))   # start=3 < prev_end=5
        self.assertEqual(E.parse_vtt(v), "hello world")

    def test_gap_boundary(self):
        # gap == GAP(5s) → 접두 dedup 적용 / gap > GAP → 보존
        self.assertEqual(E.parse_vtt(_vtt((1, 2, "hello"), (7, 8, "hello there"))),
                         "hello there")                          # gap=5 → 대체
        self.assertEqual(E.parse_vtt(_vtt((1, 2, "hello"), (8, 9, "hello there"))),
                         "hello\nhello there")                   # gap=6 → 보존


class TestCorruptInput(unittest.TestCase):
    """손상 입력 크래시0(codex M3/M4·AC-3) — 부분 복구·raise 없음."""
    def test_note_block_skipped(self):
        v = "WEBVTT\n\nNOTE just a note\n\n00:00:01.000 --> 00:00:02.000\nhi\n"
        self.assertEqual(E.parse_vtt(v), "hi")

    def test_empty_cue_skipped(self):
        v = ("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n \n\n"
             "00:00:02.000 --> 00:00:03.000\nreal\n")
        self.assertEqual(E.parse_vtt(v), "real")

    def test_start_after_end_skipped(self):
        v = ("WEBVTT\n\n00:00:05.000 --> 00:00:02.000\nbad\n\n"
             "00:00:06.000 --> 00:00:07.000\ngood\n")
        self.assertEqual(E.parse_vtt(v), "good")

    def test_malformed_timing_skipped(self):
        v = ("WEBVTT\n\n00:00:01.000 --> broken\ndropme\n\n"
             "00:00:02.000 --> 00:00:03.000\nkeep\n")
        self.assertEqual(E.parse_vtt(v), "keep")

    def test_backward_timestamp_no_crash(self):
        v = _vtt((100, 101, "later"), (1, 2, "earlier"))
        out = E.parse_vtt(v)
        self.assertIn("later", out)
        self.assertIn("earlier", out)

    def test_empty_and_garbage(self):
        self.assertEqual(E.parse_vtt(""), "")
        self.assertEqual(E.parse_vtt("WEBVTT\n"), "")
        self.assertEqual(E.parse_vtt("total garbage no cues here"), "")


class TestMarkerAndUnicode(unittest.TestCase):
    def test_marker_at_600s(self):
        v = _vtt((1, 2, "before"), (601, 602, "after"))
        self.assertEqual(E.parse_vtt(v), "before\n[00:10:00]\nafter")

    def test_multiple_markers(self):
        v = _vtt((1, 2, "a"), (1201, 1202, "b"))  # 20분 뒤 → 마커 2개
        self.assertEqual(E.parse_vtt(v), "a\n[00:10:00]\n[00:20:00]\nb")

    def test_korean_preserved(self):
        v = _vtt((1, 2, "안녕하세요 여러분"))
        self.assertEqual(E.parse_vtt(v), "안녕하세요 여러분")

    def test_mmss_timestamp(self):
        # 시 생략 MM:SS.mmm 형식도 파싱(WebVTT 유효·codex 3회차 견고성)
        v = "WEBVTT\n\n01:30.000 --> 01:33.000\nhello there\n"
        self.assertEqual(E.parse_vtt(v), "hello there")


class TestSpeechText(unittest.TestCase):
    """음향 이벤트/마커 제외 실질 발화 판정(codex HIGH-3·P1-2·CK-25)."""
    def test_music_only_no_speech(self):
        # [Music]/[Applause]만 → 실질 발화 0 → quality 미달(silent-failure 차단)
        v = _vtt((1, 2, "[Music]"), (50, 51, "[Applause]"), (120, 121, "[Music]"))
        self.assertFalse(E.quality_ok(E._speech_text(E.parse_vtt(v))))

    def test_marker_lines_excluded(self):
        # 10분 마커 문자열이 quality 문자수를 부풀리지 않음
        v = _vtt((1, 2, "hi"), (1801, 1802, "yo"))   # 마커 3개 삽입
        transcript = E.parse_vtt(v)
        self.assertIn("[00:10:00]", transcript)       # 마커는 transcript에 존재
        self.assertEqual(E._speech_text(transcript), "hi\nyo")  # 판정 대상엔 없음

    def test_speech_with_events_passes(self):
        v = _vtt((1, 2, "[Music]"),
                 (3, 5, "welcome everyone to the show today thanks for coming"))
        self.assertTrue(E.quality_ok(E._speech_text(E.parse_vtt(v))))


if __name__ == "__main__":
    unittest.main()
