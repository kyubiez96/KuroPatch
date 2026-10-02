"""Tests for the headless UI wrapper."""

from __future__ import annotations

def test_engine_is_actually_created(tmp_path):
    """self.core used to stay None, so check_tools() raised AttributeError."""
    from ui import TranslatorActivity

    activity = TranslatorActivity(
        tools_dir=str(tmp_path / "tools"), output_dir=str(tmp_path / "out")
    )
    try:
        assert activity.core is not None
        assert activity.ui_state["status"] == "idle"
    finally:
        activity.core.clean_workspace()


def test_check_tools_does_not_raise(tmp_path):
    from ui import TranslatorActivity

    activity = TranslatorActivity(
        tools_dir=str(tmp_path / "tools"), output_dir=str(tmp_path / "out")
    )
    try:
        result = activity.check_tools()
        assert isinstance(result, bool)
    finally:
        activity.core.clean_workspace()


def test_select_apk_validates_the_path(tmp_path):
    from ui import TranslatorActivity

    activity = TranslatorActivity(
        tools_dir=str(tmp_path / "tools"), output_dir=str(tmp_path / "out")
    )
    try:
        assert activity.select_apk(str(tmp_path / "missing.apk")) is False
        assert activity.ui_state["status"] == "error"

        real = tmp_path / "game.apk"
        real.write_bytes(b"PK\x03\x04")
        assert activity.select_apk(str(real)) is True
        assert activity.ui_state["status"] == "idle"
    finally:
        activity.core.clean_workspace()


def test_progress_update_survives_zero_total(tmp_path):
    from ui import TranslatorActivity

    activity = TranslatorActivity(
        tools_dir=str(tmp_path / "tools"), output_dir=str(tmp_path / "out")
    )
    try:
        activity.on_progress_update(0, 0)
        assert activity.ui_state["progress"] == 0
        activity.on_progress_update(5, 10)
        assert activity.ui_state["progress"] == 50
    finally:
        activity.core.clean_workspace()


def test_log_buffer_is_bounded(tmp_path):
    from ui import TranslatorActivity

    activity = TranslatorActivity(
        tools_dir=str(tmp_path / "tools"), output_dir=str(tmp_path / "out")
    )
    try:
        for index in range(600):
            activity.on_log_message(f"line {index}")
        assert len(activity.log_lines) <= 500
        assert activity.log_lines[-1] == "line 599"
    finally:
        activity.core.clean_workspace()


def test_describe_lists_the_five_steps():
    from ui import TranslatorActivity

    described = TranslatorActivity.describe()
    assert len(described["steps"]) == 5
    assert described["colors"]["primary"] == "#BB86FC"
    assert "done" in described["states"] and "error" in described["states"]


def test_demo_entry_point_runs(capsys):
    import runpy

    runpy.run_module("ui", run_name="__main__")
    out = capsys.readouterr().out
    assert "engine wired: True" in out
    assert "Select APK" in out
