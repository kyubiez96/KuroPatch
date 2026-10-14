"""Regression tests for the on_progress callback in translate_strings.

Progress is real per-string completion: the callback fires after EVERY
string (skip / glossary / cache / translated / failed) with
(done, total), done == 1..N, total == N. No timers, no interpolation,
no fake increments. A crashing callback never breaks translation.
"""
import os
import tempfile
import unittest

from cache import TranslationCache
from engine import translate_strings
from providers import BaseProvider


class _RecordingProvider(BaseProvider):
    """Returns a fixed reply per source text."""

    name = "recording"

    def __init__(self, replies):
        self.replies = replies

    def translate(self, text, source_lang="en", target_lang="id",
                  extra_instruction=None):
        return self.replies[text]


class _FakeJavaCallback:
    """Mimics a Chaquopy Java object: not callable, has onProgress."""

    def __init__(self):
        self.calls = []

    def onProgress(self, done, total):
        self.calls.append((done, total))


class TestOnProgress(unittest.TestCase):
    def test_mixed_batch_reports_every_string(self):
        replies = {
            "Hello world": "Halo dunia",
            "Game string one": "Satu string game",
            "You earned %d gold": "Kamu dapat emas",  # drops %d -> failed
        }
        with tempfile.TemporaryDirectory() as d:
            cache = TranslationCache(os.path.join(d, "c.json"))
            p = _RecordingProvider(replies)
            # Pre-populate the cache so "Hello world" is a cache hit.
            translate_strings({"h": "Hello world"}, provider=p, cache=cache)
            calls = []
            batch = {
                "k1": "1080x1920",            # structural skip (dimension)
                "k2": "Monday",                # fixed-map -> Senin
                "k3": "Hello world",          # cache hit
                "k4": "Game string one",      # provider translated
                "k5": "You earned %d gold",   # validation fails -> failed
                "k6": "https://example.com/x",  # structural skip (url)
            }
            res = translate_strings(
                batch, provider=p, cache=cache,
                on_progress=lambda done, total: calls.append((done, total)))
        self.assertEqual([c[0] for c in calls], [1, 2, 3, 4, 5, 6])
        self.assertEqual([c[1] for c in calls], [6] * 6)
        self.assertEqual(
            [r.status for r in res],
            ["skipped", "translated", "cached",
             "translated", "failed", "skipped"])

    def test_raising_callback_never_breaks_batch(self):
        calls = []

        def boom(done, total):
            calls.append((done, total))
            raise RuntimeError("callback exploded")

        p = _RecordingProvider({
            "Alpha one": "Satu alfa",
            "Beta two": "Dua beta",
            "Gamma three": "Tiga gama",
        })
        res = translate_strings(
            {"a": "Alpha one", "b": "Beta two", "c": "Gamma three"},
            provider=p, on_progress=boom)
        self.assertEqual([r.status for r in res], ["translated"] * 3)
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])

    def test_java_style_onprogress_object(self):
        cb = _FakeJavaCallback()
        p = _RecordingProvider({"Alpha one": "Satu alfa",
                                "Beta two": "Dua beta"})
        translate_strings({"a": "Alpha one", "b": "Beta two"},
                          provider=p, on_progress=cb)
        self.assertEqual(cb.calls, [(1, 2), (2, 2)])

    def test_no_callback_unchanged(self):
        p = _RecordingProvider({"Alpha one": "Satu alfa"})
        res = translate_strings({"k": "Alpha one"}, provider=p)
        self.assertEqual(res[0].status, "translated")
        self.assertEqual(res[0].translation, "Satu alfa")

    def test_empty_batch_no_calls(self):
        calls = []
        res = translate_strings(
            {}, on_progress=lambda done, total: calls.append((done, total)))
        self.assertEqual(res, [])
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
