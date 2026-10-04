"""Translation engine tests: placeholders, retries, caching, skip heuristics."""

from __future__ import annotations

import json
import os

import pytest

from conftest import FakeProvider
from translator import (
    PassthroughProvider,
    TranslatorEngine,
    build_provider,
    protect_segments,
    restore_segments,
    should_translate,
)


# ---------------------------------------------------------------------------
# Placeholder protection
# ---------------------------------------------------------------------------
def test_printf_and_index_placeholders_are_protected():
    masked, tokens = protect_segments("Hello %s and %1$s of %2$s")
    assert tokens == ["%s", "%1$s", "%2$s"]
    assert "%s" not in masked


def test_braces_tags_entities_and_newlines_are_protected():
    masked, tokens = protect_segments("{0} <b>bold</b> &amp; \\n \\u00e9")
    for token in ("{0}", "<b>", "</b>", "&amp;", "\\n", "\\u00e9"):
        assert token in tokens
    assert "<b>" not in masked


def test_ip_addresses_are_protected():
    _masked, tokens = protect_segments("Server at 192.168.1.10 timed out")
    assert "192.168.1.10" in tokens


def test_restore_round_trips():
    original = "Hi <b>%s</b>, {0} &amp; 10%\nbye"
    masked, tokens = protect_segments(original)
    assert restore_segments(masked, tokens) == original


def test_translation_never_reorders_placeholders():
    """The provider sees only the translatable chunks and cannot move a %s."""
    seen = []

    class RecordingProvider(PassthroughProvider):
        def translate(self, text):
            seen.append(text)
            return text.upper()

    engine = TranslatorEngine(
        cache_file="", provider=RecordingProvider(), delay=0, max_retries=1
    )
    result = engine.translate_detailed("Hello %s, you have {0} new <b>items</b>")
    assert result.status == "done"
    assert result.translated == "HELLO %s, YOU HAVE {0} NEW <b>ITEMS</b>"
    # placeholders never reached the backend
    assert not any("%s" in chunk or "<b>" in chunk for chunk in seen)


def test_escaped_backslash_n_survives_translation():
    engine = TranslatorEngine(cache_file="", provider=PassthroughProvider(), delay=0, max_retries=1)
    result = engine.translate_detailed("Line one\\nline two")
    assert result.translated == "Line one\\nline two"


# ---------------------------------------------------------------------------
# Skip heuristics
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("Start Game", True),
        ("Quit", True),                 # a single capitalised UI word must translate
        ("Pause", True),
        ("Score: %d", True),
        ("", False),
        ("a", False),
        ("100%", False),
        ("2024", False),
        ("https://example.com/x", False),
        ("/data/user/0/com.game", False),
        ("./assets/levels/one.tmx", False),
        ("res/drawable-hdpi/icon.png", False),
        ("menu_start", False),          # identifier punctuation
        ("com.game.internal.Class", False),
        ("loading", False),             # single lowercase token
        # Technical tokens: never UI, and translating a key breaks lookups.
        ("width", False),
        ("height", False),
        ("Width", False),
        ("true", False),
        ("null", False),
        ("utf-8", False),
        ("Screen width", True),         # ...but inside a real sentence it's UI
        # Non-Latin source languages: the old [A-Za-z] checks dropped these.
        ("开始游戏", True),               # Chinese
        ("设置", True),
        ("剑", True),                    # a single Han char is a full morpheme
        ("設定", True),                  # Japanese
        ("はじめから", True),
        ("시작 게임", True),              # Korean
        ("เกม", True),                   # Thai
        ("Level %d 完成", True),          # mixed scripts
    ],
)
def test_should_translate(value, expected):
    assert should_translate(value)[0] is expected


def test_translate_all_disables_heuristics():
    assert should_translate("menu_start", translate_all=True)[0] is True
    assert should_translate("", translate_all=True)[0] is False


# ---------------------------------------------------------------------------
# Errors, retries, caching
# ---------------------------------------------------------------------------
def test_transient_failures_are_retried_then_reported(engine):
    from translator import TranslatorEngine as TE

    flaky = FakeProvider(fail_times=2)
    instance = TE(cache_file="", provider=flaky, delay=0, max_retries=3)
    result = instance.translate_detailed("Start Game")
    assert result.status == "done"
    assert result.translated == "Mulai Game"
    assert len(flaky.calls) == 3


def test_permanent_failure_keeps_the_original_and_records_the_error():
    from translator import TranslatorEngine as TE

    broken = FakeProvider(fail_times=99)
    instance = TE(cache_file="", provider=broken, delay=0, max_retries=2)
    result = instance.translate_detailed("Start Game")
    assert result.status == "error"
    assert result.translated == "Start Game"
    assert "simulated" in result.error
    assert instance.stats["error"] == 1


def test_bare_except_is_gone_so_keyboard_interrupt_propagates():
    from translator import TranslatorEngine as TE

    class Interrupting(PassthroughProvider):
        def translate(self, text):
            raise KeyboardInterrupt

    instance = TE(cache_file="", provider=Interrupting(), delay=0, max_retries=3)
    with pytest.raises(KeyboardInterrupt):
        instance.translate_detailed("Start Game")


def test_cache_is_written_once_per_batch_not_per_string(tmp_path):
    from translator import TranslatorEngine as TE

    cache = tmp_path / "cache.json"
    writes = []
    instance = TE(cache_file=str(cache), provider=FakeProvider(), delay=0, flush_every=1000)
    original = instance._save_cache
    instance._save_cache = lambda: (writes.append(1), original())[1]

    instance.translate_many(["Start Game", "Quit", "Pause", "My Game"])
    instance.close()

    assert len(writes) <= 2, "cache must not be rewritten for every single string"
    assert json.loads(cache.read_text())


def test_cache_hit_avoids_the_provider_entirely(tmp_path):
    from translator import TranslatorEngine as TE

    cache = tmp_path / "cache.json"
    first = FakeProvider()
    one = TE(cache_file=str(cache), provider=first, delay=0)
    one.translate_many(["Start Game", "Quit"])
    one.close()

    second_calls = FakeProvider()
    two = TE(cache_file=str(cache), provider=second_calls, delay=0)
    results = two.translate_many(["Start Game", "Quit"])
    two.close()

    assert second_calls.calls == []
    assert [r.status for r in results] == ["cached", "cached"]


def test_corrupt_cache_does_not_break_a_run(tmp_path):
    from translator import TranslatorEngine as TE

    cache = tmp_path / "cache.json"
    cache.write_text("{not json at all")
    instance = TE(cache_file=str(cache), provider=FakeProvider(), delay=0, max_retries=1)
    assert instance.cache == {}
    assert instance.translate_detailed("Start Game").status == "done"
    instance.close()


def test_interrupted_cache_write_cannot_truncate(tmp_path, monkeypatch):
    """Cache writes go through os.replace, so a crash leaves the old file."""
    from translator import TranslatorEngine as TE

    cache = tmp_path / "cache.json"
    instance = TE(cache_file=str(cache), provider=FakeProvider(), delay=0)
    instance.translate_many(["Start Game"])
    instance.close()
    original = cache.read_text()

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", boom)
    broken = TE(cache_file=str(cache), provider=FakeProvider(), delay=0)
    broken.cache["x"] = "y"
    broken.flush()
    assert cache.read_text() == original
    assert not os.path.exists(f"{cache}.tmp")


def test_results_keep_input_order_with_workers(tmp_path):
    from translator import TranslatorEngine as TE

    instance = TE(cache_file="", provider=FakeProvider(), delay=0)
    texts = ["Start Game", "Quit", "Pause", "My Game", "Settings"]
    results = instance.translate_many(texts, workers=4)
    instance.close()
    assert [r.original for r in results] == texts


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def test_provider_factory_builds_each_backend():
    assert build_provider("none").name == "passthrough"
    assert build_provider("libretranslate", api_key="k").name == "libretranslate"
    with pytest.raises(ValueError):
        build_provider("does-not-exist")


def test_google_v2_requires_a_key():
    with pytest.raises(RuntimeError, match="TRANSLATE_API_KEY"):
        build_provider("google-v2", api_key="")


def test_google_web_reports_a_missing_dependency_clearly():
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "deep_translator":
            raise ImportError("nope")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = blocked
    try:
        with pytest.raises(RuntimeError, match="requirements.txt"):
            build_provider("google-web")
    finally:
        builtins.__import__ = real_import
