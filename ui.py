#!/usr/bin/env python3
"""Thin UI wrapper around :class:`core.GameTranslator`.

This is the headless part of the UI: it owns the state machine, the thread
that runs the pipeline and the callbacks the widgets bind to. The original
version left ``self.core`` as ``None`` forever, so ``check_tools()`` and
``run_pipeline()`` both raised ``AttributeError`` the moment they were used.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, List, Optional

from core import GameTranslator

# Statuses the UI knows how to render.
STATES = ("idle", "selecting", "downloading", "decompiling", "extracting",
          "translating", "patching", "rebuilding", "signing", "done", "error")


class TranslatorActivity:
    """Main activity for game translator app"""

    def __init__(self, tools_dir: Optional[str] = None, output_dir: Optional[str] = None) -> None:
        self.tools_dir = Path(tools_dir) if tools_dir else self.get_tools_dir()
        self.output_dir = Path(output_dir) if output_dir else self.get_output_dir()

        # The engine. Previously this stayed None and every method below blew up.
        self.core = GameTranslator(output_dir=str(self.output_dir))

        self.current_step = 0
        self.apk_path: Optional[str] = None
        self.output_path: Optional[str] = None
        self.thread: Optional[threading.Thread] = None

        self.log_lines: List[str] = []
        self.ui_state: Dict = {
            "status": "idle",
            "progress": 0,
            "message": "Ready to translate",
            "stats": {},
        }

    # -- layout ------------------------------------------------------------
    def setup_ui(self) -> None:
        """Describe the 5 step layout.

        1. Select APK
        2. Download tools (if needed)
        3. Decompile & extract
        4. Translate (queue display)
        5. Patch, rebuild & sign

        Dark theme: background #121212, card #1E1E1E, primary #BB86FC,
        secondary #03DAC6, error #CF6679, success #4CAF50, text #E1E1E1,
        muted #888888.
        """

    # -- paths -------------------------------------------------------------
    @staticmethod
    def get_tools_dir() -> Path:
        """Where downloaded tools live."""
        base = Path(os.environ.get("EXTERNAL_STORAGE", "/sdcard"))
        return base / "KuroPatch" / "tools"

    @staticmethod
    def get_output_dir() -> Path:
        """Where patched APKs are written."""
        base = Path(os.environ.get("EXTERNAL_STORAGE", "/sdcard"))
        return base / "KuroPatch" / "output"

    # -- steps -------------------------------------------------------------
    def check_tools(self) -> bool:
        """Check if required tools are present, download if needed."""
        self.ui_state["status"] = "downloading"
        self.tools_dir.mkdir(parents=True, exist_ok=True)

        if self.core.setup_tools(str(self.tools_dir)):
            self.ui_state["status"] = "idle"
            return True

        results = self.core.download_tools(str(self.tools_dir), callback=self.on_download_progress)
        ok = all(results.values())
        self.ui_state["status"] = "idle" if ok else "error"
        return ok

    def select_apk(self, apk_path: str) -> bool:
        """Validate a chosen APK and start the workflow."""
        self.ui_state["status"] = "selecting"
        if not apk_path or not os.path.isfile(apk_path):
            self.ui_state["status"] = "error"
            self.ui_state["message"] = f"No such file: {apk_path}"
            return False

        self.apk_path = apk_path
        self.ui_state["status"] = "idle"
        self.ui_state["message"] = os.path.basename(apk_path)
        return True

    def run_pipeline(self, apk_path: str) -> threading.Thread:
        """Run the full pipeline on a background thread."""
        if not self.select_apk(apk_path):
            raise ValueError(self.ui_state["message"])

        self.core.set_callbacks(
            status=self.on_status_change,
            progress=self.on_progress_update,
            log=self.on_log_message,
        )

        self.thread = threading.Thread(
            target=self._run_pipeline_thread, args=(apk_path,), daemon=True
        )
        self.thread.start()
        return self.thread

    def _run_pipeline_thread(self, apk_path: str) -> None:
        """Thread-safe pipeline execution."""
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            result = self.core.run_full_pipeline(apk_path, str(self.output_dir))
            if result["success"]:
                self.output_path = result["output_apk"]
                self.ui_state["output_apk"] = self.output_path
                self.ui_state["status"] = "done"
                self.ui_state["message"] = f"Patched: {self.output_path}"
            else:
                self.ui_state["status"] = "error"
                self.ui_state["errors"] = result["errors"]
                self.ui_state["message"] = "; ".join(result["errors"]) or "Failed"
        except Exception as exc:  # noqa: BLE001 - UI boundary
            self.ui_state["status"] = "error"
            self.ui_state["errors"] = [str(exc)]
            self.ui_state["message"] = str(exc)
        finally:
            self.on_pipeline_complete()

    # -- callbacks ---------------------------------------------------------
    def on_download_progress(self, message: str, percent: int) -> None:
        self.ui_state["message"] = message
        self.ui_state["progress"] = percent
        self.update_ui()

    def on_status_change(self, status: str, details: str = "") -> None:
        self.ui_state["status"] = status
        if details:
            self.ui_state["message"] = details
        self.update_ui()

    def on_progress_update(self, completed: int, total: int) -> None:
        self.ui_state["progress"] = int((completed / total) * 100) if total > 0 else 0
        self.ui_state["stats"] = {
            key: value for key, value in self.core.get_queue_stats().items() if key != "items"
        }
        self.update_ui()

    def on_log_message(self, message: str) -> None:
        """Handle log message"""
        self.log_lines.append(message)
        if len(self.log_lines) > 500:
            del self.log_lines[:-500]

    def on_pipeline_complete(self) -> None:
        """Handle pipeline completion"""
        self.update_ui()
        if self.ui_state["status"] == "done":
            self.on_apk_ready(self.ui_state.get("output_apk"))

    def on_apk_ready(self, apk_path: Optional[str]) -> None:
        """Handle ready APK — open/share via Intent.ACTION_VIEW or ACTION_SEND."""
        self.ui_state["share_target"] = apk_path

    def update_ui(self) -> None:
        """Push ui_state to the widgets (no-op headless)."""

    # -- test/demo entry point --------------------------------------------
    @classmethod
    def describe(cls) -> Dict[str, object]:
        """A plain description of the layout, used by the demo and the tests."""
        return {
            "steps": [
                "Select APK",
                "Download tools (if needed)",
                "Decompile & extract",
                "Translate",
                "Patch, rebuild & sign",
            ],
            "colors": {
                "background": "#121212", "card": "#1E1E1E", "primary": "#BB86FC",
                "secondary": "#03DAC6", "error": "#CF6679", "success": "#4CAF50",
                "text": "#E1E1E1", "muted": "#888888",
            },
            "states": list(STATES),
        }


if __name__ == "__main__":
    print("KuroPatch - UI wrapper")
    print("=" * 40)
    for key, value in TranslatorActivity.describe().items():
        print(f"\n{key}:")
        if isinstance(value, dict):
            for name, color in value.items():
                print(f"  {name}: {color}")
        else:
            for item in value:
                print(f"  - {item}")

    activity = TranslatorActivity(tools_dir="/tmp/kuropatch-tools", output_dir="/tmp/kuropatch-out")
    print(f"\nengine wired: {activity.core is not None}")
    print(f"initial status: {activity.ui_state['status']}")
    activity.core.clean_workspace()
