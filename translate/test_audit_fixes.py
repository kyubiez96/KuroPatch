"""Regression tests for the 16 findings fixed in the 2026-10-14 audit
(12 translate-pipeline findings + Argon's 4 batch-abort findings).

Each test class names its finding. Tests that would have FAILED on the
pre-fix code are marked [PRE-FAIL]; tests that pin intended behaviour
(Argon A1/A2 -- verified NOT bugs) are marked [PIN].
"""
import json
import os
import tempfile
import unittest

import engine as engine_mod
import providers as providers_mod
from cache import TranslationCache
from engine import translate_strings, results_to_dict
from guard import should_skip
from providers import AgnesProvider, BaseProvider, TransientProviderError
from validator import validate, extract_placeholders


class _RecordingProvider(BaseProvider):
    """Returns a fixed reply per source; records (text, extra)."""
    name = "recording"

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def translate(self, text, source_lang="en", target_lang="id",
                  extra_instruction=None):
        self.calls.append((text, extra_instruction))
        return self.replies.get(text, "ID:" + text)


# ---------------------------------------------------------------- HIGH #1
class TestWhitespacePreserved(unittest.TestCase):
    """[PRE-FAIL] Provider strip + no validator check destroyed
    load-bearing edge whitespace on .loc / .properties / smali paths
    (fragments completed at runtime by concatenation)."""

    def test_validator_rejects_edge_whitespace_change(self):
        self.assertFalse(validate("Hello ", "Halo"))
        self.assertFalse(validate(" Hello", "Halo"))
        self.assertFalse(validate("  Hello  ", "Halo"))
        self.assertFalse(validate("Hello", " Halo"))
        self.assertTrue(validate("Hello ", "Halo "))
        self.assertTrue(validate(" Hello", " Halo"))
        self.assertTrue(validate("  Hello  ", "  Halo  "))

    def test_engine_reapplies_source_whitespace(self):
        # The model returns stripped edges; the engine restores the
        # source's exact edges before validation / shipping.
        p = _RecordingProvider({
            "Celia... ": "Celia...",
            " seconds remaining": "detik tersisa",
            "GAME WILL NOT BE SAVED. DO YOU WANT TO QUIT GAME? ":
                "GAME TIDAK AKAN DISIMPAN. APAKAH KAMU MAU KELUAR DARI GAME?",
        })
        res = translate_strings(
            {"a": "Celia... ",
             "b": " seconds remaining",
             "c": "GAME WILL NOT BE SAVED. DO YOU WANT TO QUIT GAME? "},
            provider=p)
        d = results_to_dict(res)
        self.assertEqual(d["a"], "Celia... ")
        self.assertEqual(d["b"], " detik tersisa")
        self.assertTrue(d["c"].endswith(" "))

    def test_engine_strips_model_added_edge_whitespace(self):
        # The model ADDS a trailing newline; the engine normalizes
        # to the source's edges (no trailing newline in the source).
        p = _RecordingProvider({"Hello": "Halo\n"})
        res = translate_strings({"a": "Hello"}, provider=p)
        self.assertEqual(res[0].translation, "Halo")

    def test_provider_does_not_strip(self):
        # AgnesProvider must return the model content verbatim --
        # stripping is the engine's job (re-apply), not the provider's.
        import urllib.request

        class FakeResp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        payload = {"choices": [{"message": {"content": "  Halo  "},
                                "finish_reason": "stop"}]}
        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            out = AgnesProvider().translate("Hello")
        finally:
            urllib.request.urlopen = orig
            os.environ.pop("AGNES_API_KEY", None)
        self.assertEqual(out, "  Halo  ")


# ---------------------------------------------------------------- MED #2
class TestCuratedMapBeatsHeuristicGuard(unittest.TestCase):
    """[PRE-FAIL] Heuristic guard rules shadowed the curated maps:
    "monday" skipped as lower-token while "Monday" -> "Senin"."""

    def test_lowercase_map_words_translate(self):
        p = _RecordingProvider({})
        res = translate_strings(
            {"a": "monday", "b": "may", "c": "settings", "d": "english"},
            provider=p)
        d = results_to_dict(res)
        self.assertEqual(d["a"], "Senin")
        self.assertEqual(d["b"], "Mei")
        self.assertEqual(d["c"], "Pengaturan")
        self.assertEqual(d["d"], "Inggris")
        self.assertEqual(p.calls, [])  # never hit the provider

    def test_genuine_iso_codes_still_skipped(self):
        p = _RecordingProvider({})
        res = translate_strings({"a": "EN", "b": "ES", "c": "DE"},
                                provider=p)
        for r in res:
            self.assertEqual(r.status, "skipped")
            self.assertEqual(r.detail, "language-code")
            self.assertEqual(r.translation, r.source)
        self.assertEqual(p.calls, [])


# ---------------------------------------------------------------- MED #3
class TestRetryPromptIsFullBlock(unittest.TestCase):
    """[PRE-FAIL] The validation-retry prompt dropped the $N /
    line-break / whitespace / ASCII constraints."""

    def test_retry_extra_instruction_has_strict_line_and_terms(self):
        class Flaky(BaseProvider):
            name = "flaky"

            def __init__(self):
                self.calls = []
                self.n = 0

            def translate(self, text, source_lang="en", target_lang="id",
                          extra_instruction=None):
                self.calls.append((text, extra_instruction))
                self.n += 1
                if self.n == 1:
                    return text.replace("%d", "")
                return f"ID:{text}"

        p = Flaky()
        res = translate_strings({"a": "Play KuroPatch %d"}, provider=p)
        self.assertEqual(res[0].translation, "ID:Play KuroPatch %d")
        first_extra, retry_extra = p.calls[0][1], p.calls[1][1]
        # First attempt: terms line present.
        self.assertIn("KuroPatch", first_extra)
        # Retry: SAME terms + the stricter placeholder line.
        self.assertIn("KuroPatch", retry_extra)
        self.assertIn("Translate ONLY", retry_extra)
        # Neither attempt stuffs instructions into the Text: field.
        self.assertEqual(p.calls[0][0], "Play KuroPatch %d")
        self.assertEqual(p.calls[1][0], "Play KuroPatch %d")


# ---------------------------------------------------------------- MED #4
class TestValidatorChecksLineBreaks(unittest.TestCase):
    """[PRE-FAIL] validate() never checked line breaks."""

    def test_line_break_mismatch_fails(self):
        self.assertFalse(validate("Line1\nLine2", "Baris1 Baris2"))
        self.assertFalse(validate("A\nB\nC", "A\nB"))
        self.assertFalse(validate("A", "A\nB"))
        self.assertTrue(validate("Line1\nLine2", "Baris1\nBaris2"))
        self.assertTrue(validate("No breaks", "Tanpa jeda"))


# ---------------------------------------------------------------- MED #5
class TestKeepVerbatimOutsideTextField(unittest.TestCase):
    """[PRE-FAIL] protect_hint injected the keep-verbatim note INSIDE
    the translatable Text: field."""

    def test_terms_line_before_text_field(self):
        prompt = AgnesProvider()._build_prompt(
            "Play KuroPatch %d", "en", "id",
            extra_instruction='Terms to keep verbatim: "KuroPatch".')
        terms_pos = prompt.index("Terms to keep verbatim")
        text_pos = prompt.index("Text: ")
        self.assertLess(terms_pos, text_pos)
        # The Text: field carries ONLY the raw text.
        self.assertEqual(prompt[text_pos + len("Text: "):],
                         "Play KuroPatch %d")


# ---------------------------------------------------------------- LOW #6
class TestMapBeatsIsoGuard(unittest.TestCase):
    """[PRE-FAIL] "Lao" never reached lookup_language (ISO guard)."""

    def test_lao_reaches_language_map(self):
        p = _RecordingProvider({})
        res = translate_strings({"a": "Lao"}, provider=p)
        self.assertEqual(res[0].translation, "Laos")
        self.assertEqual(p.calls, [])

    def test_unmapped_iso_lookalikes_still_skipped(self):
        # "Ice"/"Arm"/"Pan"/"Sin"/"Be"/"Is" have no curated mapping,
        # so the ISO guard still (correctly) skips them -- the fix
        # only lets exact MAP matches win, it does not whitelist
        # ambiguous words.
        p = _RecordingProvider({})
        res = translate_strings(
            {"a": "Ice", "b": "Arm", "c": "Pan", "d": "Sin",
             "e": "Be", "f": "Is"}, provider=p)
        for r in res:
            self.assertEqual(r.status, "skipped")
            self.assertEqual(r.detail, "language-code")
        self.assertEqual(p.calls, [])


# ---------------------------------------------------------------- LOW #7
class TestHyphenatedWordsNotIdentifiers(unittest.TestCase):
    """[PRE-FAIL] "well-known" / "two-player" / "co-op" skipped as
    identifiers."""

    def test_hyphenated_display_words_translate(self):
        for word in ("well-known", "two-player", "co-op"):
            skip, _ = should_skip(word)
            self.assertFalse(skip, word)

    def test_real_identifiers_still_skipped(self):
        for word in ("level-1", "menu_start", "com.game.Main", "btn.ok"):
            skip, reason = should_skip(word)
            self.assertTrue(skip, word)
            self.assertEqual(reason, "identifier", word)


# ---------------------------------------------------------------- LOW #8
class TestDimensionAndPathAnchored(unittest.TestCase):
    """[PRE-FAIL] Dimension/path rules used substring search on whole
    sentences ("Play at 720p for best experience" skipped wholesale)."""

    def test_sentence_mentioning_resolution_translates(self):
        skip, _ = should_skip("Play at 720p for best experience")
        self.assertFalse(skip)
        skip, _ = should_skip("Save as .png")
        self.assertFalse(skip)

    def test_bare_dimension_and_filename_still_skipped(self):
        for s, reason in (("1080x1920", "screen-dimension"),
                           ("720p", "screen-dimension"),
                           ("4K", "screen-dimension"),
                           ("icon.png", "file-path"),
                           ("game.apk", "file-path"),
                           ("Save as game.png", "file-path")):
            skip, got = should_skip(s)
            self.assertTrue(skip, s)
            self.assertEqual(got, reason, s)


# ---------------------------------------------------------------- LOW #9
class TestLiteralNewlineEscapeNormalization(unittest.TestCase):
    """[PRE-FAIL] A real newline for a literal \\n escape failed
    validation forever (string never translated)."""

    def test_real_newline_satisfies_literal_escape(self):
        self.assertTrue(validate("Line1\\nLine2", "Baris1\nBaris2"))
        # The reverse (real newline -> literal escape) still fails:
        # it would ship a literal "\n" into the game text.
        self.assertFalse(validate("Line1\nLine2", "Baris1\\nBaris2"))

    def test_engine_translates_literal_escape_source(self):
        p = _RecordingProvider({"Line1\\nLine2": "Baris1\nBaris2"})
        res = translate_strings({"a": "Line1\\nLine2"}, provider=p)
        self.assertEqual(res[0].status, "translated")
        self.assertEqual(res[0].translation, "Baris1\nBaris2")


# ---------------------------------------------------------------- LOW #10
class TestPairedGameloftCodesOneChar(unittest.TestCase):
    """[PRE-FAIL] \\m1\\m matched only the \\m half."""

    def test_one_char_param_code_extracted_whole(self):
        self.assertEqual(extract_placeholders("\\m1\\m"), ["\\m1\\m"])
        self.assertEqual(extract_placeholders("\\f3\\f"), ["\\f3\\f"])
        self.assertEqual(extract_placeholders("\\m12\\m"), ["\\m12\\m"])
        self.assertTrue(validate("\\m1\\mHello", "\\m1\\mHalo"))
        self.assertFalse(validate("\\m1\\mHello", "Halo"))


# ---------------------------------------------------------------- LOW #11
class TestCacheDirtyOnlyOnSuccess(unittest.TestCase):
    """[PRE-FAIL] save() cleared _dirty even when the write failed."""

    def test_failed_save_keeps_dirty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            cache = TranslationCache(path)
            cache.put("Hello", "en", "id", "mock", "Halo")
            # A directory sitting at the cache path makes os.replace
            # fail (IsADirectoryError) -- write must not crash, and
            # _dirty must stay True so a later save() retries.
            os.mkdir(path)
            cache.save()
            self.assertTrue(cache._dirty)
            os.rmdir(path)
            cache.save()
            self.assertFalse(cache._dirty)
            self.assertEqual(cache.get("Hello", "en", "id", "mock"),
                             "Halo")


# ---------------------------------------------------------------- LOW #12
class TestMalformed200IsTransient(unittest.TestCase):
    """[PRE-FAIL] JSON/UTF-8 decode errors on a 200 body were
    classified PERMANENT (RuntimeError), bypassing recovery."""

    def _provider_with_body(self, body):
        import urllib.request

        class FakeResp:
            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: FakeResp()
        orig_sleep = providers_mod._sleep
        providers_mod._sleep = lambda s: None
        self.addCleanup(setattr, urllib.request, "urlopen", orig)
        self.addCleanup(setattr, providers_mod, "_sleep", orig_sleep)
        os.environ["AGNES_API_KEY"] = "dummy-for-test"
        self.addCleanup(os.environ.pop, "AGNES_API_KEY", None)
        return AgnesProvider()

    def test_bad_json_is_transient(self):
        p = self._provider_with_body(b"{not json")
        with self.assertRaises(TransientProviderError):
            p.translate("Hello")

    def test_non_utf8_body_is_transient(self):
        p = self._provider_with_body(b"\xff\xfe not utf-8")
        with self.assertRaises(TransientProviderError):
            p.translate("Hello")


# ------------------------------------------------------------- Argon A1
class TestProviderLevelRetriesCoverStreak(unittest.TestCase):
    """[PIN] Argon HIGH A1, VERIFIED intended: the engine `break` on a
    provider exception does NOT lose strings without retry -- each
    provider call ALREADY retries 3x internally."""

    def test_streak_strings_each_get_provider_retries(self):
        import urllib.error
        import urllib.request

        attempts = []
        provider_sleeps = []
        engine_sleeps = []
        orig_urlopen = urllib.request.urlopen
        orig_psleep = providers_mod._sleep
        orig_esleep = engine_mod._sleep

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            raise urllib.error.HTTPError(
                "https://apihub.agnes-ai.com/v1/chat/completions",
                429, "Too Many Requests", {}, None)

        urllib.request.urlopen = fake_urlopen
        providers_mod._sleep = provider_sleeps.append
        engine_mod._sleep = engine_sleeps.append
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            res = translate_strings(
                {"a": "Game string 1", "b": "Game string 2"},
                provider=AgnesProvider())
        finally:
            urllib.request.urlopen = orig_urlopen
            providers_mod._sleep = orig_psleep
            engine_mod._sleep = orig_esleep
            os.environ.pop("AGNES_API_KEY", None)
        # 2 strings x (1 + 3 provider-level retries) = 8 HTTP attempts.
        self.assertEqual(len(attempts), 8)
        self.assertEqual(provider_sleeps, [1, 2, 4, 1, 2, 4])
        # 2 consecutive errors < 3: no recovery, no batch abort.
        self.assertEqual([r.status for r in res], ["failed", "failed"])
        self.assertEqual(engine_sleeps, [12.0])  # adaptive: 6 -> 12


# ------------------------------------------------------------- Argon A2
class TestRecoveryNeverWastesSleepAtMaxAttempt(unittest.TestCase):
    """[PIN] Argon HIGH A2, VERIFIED no-op: Argon feared the recovery
    `continue` could fire with `attempt` already at max_retries,
    wasting the 10s/30s sleep on a string that is abandoned anyway.
    That cannot happen: `attempt` only grows after a provider ANSWER
    (validation failure), and every answer resets the consecutive
    streak to 0 -- so a string that just failed validation can
    contribute at most 1 to the streak, never reaching the 3-streak
    recovery. Conversely, every recovery that DOES fire has
    attempt == 0 (no prior answer in this string's loop), so the
    retry always happens (pinned by
    test_transient_errors_recover_and_batch_continues)."""

    def test_no_wasted_recovery_sleep_at_max_attempt(self):
        class StreakProvider(BaseProvider):
            name = "streak"

            def __init__(self):
                self.calls = 0

            def translate(self, text, source_lang="en",
                          target_lang="id", extra_instruction=None):
                self.calls += 1
                if self.calls <= 2:
                    # k0, k1: two consecutive transient failures.
                    raise TransientProviderError("429")
                if self.calls == 3:
                    # k2: validation failure (drops %d) -- attempt
                    # becomes 1 == max_retries, streak resets to 0.
                    return "Kamu dapat emas"
                # k2 retry: transient error at attempt == max_retries.
                raise TransientProviderError("429")

        p = StreakProvider()
        orig = engine_mod._sleep
        sleeps = []
        engine_mod._sleep = sleeps.append
        try:
            res = translate_strings(
                {"a": "Game string 1", "b": "Game string 2",
                 "c": "You earned %d gold"},
                provider=p, max_retries=1)
        finally:
            engine_mod._sleep = orig
        # All three failed; NO recovery sleep was wasted -- the streak
        # never reached 3 after k2's validation failure reset it.
        self.assertEqual([r.status for r in res],
                         ["failed", "failed", "failed"])
        self.assertEqual(sleeps, [])
        self.assertEqual(p.calls, 4)


# ------------------------------------------------------------- Argon A3
class TestAdaptiveThrottle(unittest.TestCase):
    """[PRE-FAIL] Fixed throttle_seconds = 0.6 was 5x too fast."""

    def test_base_is_conservative(self):
        self.assertEqual(AgnesProvider().throttle_seconds, 6.0)

    def test_doubles_on_rate_limit_signal(self):
        p = AgnesProvider()
        p._on_rate_limited()
        self.assertEqual(p.throttle_seconds, 12.0)
        p._on_rate_limited()
        self.assertEqual(p.throttle_seconds, 24.0)

    def test_caps_at_thirty(self):
        p = AgnesProvider()
        for _ in range(10):
            p._on_rate_limited()
        self.assertEqual(p.throttle_seconds, 30.0)

    def test_decays_toward_base_on_success(self):
        p = AgnesProvider()
        p.throttle_seconds = 30.0
        p._on_success()
        self.assertAlmostEqual(p.throttle_seconds, 27.0)
        for _ in range(50):
            p._on_success()
        self.assertEqual(p.throttle_seconds, 6.0)

    def test_429_then_success_doubles_once(self):
        import urllib.error
        import urllib.request

        calls = {"n": 0}

        class FakeResp:
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "Halo"},
                                                "finish_reason": "stop"}]}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    "https://apihub.agnes-ai.com/v1/chat/completions",
                    429, "Too Many Requests", {}, None)
            return FakeResp()

        orig = urllib.request.urlopen
        orig_sleep = providers_mod._sleep
        urllib.request.urlopen = fake_urlopen
        providers_mod._sleep = lambda s: None
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            p = AgnesProvider()
            p.translate("Hello")
        finally:
            urllib.request.urlopen = orig
            providers_mod._sleep = orig_sleep
            os.environ.pop("AGNES_API_KEY", None)
        # One 429 seen (retry then succeeded) -> doubled once.
        self.assertEqual(p.throttle_seconds, 12.0)


# ------------------------------------------------------------- Argon A4
class TestThrottleSkipAfterRecovery(unittest.TestCase):
    """[PRE-FAIL] The throttle slept right after a 10s/30s recovery
    backoff (redundant) and before the batch's first call."""

    def test_no_throttle_sleep_after_recovery_backoff(self):
        class ThrottledFlaky(BaseProvider):
            name = "throttled-flaky"
            throttle_seconds = 0.6

            def __init__(self):
                self.calls = 0

            def translate(self, text, source_lang="en", target_lang="id",
                          extra_instruction=None):
                self.calls += 1
                if self.calls <= 3:
                    raise TransientProviderError("429")
                return "ID:" + text

        p = ThrottledFlaky()
        orig = engine_mod._sleep
        sleeps = []
        engine_mod._sleep = sleeps.append
        try:
            res = translate_strings(
                {"a": "Game string 1", "b": "Game string 2",
                 "c": "Game string 3"}, provider=p)
        finally:
            engine_mod._sleep = orig
        self.assertEqual(res[2].status, "translated")
        # k0: no sleep (first call). k1: 0.6. k2: 0.6. Recovery: 10.0.
        # k2 retry: NO throttle sleep (backoff already spaced it).
        self.assertEqual(sleeps, [0.6, 0.6, 10.0])


if __name__ == "__main__":
    unittest.main()
