"""Tests for glossary and cache."""
import os
import tempfile
import unittest

from cache import TranslationCache
from glossary import Glossary


class TestGlossary(unittest.TestCase):
    def test_default_terms(self):
        g = Glossary()
        self.assertTrue(g.is_only_terms("KuroPatch"))
        self.assertTrue(g.is_only_terms("  APK  "))
        self.assertFalse(g.is_only_terms("Welcome to KuroPatch!"))

    def test_add_remove(self):
        g = Glossary()
        g.add("DragonQuest")
        self.assertTrue(g.is_only_terms("DragonQuest"))
        g.remove("DragonQuest")
        self.assertFalse(g.is_only_terms("DragonQuest"))

    def test_protect_hint(self):
        g = Glossary()
        out = g.protect_hint("Patch your APK with KuroPatch")
        self.assertIn("APK", out)
        self.assertIn("do not translate", out.lower())

    def test_protect_hint_no_terms(self):
        g = Glossary()
        self.assertEqual(g.protect_hint("Hello world"), "Hello world")


class TestLanguageNames(unittest.TestCase):
    def test_live_bug_cases(self):
        g = Glossary()
        # Live Agnes mistranslations, 2026-10-14: Español->Jerman (wrong),
        # English->Ingris (typo). These must never reach the provider.
        self.assertEqual(g.lookup_language("Español"), "Spanyol")
        self.assertEqual(g.lookup_language("English"), "Inggris")
        self.assertEqual(g.lookup_language("Deutsch"), "Jerman")

    def test_case_and_whitespace_insensitive(self):
        g = Glossary()
        self.assertEqual(g.lookup_language("ENGLISH"), "Inggris")
        self.assertEqual(g.lookup_language("  español  "), "Spanyol")
        self.assertEqual(g.lookup_language("Français"), "Prancis")

    def test_native_and_english_names(self):
        g = Glossary()
        self.assertEqual(g.lookup_language("日本語"), "Jepang")
        self.assertEqual(g.lookup_language("Japanese"), "Jepang")
        self.assertEqual(g.lookup_language("中文"), "Mandarin")
        self.assertEqual(g.lookup_language("Русский"), "Rusia")
        self.assertEqual(g.lookup_language("한국어"), "Korea")

    def test_no_partial_matches(self):
        g = Glossary()
        # Sentences/fragments must fall through to the provider.
        self.assertIsNone(g.lookup_language("English version"))
        self.assertIsNone(g.lookup_language("Hello"))
        self.assertIsNone(g.lookup_language(""))
        self.assertIsNone(g.lookup_language("Inggris"))


class TestCache(unittest.TestCase):
    def test_put_get(self):
        with tempfile.TemporaryDirectory() as d:
            c = TranslationCache(os.path.join(d, "t.json"))
            self.assertIsNone(c.get("hi", "en", "id", "agnes"))
            c.put("hi", "en", "id", "agnes", "halo")
            self.assertEqual(c.get("hi", "en", "id", "agnes"), "halo")

    def test_key_includes_provider_and_langs(self):
        with tempfile.TemporaryDirectory() as d:
            c = TranslationCache(os.path.join(d, "t.json"))
            c.put("hi", "en", "id", "agnes", "halo")
            # different provider -> miss
            self.assertIsNone(c.get("hi", "en", "id", "other"))
            # different target -> miss
            self.assertIsNone(c.get("hi", "en", "ms", "agnes"))

    def test_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            c = TranslationCache(p)
            c.put("hi", "en", "id", "agnes", "halo")
            c.save()
            c2 = TranslationCache(p)
            self.assertEqual(c2.get("hi", "en", "id", "agnes"), "halo")

    def test_corrupt_file_recovers(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with open(p, "w") as fh:
                fh.write("not json{{{")
            c = TranslationCache(p)
            self.assertEqual(len(c), 0)


if __name__ == "__main__":
    unittest.main()


class TestFixedTranslations(unittest.TestCase):
    def test_day_month_ui_terms(self):
        from glossary import lookup_fixed
        # Same failure mode as language names: bare short strings the
        # LLM hallucinates on. Exact matches only, never the provider.
        self.assertEqual(lookup_fixed("Monday"), "Senin")
        self.assertEqual(lookup_fixed("January"), "Januari")
        self.assertEqual(lookup_fixed("Yes"), "Ya")
        self.assertEqual(lookup_fixed("Cancel"), "Batal")
        self.assertEqual(lookup_fixed("Game Over"), "Game Over")

    def test_case_and_whitespace_insensitive(self):
        from glossary import lookup_fixed
        self.assertEqual(lookup_fixed("  MONDAY  "), "Senin")
        self.assertEqual(lookup_fixed("february"), "Februari")

    def test_no_partial_matches(self):
        from glossary import lookup_fixed
        self.assertIsNone(lookup_fixed("Monday morning"))
        self.assertIsNone(lookup_fixed("Hello"))
        self.assertIsNone(lookup_fixed(""))
        self.assertIsNone(lookup_fixed("Yes please"))

    def test_no_overlap_with_language_names(self):
        # LANGUAGE_NAMES_ID is the single source of truth for language
        # names; this table must not duplicate it.
        from glossary import FIXED_TRANSLATIONS, LANGUAGE_NAMES_ID
        overlap = set(FIXED_TRANSLATIONS["id"]) & set(LANGUAGE_NAMES_ID)
        self.assertEqual(overlap, set())

    def test_unknown_target_lang(self):
        from glossary import lookup_fixed
        self.assertIsNone(lookup_fixed("Monday", target_lang="en"))
