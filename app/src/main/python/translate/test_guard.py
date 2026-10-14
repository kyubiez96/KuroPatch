"""Tests for the string guard."""
import unittest

from guard import should_skip, extract_placeholders


class TestShouldSkip(unittest.TestCase):
    def test_dimensions_skipped(self):
        for t in ["1080x1920", "1920x1080", "1920 x 1080", "720p", "1080P", "4K"]:
            skip, reason = should_skip(t)
            self.assertTrue(skip, t)
            self.assertEqual(reason, "screen-dimension")

    def test_urls_skipped(self):
        skip, _ = should_skip("https://example.com/path")
        self.assertTrue(skip)
        skip, _ = should_skip("Visit www.example.com now")
        self.assertTrue(skip)

    def test_versions_skipped(self):
        for t in ["v1.2.3", "1.0.0", "2.4.1-beta"]:
            skip, reason = should_skip(t)
            self.assertTrue(skip, t)
            self.assertEqual(reason, "version")

    def test_paths_skipped(self):
        skip, _ = should_skip("/sdcard/Android/data/game")
        self.assertTrue(skip)
        skip, _ = should_skip("assets.apk")
        self.assertTrue(skip)

    def test_numbers_skipped(self):
        skip, _ = should_skip("12345")
        self.assertTrue(skip)
        skip, _ = should_skip("1,000")
        self.assertTrue(skip)

    def test_hex_letter_words_not_numbers(self):
        # Real words made only of hex letters must NOT be skipped as
        # "numbers" (regression: DEAD/CAFE/FACE were skipped).
        for t in ["DEAD", "CAFE", "FACE", "DECAF", "FACADE", "BEEF",
                  "FEED", "DEFACE", "Dead", "Cafe"]:
            skip, reason = should_skip(t)
            self.assertFalse(skip, t)
            self.assertEqual(reason, "", t)
        # ...while real numbers (incl. hex with a digit) still skip.
        for t in ["123", "1,000", "0x1F", "A1", "FF00"]:
            skip, _ = should_skip(t)
            self.assertTrue(skip, t)

    def test_tags_only_skipped(self):
        skip, _ = should_skip("<b></b>")
        self.assertTrue(skip)

    def test_empty_skipped(self):
        skip, _ = should_skip("   ")
        self.assertTrue(skip)
        skip, _ = should_skip(None)
        self.assertTrue(skip)

    def test_identifiers_skipped(self):
        # single tokens that look like keys / class names, not UI text
        for t in ["menu_start", "com.game.Main", "level-1", "btn.ok"]:
            skip, reason = should_skip(t)
            self.assertTrue(skip, t)
            self.assertEqual(reason, "identifier")
        # a single capitalised word IS a UI label
        skip, _ = should_skip("Quit")
        self.assertFalse(skip)

    def test_language_codes_skipped(self):
        # Bare ISO codes are language tags, never UI text -- in ANY case.
        for t in ["EN", "ES", "DE", "en", "es", "ENG", "IT", "FR"]:
            skip, reason = should_skip(t)
            self.assertTrue(skip, t)
            self.assertEqual(reason, "language-code", t)
        # "No" is deliberately NOT treated as the Norwegian code: a bare
        # "No" is far more often the Yes/No dialog answer.
        skip, _ = should_skip("No")
        self.assertFalse(skip)
        skip, _ = should_skip("NO")
        self.assertFalse(skip)
        # Non-codes still pass through.
        skip, _ = should_skip("OK")
        self.assertFalse(skip)
        skip, _ = should_skip("Quit")
        self.assertFalse(skip)
        skip, reason = should_skip("quit")
        self.assertTrue(skip)
        self.assertEqual(reason, "lower-token")

    def test_no_word_skipped(self):
        skip, reason = should_skip("100%")
        self.assertTrue(skip)
        self.assertEqual(reason, "no-word")

    def test_slash_ui_labels_not_paths(self):
        # "Word/Word" UI labels must reach the translator -- the old
        # path regex swallowed them as file paths.
        for t in ["Save/Load", "Yes/No", "On/Off", "Win/Lose",
                  "Attack/Defend", "Start/Stop"]:
            skip, reason = should_skip(t)
            self.assertFalse(skip, t)
            self.assertEqual(reason, "", t)
        # Real paths / identifiers still skip (single-slash lowercase
        # paths fall through to the identifier rule).
        for t, reason in [("res/drawable", "identifier"),
                          ("images/logo", "identifier"),
                          ("com/game/Main", "file-path"),
                          ("a/b/c", "file-path"),
                          ("a/b", "identifier"),
                          ("menu_start", "identifier")]:
            skip, got = should_skip(t)
            self.assertTrue(skip, t)
            self.assertEqual(got, reason, t)


class TestExtractPlaceholders(unittest.TestCase):
    def test_real_text_not_skipped(self):
        skip, _ = should_skip("Welcome to the arena!")
        self.assertFalse(skip)
        # text WITH placeholders is translatable (guarded later by validator)
        skip, _ = should_skip("You earned %d gold")
        self.assertFalse(skip)
        skip, _ = should_skip("Hello {name}, welcome!")
        self.assertFalse(skip)


class TestExtractPlaceholders(unittest.TestCase):
    def test_printf(self):
        self.assertEqual(extract_placeholders("Score: %d"), ["%d"])
        self.assertEqual(extract_placeholders("%1$s killed %2$d"),
                         ["%1$s", "%2$d"])

    def test_printf_flags_width_precision(self):
        # Haiku audit: the old narrow class was blind to these -- a
        # translation dropping "%05.2f" passed validation and the
        # patched game crashed in String.format().
        self.assertEqual(extract_placeholders("Score: %05.2f"), ["%05.2f"])
        self.assertEqual(extract_placeholders("Pad: %-10s!"), ["%-10s"])
        self.assertEqual(extract_placeholders("Signed: %+d"), ["%+d"])
        self.assertEqual(extract_placeholders("Grouped: %,d"), ["%,d"])
        self.assertEqual(extract_placeholders("Space: % d"), ["% d"])

    def test_printf_all_conversions(self):
        # %b %B %h %H %t %T were missing from the old class.
        for ph in ["%b", "%B", "%h", "%H", "%tY", "%TY", "%c", "%%"]:
            self.assertEqual(extract_placeholders(f"X {ph} Y"), [ph])

    def test_gameloft_codes(self):
        # v1 masked these (protect_segments); v2 has no masking, so
        # validation must at least SEE them.
        self.assertEqual(extract_placeholders("A\\p1 B"), ["\\p1"])
        self.assertEqual(extract_placeholders("A\\v600 B"), ["\\v600"])
        self.assertEqual(extract_placeholders("A\\m12\\m B"), ["\\m12\\m"])
        self.assertEqual(extract_placeholders("A\\f34\\f B"), ["\\f34\\f"])
        # Lone "]" is ordinary punctuation, NOT a placeholder (see
        # _FORMAT_RE note): a translation adding brackets must pass.
        self.assertEqual(extract_placeholders("A]B"), [])
        self.assertEqual(extract_placeholders("A\\x B"), ["\\x"])
        self.assertEqual(extract_placeholders("A\\d B"), ["\\d"])

    def test_escapes_and_tags(self):
        self.assertEqual(extract_placeholders("A\\u0041 B"), ["\\u0041"])
        self.assertEqual(extract_placeholders("A\\n B"), ["\\n"])
        self.assertEqual(extract_placeholders("<b>Hello</b>"),
                         ["<b>", "</b>"])
        # stray "<" is not a tag
        self.assertEqual(extract_placeholders("a < b"), [])

    def test_braces(self):
        self.assertEqual(extract_placeholders("Hi {name}!"), ["{name}"])
        self.assertEqual(extract_placeholders("Item {0} of {1}"), ["{0}", "{1}"])

    def test_none(self):
        self.assertEqual(extract_placeholders("plain text"), [])

    def test_non_string(self):
        self.assertEqual(extract_placeholders(None), [])
        self.assertEqual(extract_placeholders(12345), [])

    def test_non_string_skipped(self):
        skip, reason = should_skip(12345)
        self.assertTrue(skip)
        self.assertEqual(reason, "non-string")


if __name__ == "__main__":
    unittest.main()
