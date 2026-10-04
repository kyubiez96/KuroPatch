"""Tests for the Android bridge (app/src/main/python/android_bridge.py).

The bridge is what Chaquopy calls on-device. It imports nothing
Android-specific, so the whole pipeline path is exercised here with a fake
listener — fully offline, no JDK, no network.
"""

from __future__ import annotations

import os
import sys
import zipfile

import pytest

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app",
        "src",
        "main",
        "python",
    ),
)

import android_bridge  # noqa: E402
from translator import TranslatorEngine  # noqa: E402

from conftest import FakeProvider  # noqa: E402


class FakeListener:
    """Records every callback the bridge makes."""

    def __init__(self) -> None:
        self.statuses = []
        self.progress = []
        self.logs = []
        self.done = None

    def on_status(self, status, message):
        self.statuses.append((status, message))

    def on_progress(self, done, total):
        self.progress.append((done, total))

    def on_log(self, line):
        self.logs.append(line)

    def on_done(self, success, output_path, message):
        self.done = (success, output_path, message)


@pytest.fixture
def tiny_jar(tmp_path):
    """A minimal .jar with one .properties file and one smali file."""
    jar = tmp_path / "game.jar"
    with zipfile.ZipFile(jar, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "assets/messages.properties",
            "welcome=Welcome\nquit=Quit\n",
        )
        archive.writestr(
            "smali/com/game/Strings.smali",
            '.class public Lcom/game/Strings;\n'
            'const-string v0, "Press start to begin"\n',
        )
    return str(jar)


@pytest.fixture
def translating_engine(monkeypatch):
    """Make the bridge translate with the deterministic FakeProvider."""
    real_engine = TranslatorEngine

    def factory(**kwargs):
        kwargs["provider"] = FakeProvider()
        return real_engine(**kwargs)

    monkeypatch.setattr(android_bridge, "TranslatorEngine", factory)


def _run(tmp_path, jar, **kwargs):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    listener = FakeListener()
    params = dict(
        input_path=jar,
        output_path=str(out_dir / "game_ID.jar"),
        report_dir=str(out_dir),
        source="en",
        target="id",
        provider="none",
        workspace_dir=str(tmp_path / "work"),
        listener=listener,
    )
    params.update(kwargs)
    result = android_bridge.run_patch(**params)
    return result, listener


def test_full_run_rebuilds_jar(tmp_path, tiny_jar, translating_engine):
    result, listener = _run(tmp_path, tiny_jar)

    assert result["success"] is True
    assert result["output_path"] and os.path.isfile(result["output_path"])
    assert result["total"] == 3  # welcome, quit, "Press start to begin"
    assert result["translated"] == 3
    assert listener.done is not None
    assert listener.done[0] is True
    assert listener.done[1] == result["output_path"]
    # Progress reached 100%.
    assert listener.progress[-1] == (3, 3)
    # Statuses walked the pipeline phases.
    phases = [status for status, _ in listener.statuses]
    assert "extracting" in phases
    assert "translating" in phases
    assert "patching" in phases
    assert "done" in phases

    # The translation actually landed in the rebuilt archive.
    with zipfile.ZipFile(result["output_path"]) as archive:
        props = archive.read("assets/messages.properties").decode("utf-8")
    assert "Selamat datang" in props
    assert "Keluar" in props


def test_dry_run_writes_reports_but_no_archive(tmp_path, tiny_jar, translating_engine):
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    listener = FakeListener()
    result = android_bridge.run_patch(
        input_path=tiny_jar,
        output_path=str(out_dir / "game_ID.jar"),
        report_dir=str(out_dir),
        provider="none",
        dry_run=True,
        workspace_dir=str(tmp_path / "work"),
        listener=listener,
    )

    assert result["success"] is True
    assert result["output_path"] == ""
    assert not os.path.exists(out_dir / "game_ID.jar")
    assert os.path.isfile(out_dir / "found_strings.txt")
    assert os.path.isfile(out_dir / "translated_strings.txt")
    assert listener.done[0] is True


def test_missing_input_reports_failure(tmp_path):
    listener = FakeListener()
    result = android_bridge.run_patch(
        input_path=str(tmp_path / "nope.jar"),
        output_path=str(tmp_path / "out.jar"),
        report_dir=str(tmp_path),
        listener=listener,
    )

    assert result["success"] is False
    assert "No such file" in result["message"]
    assert listener.done[0] is False
    assert any(status == "error" for status, _ in listener.statuses)


def test_broken_listener_never_breaks_pipeline(tmp_path, tiny_jar, translating_engine):
    """A listener that raises on every callback must not fail the run."""

    class ExplodingListener:
        def __getattr__(self, _name):
            def boom(*_args):
                raise RuntimeError("ui is broken")

            return boom

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = android_bridge.run_patch(
        input_path=tiny_jar,
        output_path=str(out_dir / "game_ID.jar"),
        report_dir=str(out_dir),
        provider="none",
        workspace_dir=str(tmp_path / "work"),
        listener=ExplodingListener(),
    )
    assert result["success"] is True
    assert os.path.isfile(result["output_path"])
