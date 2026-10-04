"""Tests for the apk.py CLI (GameTranslator is stubbed, no JDK needed)."""

from __future__ import annotations

import os
import zipfile

import pytest


def _make_apk(path: str) -> str:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"<manifest/>")
        archive.writestr("classes.dex", b"dex")
    return path


@pytest.fixture
def fake_engine(monkeypatch):
    import apk as cli_module
    import core

    class Engine:
        instances = []

        def __init__(self, *args, **kwargs):
            Engine.instances.append(self)
            self.calls = []
            self.source_lang = "en"
            self.target_lang = "id"
            self.workers = 4
            self.max_retries = 3
            self.request_delay = 0.35
            self.api_key = ""
            self.engine = None
            self.keystore_path = None
            self.alias = "androiddebugkey"
            self.keystore_pass = "android"
            self.key_pass = "android"
            self.tools = {"apktool": "/fake/apktool.jar"}
            self.sources = []

        def set_callbacks(self, **kwargs):
            self.calls.append(("callbacks", kwargs))

        def setup_tools(self, tools_dir):
            self.calls.append(("setup_tools", tools_dir))
            return True

        def download_tools(self, tools_dir):
            self.calls.append(("download_tools", tools_dir))
            return {"apktool": True}

        def preflight(self, apk, tools_dir=None):
            self.calls.append(("preflight", apk))
            return []

        def resolve_output_dir(self, apk, output=None):
            self.calls.append(("resolve_output_dir", apk))
            return output or "/tmp/out"

        def decompile_apk(self, apk):
            self.calls.append(("decompile_apk", apk))
            return True

        def extract_strings(self):
            self.calls.append(("extract",))
            return {"a": "Start Game"}

        def write_report(self, path=None):
            self.calls.append(("report", path))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("# report\n")
            return path

        def run_full_pipeline(self, apk, output=None, translate_all=False):
            self.calls.append(("pipeline", apk, output, translate_all))
            verdict = {"size": 100, "v1": True, "v2": True}
            return {
                "success": True,
                "output_apk": "/tmp/out/patched.apk",
                "stats": {"done": 3, "cached": 0, "error": 0, "items": []},
                "errors": [],
                "report": "/tmp/out/found_strings.txt",
                "verification": verdict,
            }

        def clean_workspace(self):
            pass

    monkeypatch.setattr(cli_module, "GameTranslator", Engine)
    monkeypatch.setattr(core, "GameTranslator", Engine)
    Engine.instances.clear()
    return Engine


def test_full_run_succeeds(fake_engine, tmp_path, capsys):
    import apk as cli_module

    apk = _make_apk(str(tmp_path / "game.apk"))
    out = str(tmp_path / "out")
    assert cli_module.main([apk, "-t", "id", "-o", out, "--tools", str(tmp_path / "tools")]) == 0
    text = capsys.readouterr().out
    assert "Patched APK" in text
    engine = fake_engine.instances[0]
    assert engine.target_lang == "id"
    assert ("pipeline", apk, out, False) in engine.calls


def test_dry_run_lists_but_builds_nothing(fake_engine, tmp_path, capsys):
    import apk as cli_module

    apk = _make_apk(str(tmp_path / "game.apk"))
    out = str(tmp_path / "out")
    assert cli_module.main([apk, "--dry-run", "-o", out, "--tools", str(tmp_path / "tools")]) == 0
    engine = fake_engine.instances[0]
    assert ("decompile_apk", apk) in engine.calls
    assert ("extract",) in engine.calls
    assert not any(call[0] == "pipeline" for call in engine.calls)
    assert "Dry run" in capsys.readouterr().out


def test_missing_input_is_a_clean_error(fake_engine, tmp_path):
    import apk as cli_module

    assert cli_module.main([str(tmp_path / "nope.apk")]) == 1


def test_non_archive_is_a_clean_error(fake_engine, tmp_path):
    import apk as cli_module

    bogus = tmp_path / "x.apk"
    bogus.write_bytes(b"nope")
    assert cli_module.main([str(bogus)]) == 1


def test_preflight_failure_blocks_the_run(fake_engine, tmp_path, monkeypatch):
    import apk as cli_module

    real_preflight = fake_engine.preflight

    def blocked(self, apk, tools_dir=None):
        return ["java not found on PATH"]

    monkeypatch.setattr(fake_engine, "preflight", blocked)
    apk = _make_apk(str(tmp_path / "game.apk"))
    assert cli_module.main([apk, "-o", str(tmp_path / "out"), "--tools", str(tmp_path / "t")]) == 1


def test_failed_pipeline_returns_one(fake_engine, tmp_path, monkeypatch):
    import apk as cli_module

    def broken(self, apk, output=None):
        return {"success": False, "errors": ["boom"], "stats": {}, "report": "", "verification": {}}

    monkeypatch.setattr(fake_engine, "run_full_pipeline", broken)
    apk = _make_apk(str(tmp_path / "game.apk"))
    assert cli_module.main([apk, "-o", str(tmp_path / "out"), "--tools", str(tmp_path / "t")]) == 1


def test_download_happens_when_tools_are_missing(fake_engine, tmp_path, monkeypatch):
    import apk as cli_module

    def no_tools(self, tools_dir):
        self.calls.append(("setup_tools", tools_dir))
        return False

    monkeypatch.setattr(fake_engine, "setup_tools", no_tools)
    apk = _make_apk(str(tmp_path / "game.apk"))
    assert cli_module.main([apk, "-o", str(tmp_path / "out"), "--tools", str(tmp_path / "t")]) == 0
    engine = fake_engine.instances[0]
    assert ("download_tools", str(tmp_path / "t")) in engine.calls


def test_no_download_flag_is_honored(fake_engine, tmp_path, monkeypatch):
    import apk as cli_module

    monkeypatch.setattr(fake_engine, "setup_tools", lambda self, d: False)
    apk = _make_apk(str(tmp_path / "game.apk"))
    assert cli_module.main([apk, "-o", str(tmp_path / "out"),
                            "--tools", str(tmp_path / "t"), "--no-download"]) == 1


def test_keystore_options_reach_the_engine(fake_engine, tmp_path):
    import apk as cli_module

    apk = _make_apk(str(tmp_path / "game.apk"))
    ks = tmp_path / "me.keystore"
    ks.write_bytes(b"ks")
    cli_module.main([apk, "-o", str(tmp_path / "out"), "--tools", str(tmp_path / "t"),
                     "--keystore", str(ks), "--ks-alias", "me", "--ks-pass", "p1"])
    engine = fake_engine.instances[0]
    assert engine.keystore_path == str(ks)
    assert engine.alias == "me"
    assert engine.keystore_pass == "p1"
