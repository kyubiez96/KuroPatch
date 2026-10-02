"""Core engine tests: extraction, patching, locale safety, queue accounting."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import pytest

from conftest import FakeProvider


@pytest.fixture
def wired(decompiled_tree, monkeypatch):
    """A GameTranslator whose translation backend is the fake provider."""
    import core
    import translator

    monkeypatch.setattr(
        translator, "build_provider", lambda *a, **k: FakeProvider(*a[:2])
    )
    decompiled_tree.target_lang = "id"
    decompiled_tree.engine = translator.TranslatorEngine(
        cache_file="", provider=FakeProvider(), delay=0, max_retries=1
    )
    return decompiled_tree


def read(path) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Queue accounting
# ---------------------------------------------------------------------------
def test_queue_counters_never_go_negative():
    from core import TranslationJob, TranslationQueue

    queue = TranslationQueue()
    queue.add(TranslationJob(key="a", original="a"))
    queue.add(TranslationJob(key="b", original="b"))
    queue.mark_start("a")
    queue.mark_start("b")
    assert queue.get_stats()["in_progress"] == 2

    queue.mark_done("a", "A")
    stats = queue.get_stats()
    assert stats["completed"] == 1
    assert stats["in_progress"] == 1
    assert stats["pending"] == 0

    queue.mark_done("b", "B", error="boom")
    stats = queue.get_stats()
    assert stats["completed"] == 2
    assert stats["pending"] == 0
    assert stats["error"] == 1


def test_completing_an_unknown_key_is_ignored():
    from core import TranslationQueue

    queue = TranslationQueue()
    queue.mark_done("ghost", "x")
    assert queue.get_stats() == {
        "completed": 0, "total": 0, "in_progress": 0, "pending": 0,
        "done": 0, "error": 0, "skipped": 0, "items": [],
    }


def test_double_completion_counts_once():
    from core import TranslationJob, TranslationQueue

    queue = TranslationQueue()
    queue.add(TranslationJob(key="a", original="a"))
    queue.mark_start("a")
    queue.mark_done("a", "A")
    queue.mark_done("a", "AGAIN")
    assert queue.get_stats()["completed"] == 1


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def test_decompile_command_decodes_resources(wired, monkeypatch, tmp_path):
    """apktool -r skips resource decoding, so strings.xml never existed."""
    captured = {}
    apk = tmp_path / "game.apk"
    apk.write_bytes(b"PK\x03\x04")

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        os.makedirs(wired.decompiled_dir, exist_ok=True)
        return Result()

    monkeypatch.setattr("core.subprocess.run", fake_run)
    wired.tools["apktool"] = "/tools/apktool.jar"

    assert wired.decompile_apk(str(apk)) is True
    assert "-r" not in captured["cmd"]
    assert "-s" not in captured["cmd"]
    assert "d" in captured["cmd"]


def test_decompile_without_apktool_fails_cleanly(wired):
    wired.tools["apktool"] = None
    assert wired.decompile_apk("/tmp/game.apk") is False


def test_rebuild_without_apktool_does_not_crash(wired):
    """The old code called " ".join(cmd) with a None element -> TypeError."""
    wired.tools["apktool"] = None
    assert wired.rebuild_apk("/tmp/game.apk") == ""


def test_extract_finds_resource_strings_and_smali_literals(wired):
    strings = wired.extract_strings()
    values = set(strings.values())
    assert "Start Game" in values
    assert "My Game" in values
    assert "Welcome, <b>%s</b>!" in values
    kinds = {source.kind for source in wired.sources}
    assert kinds == {"xml", "smali"}


def test_class_descriptors_are_not_treated_as_strings(wired):
    wired.extract_strings()
    assert "Lcom/game/Main;" not in {source.value for source in wired.sources}


def test_entities_are_decoded_once(wired):
    strings = wired.extract_strings()
    assert strings["amp"] == "Tom & Jerry say <hi>"


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------
def test_patch_preserves_markup_entities_and_comments(wired):
    strings = wired.extract_strings()
    wired.patch_strings(strings, wired.translate_strings(strings))

    out = read(os.path.join(wired.decompiled_dir, "res/values/strings.xml"))
    assert "<!-- a comment that must survive -->" in out
    assert "<b>%s</b>" in out          # inline tag stays a tag
    assert "<xliff:g id=\"name\"" in out  # namespaced placeholder survives
    assert "Tom &amp; Jerry say &lt;hi&gt;" in out  # escaped exactly once
    assert '<string name="btn_start">Mulai Game</string>' in out
    ET.parse(os.path.join(wired.decompiled_dir, "res/values/strings.xml"))


def test_patch_leaves_other_locales_untouched(wired):
    """The old code wrote every translation into every values-* folder."""
    before = read(os.path.join(wired.decompiled_dir, "res/values-ru/strings.xml"))
    strings = wired.extract_strings()
    wired.patch_strings(strings, wired.translate_strings(strings))
    after = read(os.path.join(wired.decompiled_dir, "res/values-ru/strings.xml"))
    assert before == after
    assert "\u041d\u0430\u0447\u0430\u0442\u044c" in after


def test_target_locale_file_is_created_and_populated(wired):
    strings = wired.extract_strings()
    wired.patch_strings(strings, wired.translate_strings(strings))

    target = os.path.join(wired.decompiled_dir, "res/values-id/strings.xml")
    assert os.path.exists(target), "the values-<lang> file must be written"
    out = read(target)
    assert '<string name="btn_start">Mulai Game</string>' in out
    ET.parse(target)


def test_smali_literals_are_written_back(wired):
    """They used to be extracted, translated, counted and then dropped."""
    strings = wired.extract_strings()
    wired.patch_strings(strings, wired.translate_strings(strings))
    out = read(os.path.join(wired.decompiled_dir, "smali/com/game/Main.smali"))
    assert 'const-string v0, "Mulai Game"' in out
    assert 'const-string/jumbo v3, "Keluar"' in out
    assert 'const-string v4, "com.game.internal.Class"' in out


def test_patching_with_no_translations_is_a_no_op(wired):
    strings = wired.extract_strings()
    before = read(os.path.join(wired.decompiled_dir, "res/values/strings.xml"))
    wired.translations = {}
    assert wired.patch_strings(strings, {}) is True
    assert read(os.path.join(wired.decompiled_dir, "res/values/strings.xml")) == before


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def test_setup_tools_falls_back_to_the_sdk(monkeypatch, tmp_path):
    import core

    engine = core.GameTranslator(keep_workspace=True)
    monkeypatch.setenv("ANDROID_SDK_ROOT", str(tmp_path))
    (tmp_path / "build-tools" / "34.0.0").mkdir(parents=True)
    (tmp_path / "build-tools" / "34.0.0" / "apksigner").write_text("#!/bin/sh\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "apktool").write_text("x")

    assert engine.setup_tools(str(tools)) is True
    assert engine.tools["apksigner"].endswith("34.0.0/apksigner")
    engine.clean_workspace()


def test_setup_tools_fails_only_when_apktool_is_missing(tmp_path):
    import core

    engine = core.GameTranslator(keep_workspace=True)
    assert engine.setup_tools(str(tmp_path / "empty")) is False
    engine.clean_workspace()


def test_jarsigner_fallback_uses_sha256():
    """SHA1withRSA has been rejected by Android since API 18."""
    import inspect

    import core

    source = inspect.getsource(core.GameTranslator.sign_apk)
    assert "SHA1withRSA" not in source
    assert "SHA256withRSA" in source
    assert "SHA-256" in source


def test_report_lists_every_string(wired, tmp_path):
    strings = wired.extract_strings()
    wired.translate_strings(strings)
    report = wired.write_report(str(tmp_path / "found_strings.txt"))
    out = read(report)
    assert "Start Game" in out
    assert "Mulai Game" in out
