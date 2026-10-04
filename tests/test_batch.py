"""Tests for batched translation (TranslatorEngine.translate_many).

Batching sends multiple translatable chunks per provider call instead of one
HTTP request per string, which is what made 3000+ string packs look frozen.
"""

import os
import sys
from typing import List

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from translator import BaseProvider, TranslatorEngine


class BatchProvider(BaseProvider):
    name = "test-batch"

    def __init__(self):
        super().__init__("en", "id")
        self.batch_calls = 0
        self.single_calls = 0

    def translate(self, text: str) -> str:
        self.single_calls += 1
        return f"[{text}]"

    def translate_batch(self, texts: List[str]) -> List[str]:
        self.batch_calls += 1
        return [f"[{t}]" for t in texts]


class SingleOnlyProvider(BaseProvider):
    """No translate_batch override: default must loop over translate()."""

    name = "test-single"

    def __init__(self):
        super().__init__("en", "id")
        self.calls = 0

    def translate(self, text: str) -> str:
        self.calls += 1
        return f"<{text}>"


def _engine(provider):
    return TranslatorEngine(provider=provider, cache_file="", translate_all=True)


def test_batching_reduces_provider_calls():
    provider = BatchProvider()
    engine = _engine(provider)
    texts = [f"Hello world number {i}" for i in range(50)]
    results = engine.translate_many(texts, batch_size=20)
    # 50 single-chunk strings / 20 per batch -> 3 provider calls, not 50.
    assert provider.batch_calls == 3
    assert provider.single_calls == 0
    assert len(results) == 50
    assert all(r.status == "done" for r in results)
    assert results[0].translated == "[Hello world number 0]"


def test_batching_preserves_placeholders():
    provider = BatchProvider()
    engine = _engine(provider)
    results = engine.translate_many(
        ["\\mSir\\m\\fMa'am\\f, you alright?]Hello"], batch_size=20
    )
    out = results[0].translated
    assert "\\m" in out and "\\f" in out and "]" in out
    assert "Sir" in out  # translatable text went through the mock


def test_default_batch_falls_back_to_single_calls():
    provider = SingleOnlyProvider()
    engine = _engine(provider)
    results = engine.translate_many(["alpha", "beta", "gamma"], batch_size=20)
    assert provider.calls == 3
    assert all(r.status == "done" for r in results)


def test_on_result_fires_for_every_string():
    provider = BatchProvider()
    engine = _engine(provider)
    seen: List[str] = []
    results = engine.translate_many(
        [f"text number {i}" for i in range(250)],
        batch_size=20,
        group_size=100,
        on_result=lambda r: seen.append(r.original),
    )
    assert len(seen) == 250
    assert len(results) == 250


def test_batch_failure_marks_error_not_crash():
    class FailProvider(BaseProvider):
        name = "test-fail"

        def __init__(self):
            super().__init__("en", "id")

        def translate(self, text: str) -> str:
            raise RuntimeError("boom")

    engine = TranslatorEngine(
        provider=FailProvider(), cache_file="", translate_all=True, max_retries=1
    )
    results = engine.translate_many(["hello world"], batch_size=20)
    assert results[0].status == "error"
    assert engine.stats["error"] == 1
