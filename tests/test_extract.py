"""extract.py 순수함수 단위 테스트 (stdlib unittest — pytest 네이티브 수집).

실행: pytest   (repo 루트에서 · pythonpath=["."] 로 extract 해결)
"""
import unittest
import extract as E


class TestValidateUrl(unittest.TestCase):
    def test_accepts_youtube_variants(self):
        for u in [
            "https://www.youtube.com/watch?v=abc123",
            "https://youtube.com/watch?v=abc",
            "https://m.youtube.com/watch?v=abc",
            "https://music.youtube.com/watch?v=abc",
            "https://youtu.be/abc123",
        ]:
            self.assertEqual(E.validate_url(u), u)

    def test_rejects_non_https(self):
        with self.assertRaises(ValueError):
            E.validate_url("http://www.youtube.com/watch?v=abc")

    def test_rejects_other_domains(self):
        for u in [
            "https://vimeo.com/123",
            "https://evil.com/youtube.com",   # host=evil.com (SSRF 차단)
            "https://localhost/watch",
            "https://169.254.169.254/latest",
            "file:///etc/passwd",
        ]:
            with self.assertRaises(ValueError):
                E.validate_url(u)


class TestDetectOrig(unittest.TestCase):
    def test_from_language_field(self):
        self.assertEqual(E.detect_orig_lang({"language": "en-US"}), "en")

    def test_from_orig_suffix(self):
        info = {"automatic_captions": {"en-orig": [{}], "ko": [{}]}}
        self.assertEqual(E.detect_orig_lang(info), "en")

    def test_from_manual_sub(self):
        self.assertEqual(E.detect_orig_lang({"subtitles": {"ja": [{}]}}), "ja")

    def test_none(self):
        self.assertIsNone(E.detect_orig_lang({}))


class TestSelectTrack(unittest.TestCase):
    def test_manual_original_preferred(self):
        info = {"language": "en-US",
                "subtitles": {"en": [{}]}, "automatic_captions": {"en": [{}], "ko": [{}]}}
        self.assertEqual(E.select_track(info, ["ko"]), ("en", False, False))

    def test_original_auto_beats_translated(self):
        # 핵심: 영어 원본 → ko 요청이어도 원어(en) 자동자막을 뽑고 Claude가 번역
        info = {"language": "en-US",
                "subtitles": {}, "automatic_captions": {"en": [{}], "ko": [{}]}}
        self.assertEqual(E.select_track(info, ["ko"]), ("en", True, False))

    def test_orig_suffix_tag_when_no_plain(self):
        info = {"language": "en-US",
                "subtitles": {}, "automatic_captions": {"en-orig": [{}], "ko": [{}]}}
        self.assertEqual(E.select_track(info, ["ko"]), ("en-orig", True, False))

    def test_korean_original_direct(self):
        info = {"language": "ko",
                "subtitles": {}, "automatic_captions": {"ko": [{}], "en": [{}]}}
        self.assertEqual(E.select_track(info, None), ("ko", True, False))

    def test_translated_fallback_when_no_orig(self):
        info = {"subtitles": {}, "automatic_captions": {"ko": [{}]}}
        self.assertEqual(E.select_track(info, ["ko"]), ("ko", True, False))

    def test_none_when_absent(self):
        info = {"subtitles": {}, "automatic_captions": {}}
        self.assertIsNone(E.select_track(info, ["ko"]))

    # ── 버그② 재현: _match_lang 요청언어 미정규화 (--lang en-US → en 트랙) ──
    def test_match_lang_normalizes_requested(self):
        # 요청 'en-US'가 'en' 트랙에 매칭돼야 (수정 전: None = 버그)
        self.assertEqual(E._match_lang({"en": [{}]}, "en-US"), "en")

    def test_match_lang_exact_precedence(self):
        # 정확일치가 정규화 폴백보다 우선
        self.assertEqual(E._match_lang({"en": [{}], "en-US": [{}]}, "en-US"), "en-US")

    def test_match_lang_reverse_still_works(self):
        # 기존 정상 경로(트랙 태그가 변종) 무회귀
        self.assertEqual(E._match_lang({"en-US": [{}]}, "en"), "en-US")

    # ── 버그① 불변식 회귀: orig 지역변종은 원어자동(③)으로 잡혀 translated=False ──
    def test_orig_regional_variant_not_flagged_translated(self):
        # en-US 자동자막(orig=en) → 동일언어이므로 번역본 오표시 안 됨
        info = {"language": "en", "subtitles": {},
                "automatic_captions": {"en-US": [{}], "ko": [{}]}}
        self.assertEqual(E.select_track(info, ["ko"]), ("en-US", True, False))


class TestCleanSrt(unittest.TestCase):
    def test_rolling_dedup_and_tag_strip_and_marker(self):
        srt = (
            "1\n00:00:01,000 --> 00:00:03,000\nso today\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\nso today we will\n\n"
            "3\n00:00:05,000 --> 00:00:07,000\nso today we will <c>talk</c>\n\n"
            "4\n00:10:02,000 --> 00:10:05,000\nnext section\n"
        )
        out = E.clean_srt(srt)
        self.assertEqual(out, "so today we will talk\n[00:10:00]\nnext section")

    def test_exact_adjacent_dup_collapsed(self):
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"
               "2\n00:00:02,000 --> 00:00:03,000\nhello\n")
        self.assertEqual(E.clean_srt(srt), "hello")

    def test_distinct_lines_preserved(self):
        srt = ("1\n00:00:01,000 --> 00:00:02,000\nthe cat\n\n"
               "2\n00:00:02,000 --> 00:00:03,000\nthe dog\n")
        self.assertEqual(E.clean_srt(srt), "the cat\nthe dog")


class TestRetryable(unittest.TestCase):
    """다운로드 실패 분류 — 일시오류(재시도) vs 진짜 없음."""
    def test_transient_errors_match(self):
        for e in ["HTTP Error 429: Too Many Requests",
                  "Connection reset by peer", "operation timed out"]:
            self.assertTrue(E._RETRYABLE.search(e), e)

    def test_genuine_no_sub_not_retryable(self):
        for e in ["There are no subtitles for the requested languages",
                  "Video unavailable"]:
            self.assertIsNone(E._RETRYABLE.search(e), e)


class TestQualityGate(unittest.TestCase):
    def test_ok_above_min(self):
        self.assertTrue(E.quality_ok("a" * 250))

    def test_short_valid_passes(self):
        # 짧은 정상 영상도 통과 (codex HIGH-3 — 길이 검열 아님)
        self.assertTrue(E.quality_ok("All right so here we are in front of the elephants"))

    def test_empty_or_noise_fails(self):
        self.assertFalse(E.quality_ok(""))
        self.assertFalse(E.quality_ok("   "))
        self.assertFalse(E.quality_ok("short"))


class TestSafeFilename(unittest.TestCase):
    def test_strips_disallowed_and_collapses(self):
        self.assertEqual(E.safe_filename("Hello: World/Test?"), "Hello World Test")

    def test_korean_preserved(self):
        self.assertEqual(E.safe_filename("한글 제목 (테스트)"), "한글 제목 테스트")

    def test_length_capped(self):
        self.assertLessEqual(len(E.safe_filename("a" * 200)), 100)


if __name__ == "__main__":
    unittest.main()
