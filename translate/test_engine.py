"""Tests for the translate engine (mocked providers, no network)."""
import os
import tempfile
import unittest

from cache import TranslationCache
from engine import translate_strings, results_to_dict, results_summary
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


class EmptyProvider(BaseProvider):
    """Returns an empty string -- must never ship as a translation."""
    name = "empty"

    def translate(self, text, source_lang="en", target_lang="id"):
        return ""


class NoneProvider(BaseProvider):
    """Returns None -- must not crash validation with TypeError."""
    name = "none"

    def translate(self, text, source_lang="en", target_lang="id"):
        return None


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

    def test_invalid_cache_hit_is_retranslated(self):
        # Round-3 audit: a cache hit was trusted blindly -- a stale /
        # hand-edited entry that drops placeholders shipped straight
        # into the game, defeating the validator. An invalid hit is
        # a miss: the provider is consulted again.
        import json as _json
        from cache import _key as _cache_key
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            with open(p, "w") as fh:
                _json.dump({
                    _cache_key("You earned %d gold", "en", "id", "mock"):
                        "Kamu dapat emas",  # %d dropped -- corrupt
                }, fh)
            cache = TranslationCache(p)
            res = translate_strings({"k": "You earned %d gold"},
                                    provider=MockProvider(), cache=cache)
            # MockProvider returns "ID:<source>", placeholders intact
            self.assertEqual(res[0].status, "translated")
            self.assertIn("%d", res[0].translation)

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

    def test_empty_translation_never_ships(self):
        # Haiku audit: "" used to come back status=translated and
        # blank out the game string. Now: failed, source kept.
        res = translate_strings({"k": "Hello world"},
                                provider=EmptyProvider(), max_retries=0)
        self.assertEqual(res[0].status, "failed")
        self.assertEqual(res[0].translation, "Hello world")

    def test_none_translation_no_crash(self):
        res = translate_strings({"k": "Hello world"},
                                provider=NoneProvider(), max_retries=0)
        self.assertEqual(res[0].status, "failed")
        self.assertEqual(res[0].translation, "Hello world")

    def test_non_string_input_no_crash(self):
        # Plain-dict callers can pass non-string values; the old code
        # crashed in text.strip() (AttributeError).
        res = translate_strings({"k": 12345})
        self.assertEqual(res[0].status, "skipped")
        self.assertEqual(res[0].translation, 12345)

    def test_none_input_returns_empty(self):
        # Round-2 audit: translate_strings(None) raised a raw
        # TypeError from dict(None). Now: empty list, no crash.
        self.assertEqual(translate_strings(None), [])

    def test_results_to_dict(self):
        res = translate_strings({"a": "Hi", "b": "1080x1920"},
                                provider=MockProvider())
        d = results_to_dict(res)
        self.assertEqual(d["a"], "ID:Hi")
        self.assertEqual(d["b"], "1080x1920")

    def test_results_summary_reports_failures(self):
        # On-device bug (Sims 3): a dead provider failed all 10
        # translatable strings, but the UI showed "0 translated, 44
        # unchanged" with NO error -- results_to_dict discards the
        # failure info. results_summary must expose it.
        class DeadProvider:
            name = "agnes"

            def translate(self, text, src, tgt):
                raise RuntimeError("HTTP 401: invalid API key")

        res = translate_strings({"a": "Hello world", "b": "1080x1920"},
                                provider=DeadProvider())
        summary = results_summary(res)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["changed"], 0)
        self.assertIn("401", summary["first_error"])
        # And prove the dict alone really does lose it (the bug):
        d = results_to_dict(res)
        self.assertEqual(d, {"a": "Hello world", "b": "1080x1920"})

    def test_results_summary_mixed_run(self):
        class FlakyProvider:
            name = "flaky"

            def translate(self, text, src, tgt):
                if "boom" in text:
                    raise RuntimeError("boom failed")
                return "ID:" + text

        res = translate_strings({"a": "Hello world", "b": "boom town"},
                                provider=FlakyProvider())
        summary = results_summary(res)
        self.assertEqual(summary["translated"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["changed"], 1)
        self.assertIn("boom", summary["first_error"])


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
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        set_api_key(None)  # order-independent: no stale override
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

    def test_set_api_key_delivers_key_without_env(self):
        # PROVEN on-device bug (v2.0.9): Kotlin's Os.setenv never
        # reached CPython's os.environ snapshot, so _api_key() ALWAYS
        # saw "" even with a saved key. set_api_key() is the explicit
        # handoff that fixes it: no env var, setter only -> key seen.
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            set_api_key(None)
            with self.assertRaises(RuntimeError):
                AgnesProvider()._api_key()
            set_api_key("test-key")
            self.assertEqual(AgnesProvider()._api_key(), "test-key")
        finally:
            set_api_key(None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_set_api_key_overwrites(self):
        # Key changed mid-session (user re-saves in the prompt):
        # the newest key must win, the old one must not linger.
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            set_api_key(None)
            set_api_key("key-A")
            self.assertEqual(AgnesProvider()._api_key(), "key-A")
            set_api_key("key-B")
            self.assertEqual(AgnesProvider()._api_key(), "key-B")
        finally:
            set_api_key(None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_set_api_key_blank_clears(self):
        # Blank / None must CLEAR the override, restoring pure env-var
        # lookup -- a cleared key must never keep translating on the
        # stale one.
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            set_api_key(None)
            set_api_key("test-key")
            self.assertEqual(AgnesProvider()._api_key(), "test-key")
            set_api_key("")
            with self.assertRaises(RuntimeError):
                AgnesProvider()._api_key()
            set_api_key("test-key")
            set_api_key(None)
            with self.assertRaises(RuntimeError):
                AgnesProvider()._api_key()
            # Whitespace-only is blank too.
            set_api_key("   ")
            with self.assertRaises(RuntimeError):
                AgnesProvider()._api_key()
        finally:
            set_api_key(None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_set_api_key_beats_env(self):
        # Explicit handoff wins over a stale env snapshot.
        from providers import AgnesProvider, set_api_key
        old = os.environ.get("AGNES_API_KEY")
        try:
            set_api_key(None)
            os.environ["AGNES_API_KEY"] = "env-key"
            set_api_key("explicit-key")
            self.assertEqual(AgnesProvider()._api_key(), "explicit-key")
        finally:
            set_api_key(None)
            if old is None:
                os.environ.pop("AGNES_API_KEY", None)
            else:
                os.environ["AGNES_API_KEY"] = old

    def test_env_fallback_still_works(self):
        # CLI path: no setter call at all, env var set -> env used.
        from providers import AgnesProvider
        old = os.environ.get("AGNES_API_KEY")
        try:
            set_api_key(None)
            os.environ["AGNES_API_KEY"] = "env-key"
            self.assertEqual(AgnesProvider()._api_key(), "env-key")
        finally:
            set_api_key(None)
            if old is None:
                os.environ.pop("AGNES_API_KEY", None)
            else:
                os.environ["AGNES_API_KEY"] = old

    def test_translate_uses_set_api_key_without_env(self):
        # Full path, exact on-device scenario: NO env var, setter only.
        # Provider must reach the API with the setter key (proven by
        # the captured Authorization header).
        import json
        import urllib.request
        from providers import AgnesProvider, set_api_key

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        payload = {"choices": [{"message": {"content": "Halo"},
                                "finish_reason": "stop"}]}
        captured = {}
        orig = urllib.request.urlopen

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return FakeResp(payload)

        env = os.environ.pop("AGNES_API_KEY", None)
        urllib.request.urlopen = fake_urlopen
        try:
            set_api_key(None)
            set_api_key("test-key")
            self.assertEqual(
                AgnesProvider().translate("Hello", "en", "id"), "Halo")
            self.assertEqual(captured.get("auth"), "Bearer test-key")
        finally:
            urllib.request.urlopen = orig
            set_api_key(None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_setter_works_without_env(self):
        # PROVEN on-device bug (v2.0.9): Kotlin's Os.setenv never
        # reaches CPython's os.environ snapshot, so the provider saw
        # "" despite a saved key. The explicit setter must make the
        # provider work with NO env var at all.
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            set_api_key("test-key")
            self.assertEqual(AgnesProvider()._api_key(), "test-key")
            # End-to-end (mocked network): translate() proceeds past
            # key lookup and returns the provider's content.
            p = AgnesProvider()
            p._fetch = lambda req: {"choices": [
                {"message": {"content": "Halo"},
                 "finish_reason": "stop"}]}
            self.assertEqual(p.translate("Hello"), "Halo")
        finally:
            set_api_key(None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_setter_empty_is_error(self):
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            for blank in ("", "   ", None):
                set_api_key(blank)
                with self.assertRaises(RuntimeError):
                    AgnesProvider()._api_key()
        finally:
            set_api_key(None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_env_fallback_still_works(self):
        # CLI path: no setter, key from the caller's shell env.
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            set_api_key(None)
            os.environ["AGNES_API_KEY"] = "env-key"
            self.assertEqual(AgnesProvider()._api_key(), "env-key")
        finally:
            set_api_key(None)
            os.environ.pop("AGNES_API_KEY", None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env

    def test_libc_setenv_does_not_reach_os_environ(self):
        # The OLD mechanism (Kotlin Os.setenv) is dead by CPython
        # design: os.environ is a startup snapshot; a libc setenv()
        # from outside Python never updates it. This pins that fact
        # so nobody "simplifies" the setter away and reintroduces
        # the on-device bug. Probe var, not the real key, to avoid
        # polluting AGNES_API_KEY -- the mechanism is identical.
        import ctypes
        probe = "KUROPATCH_SETENV_PROBE"
        os.environ.pop(probe, None)
        try:
            ctypes.CDLL(None).setenv(probe.encode(), b"probe-value", 1)
            self.assertEqual(os.environ.get(probe, ""), "")
        finally:
            ctypes.CDLL(None).unsetenv(probe.encode())
            os.environ.pop(probe, None)

    def test_setter_beats_env(self):
        # On-device the setter carries the CURRENT saved key; a stale
        # env var (if any) must never shadow it.
        from providers import AgnesProvider, set_api_key
        env = os.environ.pop("AGNES_API_KEY", None)
        try:
            os.environ["AGNES_API_KEY"] = "env-key"
            set_api_key("setter-key")
            self.assertEqual(AgnesProvider()._api_key(), "setter-key")
        finally:
            set_api_key(None)
            os.environ.pop("AGNES_API_KEY", None)
            if env is not None:
                os.environ["AGNES_API_KEY"] = env


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


class TestRetryKeepsGlossaryHint(unittest.TestCase):
    def test_retry_prompt_keeps_keep_verbatim_hint(self):
        # Round-2 (Agnes): the retry prompt used to drop the glossary
        # keep-verbatim hint, so a retried string could mistranslate a
        # brand term the first attempt had protected.
        class RetryMock(BaseProvider):
            name = "retrymock"

            def __init__(self):
                self.calls = []
                self.n = 0

            def translate(self, text, source_lang="en", target_lang="id"):
                self.calls.append(text)
                core = text
                prefix = ("Translate ONLY. Do not explain. Copy every "
                          "placeholder character-for-character.\n")
                if core.startswith(prefix):
                    core = core[len(prefix):]
                core = core.split("\n[Keep these")[0]
                self.n += 1
                if self.n == 1:
                    return core.replace("%d", "")  # fail validation
                return f"ID:{core}"

        p = RetryMock()
        res = translate_strings({"a": "Play KuroPatch %d"}, provider=p)
        self.assertEqual(len(p.calls), 2)
        self.assertIn("Keep these terms", p.calls[1])
        self.assertIn("KuroPatch", p.calls[1])
        self.assertEqual(res[0].translation, "ID:Play KuroPatch %d")


class TestTruncatedResponseRejected(unittest.TestCase):
    def test_finish_reason_length_raises(self):
        # Round-2 (Agnes): a response truncated at max_tokens used to
        # ship as a translation -- the validator cannot detect a
        # mid-sentence cut when placeholders survive by luck.
        import json
        import urllib.request
        from providers import AgnesProvider

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        payload = {"choices": [{"message": {"content": "Skor: %d terpotong"},
                                "finish_reason": "length"}]}
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            with self.assertRaises(RuntimeError):
                AgnesProvider().translate("Score: %d cut off", "en", "id")
        finally:
            urllib.request.urlopen = orig
            os.environ.pop("AGNES_API_KEY", None)

    def test_finish_reason_stop_ok(self):
        import json
        import urllib.request
        from providers import AgnesProvider

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        payload = {"choices": [{"message": {"content": "Skor: %d"},
                                "finish_reason": "stop"}]}
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            self.assertEqual(
                AgnesProvider().translate("Score: %d", "en", "id"),
                "Skor: %d")
        finally:
            urllib.request.urlopen = orig
            os.environ.pop("AGNES_API_KEY", None)


    def test_null_content_raises_clear_error(self):
        # Final audit round 1: a null content (content filter /
        # empty reply) died with a raw AttributeError on .strip()
        # -- first_error showed "NoneType has no attribute 'strip'".
        # Must raise a clear RuntimeError instead.
        import json
        import urllib.request
        from providers import AgnesProvider

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        payload = {"choices": [{"message": {"content": None},
                                "finish_reason": "stop"}]}
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            with self.assertRaises(RuntimeError) as ctx:
                AgnesProvider().translate("Hello", "en", "id")
            self.assertIn("no content", str(ctx.exception))
        finally:
            urllib.request.urlopen = orig
            os.environ.pop("AGNES_API_KEY", None)


class TestAgnesProviderRetry(unittest.TestCase):
    """Final audit round 2: 429 / 5xx must RETRY with backoff.

    Before the fix, a single 429 raised immediately -- a burst of
    strings tripped the free-tier rate limit, the engine counted 3
    consecutive failures and aborted the whole batch. A 429 means
    "slow down", not "provider dead".
    """

    def _run(self, behaviors):
        """behaviors: list of HTTPError | dict-payload, one per urlopen call.

        Returns (result_or_exception, calls, sleeps).
        """
        import json
        import urllib.error
        import urllib.request
        import providers as providers_mod
        from providers import AgnesProvider

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        state = {"calls": 0, "sleeps": []}
        it = iter(behaviors)

        def fake_urlopen(req, timeout=None):
            state["calls"] += 1
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return FakeResp(item)

        orig_urlopen = urllib.request.urlopen
        # Old code (pre-retry-fix) has no _sleep hook -- use getattr
        # so the helper works there too (the tests then FAIL on
        # behavior, not ERROR on the harness).
        orig_sleep = getattr(providers_mod, "_sleep", None)
        urllib.request.urlopen = fake_urlopen
        providers_mod._sleep = lambda s: state["sleeps"].append(s)
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            try:
                out = AgnesProvider().translate("Hello", "en", "id")
                return out, state["calls"], state["sleeps"]
            except Exception as exc:  # noqa: BLE001 -- test captures it
                return exc, state["calls"], state["sleeps"]
        finally:
            urllib.request.urlopen = orig_urlopen
            if orig_sleep is not None:
                providers_mod._sleep = orig_sleep
            else:
                try:
                    del providers_mod._sleep
                except AttributeError:
                    pass
            os.environ.pop("AGNES_API_KEY", None)

    @staticmethod
    def _http_error(code, retry_after=None):
        import urllib.error
        hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return urllib.error.HTTPError(
            "https://apihub.agnes-ai.com/v1/chat/completions",
            code, "Too Many Requests" if code == 429 else "Error",
            hdrs, None)

    @staticmethod
    def _ok_payload(text="Halo"):
        return {"choices": [{"message": {"content": text},
                             "finish_reason": "stop"}]}

    def test_429_recovers_after_retries(self):
        out, calls, sleeps = self._run([
            self._http_error(429), self._http_error(429),
            self._ok_payload("Halo")])
        self.assertEqual(out, "Halo")
        self.assertEqual(calls, 3)
        self.assertEqual(sleeps, [1, 2])

    def test_429_exhausted_raises_after_three_retries(self):
        out, calls, sleeps = self._run([self._http_error(429)] * 4)
        self.assertIsInstance(out, RuntimeError)
        self.assertIn("429", str(out))
        self.assertEqual(calls, 4)  # initial + 3 retries
        self.assertEqual(sleeps, [1, 2, 4])

    def test_401_not_retried(self):
        out, calls, sleeps = self._run([self._http_error(401)])
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])

    def test_503_retried(self):
        out, calls, sleeps = self._run([
            self._http_error(503), self._ok_payload("Halo")])
        self.assertEqual(out, "Halo")
        self.assertEqual(calls, 2)
        self.assertEqual(sleeps, [1])

    def test_retry_after_header_honored_and_capped(self):
        out, calls, sleeps = self._run([
            self._http_error(429, retry_after=5), self._ok_payload("Halo")])
        self.assertEqual(out, "Halo")
        self.assertEqual(sleeps, [5])
        out, calls, sleeps = self._run([
            self._http_error(429, retry_after=3600), self._ok_payload("Halo")])
        self.assertEqual(out, "Halo")
        self.assertEqual(sleeps, [10])  # capped, never wait an hour

    def test_network_error_not_retried(self):
        import urllib.error
        out, calls, sleeps = self._run([
            urllib.error.URLError("connection refused")])
        self.assertIsInstance(out, RuntimeError)
        self.assertEqual(calls, 1)
        self.assertEqual(sleeps, [])


class TestCacheSharedAcrossBatches(unittest.TestCase):
    def test_cache_is_global_not_per_jar(self):
        # Round-2 audit, part 3: the cache is keyed by
        # (text, source, target, provider), NOT per-jar. A second
        # translate batch (a different game) must hit the first
        # batch's entry without calling the provider again.
        with tempfile.TemporaryDirectory() as d:
            cache = TranslationCache(os.path.join(d, "c.json"))
            p1 = MockProvider({"Choose your character": "Pilih karaktermu"})
            r1 = translate_strings(
                {"gameA": "Choose your character"}, provider=p1, cache=cache)
            self.assertEqual(r1[0].translation, "Pilih karaktermu")

            class MustNotBeCalled(BaseProvider):
                name = "mock"

                def translate(self, text, source_lang="en", target_lang="id"):
                    raise AssertionError("provider must not be called")

            r2 = translate_strings(
                {"gameB": "Choose your character"},
                provider=MustNotBeCalled(), cache=cache)
            self.assertEqual(r2[0].status, "cached")
            self.assertEqual(r2[0].translation, "Pilih karaktermu")
    def test_dead_provider_aborts_after_three_errors(self):
        # Round-4 audit: a dead network used to mean one timeout PER
        # string (97 strings = 97 timeouts). Three consecutive
        # provider exceptions must abort the batch.
        class CountingDeadProvider(BaseProvider):
            name = "dead"

            def __init__(self):
                self.calls = 0

            def translate(self, text, source_lang="en", target_lang="id"):
                self.calls += 1
                raise RuntimeError("network down")

        p = CountingDeadProvider()
        strings = {f"k{i}": f"String number {i}" for i in range(5)}
        res = translate_strings(strings, provider=p)
        self.assertEqual(p.calls, 3)
        self.assertEqual(len(res), 5)
        for r in res:
            self.assertEqual(r.status, "failed", r.key)
            self.assertEqual(r.translation, r.source)
        self.assertIn("aborted", res[4].detail)

    def test_validation_failures_do_not_abort(self):
        # Validation failures are per-string issues (bad translation),
        # NOT infrastructure failure -- they must never trigger the
        # abort. All 5 strings must be attempted. (The provider strips
        # placeholders from whatever it receives, including the retry
        # prompt -- a naive echo mock would pass validation spuriously
        # because the retry prompt embeds the source text.)
        import re

        class StripProvider(BaseProvider):
            name = "strip"

            def __init__(self):
                self.calls = []

            def translate(self, text, source_lang="en", target_lang="id"):
                self.calls.append(text)
                return re.sub(r"%[a-zA-Z%]", "", text)

        p = StripProvider()
        strings = {f"k{i}": t for i, t in enumerate([
            "Alpha %d one", "Beta %d two", "Gamma %d three",
            "Delta %d four", "Epsilon %d five"])}
        res = translate_strings(strings, provider=p)
        # 2 attempts x 5 strings (retry included): every string was
        # attempted, none short-circuited by the abort.
        self.assertEqual(len(p.calls), 10)
        for r in res:
            self.assertEqual(r.status, "failed", r.key)

    def test_glossary_and_skip_survive_provider_death(self):
        # The abort only skips PROVIDER CALLS: glossary / fixed-map /
        # skip paths must still be served after the provider dies.
        class CountingDeadProvider(BaseProvider):
            name = "dead"

            def __init__(self):
                self.calls = 0

            def translate(self, text, source_lang="en", target_lang="id"):
                self.calls += 1
                raise RuntimeError("network down")

        p = CountingDeadProvider()
        strings = {
            "a": "Alpha one", "b": "Beta two", "c": "Gamma three",
            "lang": "Español",  # glossary hit, needs no provider
            "dim": "1080x1920",  # skip, needs no provider
        }
        res = translate_strings(strings, provider=p)
        d = {r.key: r for r in res}
        self.assertEqual(p.calls, 3)
        self.assertEqual(d["lang"].status, "translated")
        self.assertEqual(d["lang"].translation, "Spanyol")
        self.assertEqual(d["dim"].status, "skipped")
