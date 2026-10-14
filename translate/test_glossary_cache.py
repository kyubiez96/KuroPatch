"""Tests for glossary and cache."""
import json
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

    def test_region_suffix(self):
        g = Glossary()
        # "Name (Region)": base translated, region kept verbatim.
        self.assertEqual(g.lookup_language("English (US)"), "Inggris (US)")
        self.assertEqual(g.lookup_language("Español (México)"),
                         "Spanyol (México)")
        self.assertEqual(g.lookup_language("Português (Brasil)"),
                         "Portugis (Brasil)")
        self.assertEqual(g.lookup_language("ENGLISH (UK)"), "Inggris (UK)")
        self.assertEqual(g.lookup_language("Deutsch (DE)"), "Jerman (DE)")

    def test_new_autonyms(self):
        # Final audit round 1: these autonyms were missing -- a game
        # offering Serbian / Slovak / Kurdish / Malay-English would
        # have had its language name translated (same brick as
        # Castlevania). "Slowakia" matches the glossary's existing
        # Indonesian spelling (cf. "slovak" -> "Slowakia").
        g = Glossary()
        self.assertEqual(g.lookup_language("Srpski"), "Serbia")
        self.assertEqual(g.lookup_language("Slovenčina"), "Slowakia")
        self.assertEqual(g.lookup_language("Kurdî"), "Kurdi")
        self.assertEqual(g.lookup_language("Kurdi"), "Kurdi")
        self.assertEqual(g.lookup_language("Bahasa Inggeris"), "Inggris")

    def test_region_suffix_rejects(self):
        g = Glossary()
        self.assertIsNone(g.lookup_language("English ()"))  # empty region
        self.assertIsNone(g.lookup_language("English (US) (extra)"))
        self.assertIsNone(g.lookup_language("Hello (World)"))  # unknown base
        self.assertIsNone(g.lookup_language("(US) English"))


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

    def test_non_dict_json_recovers(self):
        # Haiku audit: a cache file containing a JSON list/string
        # crashed .get() with AttributeError (no .get on list).
        for payload in ['["not", "a", "dict"]', '"just a string"', "42"]:
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "t.json")
                with open(p, "w") as fh:
                    fh.write(payload)
                c = TranslationCache(p)
                self.assertEqual(len(c), 0)
                self.assertIsNone(c.get("hi", "en", "id", "agnes"))

    def test_non_string_value_is_miss(self):
        # Round-2 audit: a non-string value under a valid key (corrupt /
        # hand-edited cache) used to come back as a "cached" translation
        # -- garbage patched into the game (or a TypeError in patch).
        # It must be treated as a miss.
        from cache import _key
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with open(p, "w") as fh:
                json.dump({
                    _key("Hello", "en", "id", "agnes"): ["not", "a", "string"],
                    _key("World", "en", "id", "agnes"): 42,
                    _key("Good", "en", "id", "agnes"): "Bagus",
                }, fh)
            c = TranslationCache(p)
            self.assertIsNone(c.get("Hello", "en", "id", "agnes"))
            self.assertIsNone(c.get("World", "en", "id", "agnes"))
            self.assertEqual(c.get("Good", "en", "id", "agnes"), "Bagus")

    def test_non_utf8_file_recovers(self):
        # Final audit round 1: a cache file with non-UTF-8 bytes
        # crashed TranslationCache.__init__ with a raw
        # UnicodeDecodeError (only JSONDecodeError was caught).
        # Cache is best-effort -- treat it like a missing cache.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            with open(p, "wb") as fh:
                fh.write(b"\xff\xfe\x00binary garbage")
            c = TranslationCache(p)  # must not raise
            self.assertEqual(len(c), 0)
            self.assertIsNone(c.get("hi", "en", "id", "agnes"))

    def test_cache_path_is_directory_save_does_not_crash(self):
        # Round-3 audit: a directory sitting at the cache path made
        # save() raise IsADirectoryError (os.replace file onto dir).
        # Cache is best-effort -- the write is dropped, translation
        # must not crash.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cache-dir")
            os.makedirs(p)
            c = TranslationCache(p)
            c.put("hi", "en", "id", "agnes", "halo")
            c.save()  # must not raise
            self.assertEqual(c.get("hi", "en", "id", "agnes"), "halo")

    def test_save_with_unserializable_value_does_not_crash(self):
        # Final audit round 2: save() only caught OSError, so a
        # poisoned in-memory value (non-JSON-serializable / circular)
        # crashed the whole batch with TypeError / ValueError at save
        # time -- AFTER all translations were done. Cache is
        # best-effort: the write is dropped, the on-disk file stays
        # intact, translation must not crash.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.json")
            c = TranslationCache(p)
            c.put("good", "en", "id", "agnes", "bagus")
            c.save()
            c.put("bad", "en", "id", "agnes", object())  # not serializable
            c.save()  # must not raise
            c2 = TranslationCache(p)
            self.assertEqual(c2.get("good", "en", "id", "agnes"), "bagus")
            circular = {}
            circular["self"] = circular
            c.put("circ", "en", "id", "agnes", circular)
            c.save()  # must not raise (ValueError: circular reference)
            c3 = TranslationCache(p)
            self.assertEqual(c3.get("good", "en", "id", "agnes"), "bagus")


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


if __name__ == "__main__":
    unittest.main()
