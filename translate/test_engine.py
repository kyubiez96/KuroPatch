"""Tests for the translate engine (mocked providers, no network)."""
import os
import tempfile
import unittest

from cache import TranslationCache
from engine import translate_strings, results_to_dict
from glossary import Glossary
from providers import BaseProvider, PassthroughProvider


class MockProvider(BaseProvider):
    """Deterministic fake: wraps text, keeps placeholders intact."""
    name = "mock"

    def __init__(self, translations=None, fail_on=()):
        self.translations = translations or {}
        self.fail_on = set(fail_on)
        self.calls = []

    def translate(self, text, source_lang="en", target_lang="id"):
        self.calls.append(text)
        # strip any glossary hint the engine appended
        core = text.split("\n[Keep these")[0]
        if core in self.fail_on:
            return core.replace("%d", "").replace("{name}", "")
        return self.translations.get(core, f"ID:{core}")


class BadProvider(BaseProvider):
    name = "bad"

    def translate(self, text, source_lang="en", target_lang="id"):
        raise RuntimeError("boom")


class TestEngine(unittest.TestCase):
    def test_passthrough_default(self):
        res = translate_strings({"k1": "Hello"})
        self.assertEqual(res[0].status, "translated")
        self.assertEqual(res[0].translation, "Hello")

    def test_guard_skips(self):
        res = translate_strings({
            "dim": "1080x1920",
            "ver": "v1.2.3",
            "url": "https://example.com",
        }, provider=MockProvider())
        for r in res:
            self.assertEqual(r.status, "skipped", r.key)
            self.assertEqual(r.translation, r.source)

    def test_glossary_only_skipped(self):
        res = translate_strings({"g": "KuroPatch"}, provider=MockProvider())
        self.assertEqual(res[0].status, "skipped")
        self.assertEqual(res[0].detail, "glossary")

    def test_cache_hit(self):
        with tempfile.TemporaryDirectory() as d:
            cache = TranslationCache(os.path.join(d, "c.json"))
            p = MockProvider()
            r1 = translate_strings({"k": "Hello"}, provider=p, cache=cache)
            self.assertEqual(r1[0].status, "translated")
            p2 = MockProvider()
            r2 = translate_strings({"k": "Hello"}, provider=p2, cache=cache)
            self.assertEqual(r2[0].status, "cached")
            self.assertEqual(r2[0].translation, r1[0].translation)
            self.assertEqual(p2.calls, [])

    def test_placeholder_validation_failure(self):
        p = MockProvider(fail_on={"You earned %d gold"})
        res = translate_strings({"k": "You earned %d gold"}, provider=p,
                                max_retries=0)
        self.assertEqual(res[0].status, "failed")
        self.assertEqual(res[0].translation, res[0].source)  # never ship broken

    def test_provider_error_fails_safe(self):
        res = translate_strings({"k": "Hello"}, provider=BadProvider())
        self.assertEqual(res[0].status, "failed")
        self.assertEqual(res[0].translation, "Hello")

    def test_results_to_dict(self):
        res = translate_strings({"a": "Hi", "b": "1080x1920"},
                                provider=MockProvider())
        d = results_to_dict(res)
        self.assertEqual(d["a"], "ID:Hi")
        self.assertEqual(d["b"], "1080x1920")


class TestAgnesProviderKeyHandling(unittest.TestCase):
    def test_missing_key_raises(self):
        from providers import AgnesProvider
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            with self.assertRaises(RuntimeError):
                AgnesProvider().translate("Hello")
        finally:
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_no_key_in_signature(self):
        import inspect
        from providers import AgnesProvider
        sig = inspect.signature(AgnesProvider.translate)
        params = list(sig.parameters)
        self.assertNotIn("api_key", params)
        self.assertNotIn("key", params)


if __name__ == "__main__":
    unittest.main()
