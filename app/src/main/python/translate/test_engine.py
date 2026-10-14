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

    def test_language_names_never_hit_provider(self):
        # Live bug: Agnes mistranslated Español->Jerman, English->Ingris.
        # Glossary mapping must win WITHOUT calling the provider.
        p = MockProvider()
        res = translate_strings(
            {"a": "Español", "b": "English", "c": "Deutsch"}, provider=p)
        d = results_to_dict(res)
        self.assertEqual(d["a"], "Spanyol")
        self.assertEqual(d["b"], "Inggris")
        self.assertEqual(d["c"], "Jerman")
        self.assertEqual(p.calls, [])  # provider never called
        for r in res:
            self.assertEqual(r.status, "translated")
            self.assertEqual(r.detail, "glossary-language")

    def test_language_names_only_for_indonesian_target(self):
        # Mapping is Indonesian-only; other targets fall through.
        p = MockProvider()
        res = translate_strings({"a": "Español"}, provider=p, target_lang="en")
        self.assertEqual(res[0].translation, "ID:Español")
        self.assertEqual(len(p.calls), 1)

    def test_language_region_suffix_never_hits_provider(self):
        p = MockProvider()
        res = translate_strings(
            {"a": "English (US)", "b": "Español (México)"}, provider=p)
        d = results_to_dict(res)
        self.assertEqual(d["a"], "Inggris (US)")
        self.assertEqual(d["b"], "Spanyol (México)")
        self.assertEqual(p.calls, [])
        for r in res:
            self.assertEqual(r.detail, "glossary-language")

    def test_language_codes_skipped_in_engine(self):
        p = MockProvider()
        res = translate_strings({"a": "EN", "b": "ES"}, provider=p)
        for r in res:
            self.assertEqual(r.status, "skipped")
            self.assertEqual(r.detail, "language-code")
            self.assertEqual(r.translation, r.source)
        self.assertEqual(p.calls, [])

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


class FakeJavaMap:
    """Simulates a Chaquopy Java LinkedHashMap proxy: no .items(),
    but .entrySet() / .get() like the real bridge object.

    CRITICAL: a REAL Chaquopy Java Set proxy is NOT Python-iterable --
    ``for e in entry_set`` raises
    ``TypeError: 'LinkedHashMap$LinkedEntrySet' object is not iterable``.
    FakeJavaSet reproduces that: ``__iter__`` raises, traversal is ONLY
    via ``.iterator()`` / ``hasNext()`` / ``next()``. A test using a
    plain Python list for entrySet would HIDE this crash (v2.0.3 bug).
    """

    def __init__(self, data):
        self._data = dict(data)

    def entrySet(self):
        return FakeJavaSet([FakeJavaEntry(k, v)
                            for k, v in self._data.items()])

    def get(self, key):
        return self._data.get(key)

    def __len__(self):
        return len(self._data)

    def __bool__(self):
        return bool(self._data)


class FakeJavaSet:
    """Simulates a real Chaquopy LinkedHashMap$LinkedEntrySet proxy."""

    def __init__(self, entries):
        self._entries = list(entries)

    def __iter__(self):
        raise TypeError(
            "'LinkedHashMap$LinkedEntrySet' object is not iterable")

    def iterator(self):
        return FakeJavaIterator(self._entries)


class FakeJavaIterator:
    def __init__(self, entries):
        self._entries = entries
        self._i = 0

    def hasNext(self):
        return self._i < len(self._entries)

    def next(self):
        if not self.hasNext():
            raise StopIteration
        e = self._entries[self._i]
        self._i += 1
        return e


class FakeJavaEntry:
    def __init__(self, key, value):
        self._key, self._value = key, value

    def getKey(self):
        return self._key

    def getValue(self):
        return self._value


class TestJavaMapBoundary(unittest.TestCase):
    """Kotlin passes Java Maps to Python via Chaquopy; the engine must
    coerce them (regression: 'LinkedHashMap' has no attribute 'items')."""

    def test_translate_accepts_java_map(self):
        res = translate_strings(
            FakeJavaMap({"k1": "Hello"}), provider=MockProvider())
        self.assertEqual(res[0].status, "translated")
        self.assertEqual(res[0].translation, "ID:Hello")

    def test_entry_set_is_not_iterable(self):
        # Locks the double's fidelity: a real Chaquopy Set raises
        # TypeError on `for`. If this ever stops raising, the double
        # no longer reproduces the on-device crash and the regression
        # test above would silently pass on broken code.
        with self.assertRaises(TypeError):
            list(FakeJavaMap({"k1": "Hello"}).entrySet())


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


class TestFixedMapIntegration(unittest.TestCase):
    def test_fixed_terms_never_hit_provider(self):
        # Day/month/UI terms must win WITHOUT calling the provider.
        p = MockProvider()
        res = translate_strings(
            {"a": "Monday", "b": "Yes", "c": "January"}, provider=p)
        d = results_to_dict(res)
        self.assertEqual(d, {"a": "Senin", "b": "Ya", "c": "Januari"})
        self.assertEqual(p.calls, [])
        for r in res:
            self.assertEqual(r.status, "translated")
            self.assertEqual(r.detail, "fixed-map")

    def test_fixed_wins_over_stale_cache(self):
        # A cached mistranslation must never resurface for fixed terms.
        with tempfile.TemporaryDirectory() as d:
            cache = TranslationCache(os.path.join(d, "c.json"))
            cache.put("Monday", "en", "id", "agnes", "WRONG")
            p = MockProvider()
            res = translate_strings(
                {"a": "Monday"}, provider=p, cache=cache)
            self.assertEqual(res[0].translation, "Senin")
            self.assertEqual(res[0].detail, "fixed-map")
            self.assertEqual(p.calls, [])

    def test_fixed_only_for_matching_target(self):
        p = MockProvider()
        res = translate_strings(
            {"a": "Monday"}, provider=p, target_lang="en")
        self.assertEqual(res[0].translation, "ID:Monday")
        self.assertEqual(len(p.calls), 1)
