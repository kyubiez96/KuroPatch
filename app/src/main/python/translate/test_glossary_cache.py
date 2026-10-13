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
