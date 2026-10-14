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

    def test_braces(self):
        self.assertEqual(extract_placeholders("Hi {name}!"), ["{name}"])
        self.assertEqual(extract_placeholders("Item {0} of {1}"), ["{0}", "{1}"])

    def test_none(self):
        self.assertEqual(extract_placeholders("plain text"), [])


if __name__ == "__main__":
    unittest.main()
