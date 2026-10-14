"""Verification: v2.0.11 on-device abort diagnosis + fix behaviour.

On-device (Kovak, Castlevania v2.0.11): "26 strings translated,
78 unchanged, 327 failed". Claimed mechanism: 3 consecutive provider
exceptions -> provider_dead -> remaining provider-bound strings marked
failed; claimed trigger: free-tier rate limit (429) under a 431-string
rapid-fire batch with NO throttle.

What is PROVEN here (all from code + these tests):
  1. The abort mechanism reproduces the EXACT on-device counts
     (26 / 78 / 327) when the provider raises a PERMANENT error.
  2. first_error distinguishes the trigger (429 vs 401 vs network),
     so surfacing it ungated is what makes the true cause visible.
  3. End-to-end termination with the REAL AgnesProvider + always-429
     urlopen: bounded calls (9 provider calls / 36 HTTP attempts /
     2 recoveries), then provider_dead. No infinite loop.
  4. No key leak: with a dummy key installed via set_api_key, the key
     appears in NO result detail, NO first_error, NO raised message.

What is NOT proven (honest scope): the trigger itself. 429 / 401 /
network-down / 400 all produce identical UI counts -- only first_error
distinguishes them, and on-device v2.0.11 the OLD Kotlin dialog gate
(failed > 0 && changed == 0) SUPPRESSED first_error because changed
(26) > 0. The 429 attribution is plausible (431 strings, no throttle,
~46 calls/min observed) but NOT proven from counts alone.

OPEN ADVERSARIAL FINDING (throttle value): the fix paces calls at
throttle_seconds = 0.6 (100 calls/min). Behaviour verified (engine
sleeps before every provider call). But the VALUE is unjustified
against any stated free-tier limit: if apihub's limit is ~20 RPM,
0.6s is 5x too fast and the batch will still burn both recoveries
then mass-fail (delayed, not prevented). The value needs apihub's
documented limit, or the throttle must adapt (double on each 429).
"""

import os
import unittest

import engine as engine_mod
import providers as providers_mod
from engine import results_summary, translate_strings
from providers import AgnesProvider, BaseProvider


class _AlwaysRaisingProvider(BaseProvider):
    """Provider that raises the given exception on every call."""

    name = "always-raising"

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def translate(self, text, source_lang="en", target_lang="id"):
        self.calls += 1
        raise self.exc


class TestAbortReproducesOnDeviceCounts(unittest.TestCase):
    def test_26_78_327(self):
        # Mirror the on-device batch: 26 fixed-map hits + 78 guard
        # skips + 327 provider-bound strings, provider raises a
        # PERMANENT error on every call. The engine must produce
        # EXACTLY the on-device counts with only 3 provider calls
        # (abort, no recovery for permanent errors).
        # NOTE: "May" is NOT in the fixed list -- it is correctly
        # guard-skipped as the Malay language code (ISO 639 "may").
        fixed = [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
            "Saturday", "Sunday", "January", "February", "March",
            "April", "June", "July", "August", "September",
            "October", "November", "Yes", "Cancel", "Continue",
            "Settings", "About", "Exit", "Start", "Pause", "Resume",
        ]
        self.assertEqual(len(fixed), 26)
        skipped = [f"http://example.com/page{i}" for i in range(78)]
        bound = [f"Normal game string number {i}" for i in range(327)]
        strings = {}
        for i, t in enumerate(fixed):
            strings[f"f{i}"] = t
        for i, t in enumerate(skipped):
            strings[f"s{i}"] = t
        for i, t in enumerate(bound):
            strings[f"b{i}"] = t
        provider = _AlwaysRaisingProvider(RuntimeError(
            "agnes provider request failed: "
            "HTTP Error 401: Unauthorized"))
        res = translate_strings(strings, provider=provider)
        summary = results_summary(res)
        self.assertEqual(provider.calls, 3)
        self.assertEqual(summary["translated"], 26)
        self.assertEqual(summary["skipped"], 78)
        self.assertEqual(summary["failed"], 327)
        self.assertIn("401", summary["first_error"])
        self.assertIn("aborted", res[-1].detail)


class TestFirstErrorDistinguishesTrigger(unittest.TestCase):
    def _first_error(self, exc):
        strings = {f"k{i}": f"Game string {i}" for i in range(5)}
        res = translate_strings(
            strings, provider=_AlwaysRaisingProvider(exc))
        return results_summary(res)["first_error"]

    def test_429_vs_401_distinguishable(self):
        err429 = self._first_error(RuntimeError(
            "agnes provider request failed: "
            "HTTP Error 429: Too Many Requests"))
        err401 = self._first_error(RuntimeError(
            "agnes provider request failed: "
            "HTTP Error 401: Unauthorized"))
        errnet = self._first_error(RuntimeError(
            "agnes provider request failed: "
            "<urlopen error [Errno 111] Connection refused>"))
        self.assertIn("429", err429)
        self.assertIn("401", err401)
        self.assertIn("Connection refused", errnet)


class TestRetryTerminatesEndToEnd(unittest.TestCase):
    def test_always_429_bounded_attempts(self):
        # Real AgnesProvider + urlopen that always 429s. Trace:
        # 3 consecutive transient -> recovery #1 (10s) -> 3 more
        # -> recovery #2 (30s) -> 3 more -> provider_dead.
        # = 9 provider calls, 36 HTTP attempts (4 per call),
        # then stop. No infinite loop. Both _sleep hooks patched
        # (providers._sleep for _fetch retries, engine._sleep for
        # recovery backoffs) so the test stays instant.
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
            strings = {f"k{i}": f"Game string {i}" for i in range(10)}
            res = translate_strings(strings, provider=AgnesProvider())
        finally:
            urllib.request.urlopen = orig_urlopen
            providers_mod._sleep = orig_psleep
            engine_mod._sleep = orig_esleep
            os.environ.pop("AGNES_API_KEY", None)
        self.assertEqual(len(attempts), 36)
        self.assertEqual(provider_sleeps, [1, 2, 4] * 9)
        # Engine sleeps: throttle 0.6 before each of the 9 provider
        # calls + recovery backoffs 10, 30, in call order.
        self.assertEqual(engine_sleeps,
                         [0.6, 0.6, 0.6, 10.0,
                          0.6, 0.6, 0.6, 30.0,
                          0.6, 0.6, 0.6])
        summary = results_summary(res)
        self.assertEqual(summary["failed"], 10)
        self.assertIn("429", summary["first_error"])

    def test_engine_honors_real_provider_throttle(self):
        # The throttle must apply to the REAL provider, not just
        # mocks: engine sleeps throttle_seconds before the call.
        import json
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

        payload = {"choices": [{"message": {"content": "Halo"},
                                "finish_reason": "stop"}]}
        orig_urlopen = urllib.request.urlopen
        orig_esleep = engine_mod._sleep
        orig_psleep = providers_mod._sleep
        engine_sleeps = []
        urllib.request.urlopen = lambda req, timeout=None: FakeResp(payload)
        engine_mod._sleep = engine_sleeps.append
        providers_mod._sleep = lambda s: None
        try:
            os.environ["AGNES_API_KEY"] = "dummy-for-test"
            res = translate_strings(
                {"k0": "Hello world"}, provider=AgnesProvider())
        finally:
            urllib.request.urlopen = orig_urlopen
            engine_mod._sleep = orig_esleep
            providers_mod._sleep = orig_psleep
            os.environ.pop("AGNES_API_KEY", None)
        self.assertEqual(res[0].status, "translated")
        self.assertEqual(engine_sleeps, [0.6])


class TestNoKeyLeakInErrors(unittest.TestCase):
    KEY = "sk-DUMMY-SECRET-KEY-12345"

    def setUp(self):
        providers_mod.set_api_key(self.KEY)

    def tearDown(self):
        providers_mod.set_api_key(None)
        os.environ.pop("AGNES_API_KEY", None)

    def test_key_nowhere_in_details_or_first_error(self):
        # Every failure mode: 429 (transient), 401 (permanent),
        # 500 (transient), network error. The key must appear in
        # NO result detail and NO first_error.
        import urllib.error
        import urllib.request

        orig_urlopen = urllib.request.urlopen
        orig_psleep = providers_mod._sleep
        orig_esleep = engine_mod._sleep
        providers_mod._sleep = lambda s: None
        engine_mod._sleep = lambda s: None
        try:
            for code in (429, 401, 500):
                def fake_urlopen(req, timeout=None, _code=code):
                    raise urllib.error.HTTPError(
                        "https://apihub.agnes-ai.com/v1/chat/completions",
                        _code, "Error", {}, None)
                urllib.request.urlopen = fake_urlopen
                strings = {f"k{i}": f"Game string {i}" for i in range(4)}
                res = translate_strings(strings, provider=AgnesProvider())
                for r in res:
                    self.assertNotIn(self.KEY, r.detail)
                self.assertNotIn(
                    self.KEY, results_summary(res)["first_error"])

            def fake_network(req, timeout=None):
                raise urllib.error.URLError("connection refused")
            urllib.request.urlopen = fake_network
            res = translate_strings(
                {"k0": "Game string 0"}, provider=AgnesProvider())
            for r in res:
                self.assertNotIn(self.KEY, r.detail)
            self.assertNotIn(
                self.KEY, results_summary(res)["first_error"])
        finally:
            urllib.request.urlopen = orig_urlopen
            providers_mod._sleep = orig_psleep
            engine_mod._sleep = orig_esleep

    def test_fetch_error_message_contains_no_key(self):
        import urllib.error
        import urllib.request

        orig_urlopen = urllib.request.urlopen
        orig_psleep = providers_mod._sleep
        orig_esleep = engine_mod._sleep
        providers_mod._sleep = lambda s: None
        engine_mod._sleep = lambda s: None
        try:
            def fake_urlopen(req, timeout=None):
                # Prove the request itself carries the key (it must,
                # as Authorization) while the ERROR never echoes it.
                auth = req.get_header("Authorization")
                assert auth is not None and self.KEY in auth
                raise urllib.error.HTTPError(
                    "https://apihub.agnes-ai.com/v1/chat/completions",
                    401, "Unauthorized", {}, None)
            urllib.request.urlopen = fake_urlopen
            with self.assertRaises(RuntimeError) as ctx:
                AgnesProvider().translate("Hello", "en", "id")
            self.assertNotIn(self.KEY, str(ctx.exception))
        finally:
            urllib.request.urlopen = orig_urlopen
            providers_mod._sleep = orig_psleep
            engine_mod._sleep = orig_esleep


if __name__ == "__main__":
    unittest.main()
