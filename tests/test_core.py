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
    # apktool 3.x rejects a trailing positional output dir ("Invalid
    # arguments"); the output must travel as -o.
    assert "-o" in captured["cmd"]
    out_index = captured["cmd"].index("-o")
    assert captured["cmd"][out_index + 1] == wired.decompiled_dir
    assert captured["cmd"][-1] == str(apk)


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


# ---------------------------------------------------------------------------
# Output survival
# ---------------------------------------------------------------------------
def _make_signed_apk(path: str, v2: bool = True) -> str:
    """A minimal but structurally valid APK stand-in."""
    import zipfile

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("AndroidManifest.xml", b"<manifest/>")
        archive.writestr("classes.dex", b"\x64\x65\x78")
        archive.writestr("META-INF/CERT.SF", b"sig")
        archive.writestr("META-INF/CERT.RSA", b"sig")
    if v2:
        with open(path, "ab") as handle:
            handle.write(b"APK Sig Block 42")
    return path


def test_output_survives_cleanup_when_output_dir_is_omitted(tmp_path, monkeypatch):
    """Regression: output_dir defaulted into the workspace, so the finished
    APK was deleted while success=True was reported."""
    import core

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        if "apktool" in " ".join(cmd) and "b" in cmd:
            out = cmd[cmd.index("-o") + 1]
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as handle:
                handle.write(b"APK")
        return Result()

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    apk = tmp_path / "game.apk"
    apk.write_bytes(b"PK")
    engine = core.GameTranslator(output_dir=None, keep_workspace=False)
    engine.tools["apktool"] = "/fake/apktool.jar"

    valid = str(tmp_path / "valid.apk")
    _make_signed_apk(valid)
    monkeypatch.setattr(engine, "preflight", lambda *a, **k: [])
    monkeypatch.setattr(engine, "decompile_apk", lambda p: True)
    monkeypatch.setattr(engine, "extract_strings", lambda *a, **k: {"a": "Start Game"})
    monkeypatch.setattr(engine, "translate_strings", lambda s, *a, **k: dict(s))
    monkeypatch.setattr(engine, "patch_strings", lambda *a, **k: True)
    monkeypatch.setattr(engine, "rebuild_apk", lambda *a, **k: valid)
    monkeypatch.setattr(engine, "sign_apk", lambda p: valid)

    result = engine.run_full_pipeline(str(apk))

    assert result["success"] is True
    assert os.path.exists(result["output_apk"]), "finished APK must still exist"
    assert os.path.realpath(result["output_apk"]) != os.path.realpath(engine.workspace)
    engine.clean_workspace()


def test_output_dir_inside_workspace_is_refused(tmp_path):
    import core

    engine = core.GameTranslator(keep_workspace=True)
    with pytest.raises(ValueError, match="inside the temp workspace"):
        engine.resolve_output_dir(
            str(tmp_path / "game.apk"),
            os.path.join(engine.workspace, "out"),
        )
    engine.clean_workspace()


def test_clean_workspace_refuses_to_delete_output(tmp_path, capsys):
    import core

    engine = core.GameTranslator(output_dir=str(tmp_path / "workspace") , keep_workspace=True)
    # Simulate the corrupted state directly.
    engine.output_dir = engine.workspace
    sentinel = os.path.join(engine.workspace, "keep.apk")
    with open(sentinel, "wb") as handle:
        handle.write(b"x")
    engine.clean_workspace()
    assert os.path.exists(sentinel), "output must never be deleted"
    engine.keep_workspace = False
    engine.output_dir = str(tmp_path / "elsewhere")
    engine.clean_workspace()


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
def test_preflight_rejects_missing_and_non_archive(tmp_path):
    import core

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    try:
        assert engine.preflight(str(tmp_path / "nope.apk")) == [
            f"no such file: {tmp_path / 'nope.apk'}"
        ]
        junk = tmp_path / "junk.apk"
        junk.write_bytes(b"not a zip")
        assert engine.preflight(str(junk)) == [f"not a zip archive: {junk}"]
    finally:
        engine.clean_workspace()


def test_preflight_flags_missing_tools(tmp_path, monkeypatch):
    import core

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    monkeypatch.setattr("shutil.which", lambda *a, **k: "/usr/bin/java")
    try:
        apk = tmp_path / "game.apk"
        import zipfile

        with zipfile.ZipFile(apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"x")
        problems = engine.preflight(str(apk))
        assert any("apktool missing" in p for p in problems)
    finally:
        engine.clean_workspace()


def test_preflight_flags_bundle_inputs(tmp_path, monkeypatch):
    import core

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    monkeypatch.setattr("shutil.which", lambda *a, **k: "/usr/bin/java")
    try:
        import zipfile

        bundle = tmp_path / "game.xapk"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("a", b"x")
        problems = engine.preflight(str(bundle))
        assert any("bundle" in p for p in problems)
    finally:
        engine.clean_workspace()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def test_verify_apk_accepts_a_signed_apk(tmp_path):
    import core

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    try:
        path = _make_signed_apk(str(tmp_path / "good.apk"))
        verdict = engine.verify_apk(path)
        assert verdict["ok"] is True
        assert verdict["v1"] and verdict["v2"] and verdict["has_dex"] and verdict["has_manifest"]
    finally:
        engine.clean_workspace()


def test_verify_apk_rejects_an_unsigned_broken_apk(tmp_path):
    import core
    import zipfile

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    try:
        bad = tmp_path / "bad.apk"
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("AndroidManifest.xml", b"<manifest/>")
        verdict = engine.verify_apk(str(bad))
        assert verdict["ok"] is False
        assert any("dex" in p for p in verdict["problems"])
        assert any("v2" in p for p in verdict["problems"])
        assert engine.verify_apk(str(tmp_path / "missing.apk"))["ok"] is False
    finally:
        engine.clean_workspace()


def test_uber_signer_is_used_before_jarsigner(tmp_path, monkeypatch):
    """The jarsigner-only path emits uninstallable output on modern Android;
    uber-apk-signer must take precedence whenever it is available."""
    import core

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        joined = " ".join(cmd)
        if "uber" in joined:
            outdir = cmd[cmd.index("-o") + 1]
            with open(os.path.join(outdir, "x-aligned-debugSigned.apk"), "wb") as handle:
                handle.write(b"APK")
        return Result()

    monkeypatch.setattr(core.subprocess, "run", fake_run)

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    try:
        engine.tools["uber_apk_signer"] = "/tools/uber-apk-signer.jar"
        unsigned = tmp_path / "unsigned.apk"
        unsigned.write_bytes(b"PK")
        signed = engine._sign_with_uber(str(unsigned))
        assert signed.endswith("patched_signed.apk")
        assert os.path.exists(signed)
    finally:
        engine.clean_workspace()


def test_uber_signer_failure_returns_empty(tmp_path, monkeypatch):
    import core

    class Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: Result())

    engine = core.GameTranslator(output_dir=str(tmp_path / "out"), keep_workspace=True)
    try:
        engine.tools["uber_apk_signer"] = "/tools/uber-apk-signer.jar"
        unsigned = tmp_path / "unsigned.apk"
        unsigned.write_bytes(b"PK")
        assert engine._sign_with_uber(str(unsigned)) == ""
    finally:
        engine.clean_workspace()
