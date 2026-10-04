#!/usr/bin/env python3
"""Android entry point for KuroPatch (runs under Chaquopy on-device).

All pipeline logic stays in :mod:`main`, :mod:`patcher` and :mod:`translator`;
this module only adapts that engine to the Android UI:

* real file paths (the app copies the picked document into private storage,
  because Chaquopy/Python cannot read ``content://`` URIs),
* a Java listener object for status / progress / log / completion callbacks,
  instead of ``print``,
* no ``sys.exit`` — results come back through ``on_done``.

The module deliberately imports nothing Android-specific, so the exact same
code path is exercised by ``tests/test_bridge.py`` on a desktop JVM-less
Python with a fake listener.

Listener protocol (duck-typed; every method is optional):

* ``on_status(status, message)`` — phase changes, e.g. ``"translating"``.
* ``on_progress(done, total)`` — translation progress counts.
* ``on_log(line)`` — one human-readable log line.
* ``on_done(success, output_path, message)`` — terminal call. ``output_path``
  is ``""`` when nothing was written (dry run or failure).
"""

from __future__ import annotations

import os
import time
import traceback
from typing import Any, Dict, List, Optional

from main import Collected, collect_from_properties, collect_from_smali, write_reports
from patcher import JarPatcher, patch_smali_literals
from translator import TranslationResult, TranslatorEngine


def _emit(listener: Any, method: str, *args: Any) -> None:
    """Call a listener method without ever breaking the pipeline."""
    if listener is None:
        return
    func = getattr(listener, method, None)
    if func is None:
        return
    try:
        func(*args)
    except Exception:
        # A broken UI callback must not kill a half-hour translation run.
        pass


def run_patch(
    input_path: str,
    output_path: str,
    report_dir: str,
    source: str = "en",
    target: str = "id",
    provider: str = "google-web",
    api_key: Optional[str] = None,
    translate_all: bool = False,
    dry_run: bool = False,
    workspace_dir: Optional[str] = None,
    listener: Any = None,
) -> Dict[str, Any]:
    """Run the JAR localisation pipeline. Returns a result dict.

    Mirrors ``main.main()`` step for step (extract → collect → translate →
    patch → report → rebuild) but reports through ``listener`` and never
    raises: failures are delivered via ``on_done(False, "", message)`` and the
    returned dict.
    """
    started = time.time()
    result: Dict[str, Any] = {
        "success": False,
        "output_path": "",
        "translated": 0,
        "total": 0,
        "message": "",
    }

    def fail(message: str) -> Dict[str, Any]:
        result["message"] = message
        _emit(listener, "on_log", f"[!] {message}")
        _emit(listener, "on_status", "error", message)
        _emit(listener, "on_done", False, "", message)
        return result

    patcher: Optional[JarPatcher] = None
    engine: Optional[TranslatorEngine] = None
    try:
        if not input_path or not os.path.isfile(input_path):
            return fail(f"No such file: {input_path}")
        os.makedirs(report_dir, exist_ok=True)
        if workspace_dir:
            os.makedirs(workspace_dir, exist_ok=True)

        # -- extract ------------------------------------------------------
        _emit(listener, "on_status", "extracting", "Unpacking archive...")
        patcher = JarPatcher(input_path, workspace=workspace_dir or "jar_workspace")
        patcher.extract()
        _emit(listener, "on_log", f"[*] Unpacked to {patcher.workspace}")

        # -- collect ------------------------------------------------------
        _emit(listener, "on_status", "extracting", "Collecting strings...")
        prop_files, prop_strings = collect_from_properties(patcher, 3, translate_all)
        smali_files, smali_strings = collect_from_smali(patcher, 3, translate_all)
        pending = sorted(set(prop_strings.origins) | set(smali_strings.origins))
        total = len(pending)
        result["total"] = total
        _emit(
            listener, "on_log",
            f"[*] {len(prop_files)} properties files, {len(smali_files)} smali files, "
            f"{total} translatable strings",
        )
        if not pending:
            return fail("Nothing translatable found in this archive.")

        # -- translate ----------------------------------------------------
        _emit(listener, "on_status", "translating", f"Translating {total} strings...")
        engine = TranslatorEngine(
            cache_file=os.path.join(report_dir, "translation_cache.json"),
            source=source,
            target=target,
            provider_name=provider,
            delay=0.35,
            max_retries=3,
            api_key=api_key or None,
            translate_all=translate_all,
        )
        results: Dict[str, TranslationResult] = {}
        done = 0

        def on_result(res: TranslationResult) -> None:
            nonlocal done
            results[res.original] = res
            done += 1
            _emit(listener, "on_progress", done, total)

        # workers=1: the rate limiter serialises provider calls anyway, and a
        # phone has no business spawning extra threads for this.
        engine.translate_many(pending, on_result=on_result, workers=1)
        stats = engine.stats
        produced = stats["done"] + stats["cached"]
        result["translated"] = produced
        _emit(
            listener, "on_log",
            f"[*] Translated {stats['done']}, cached {stats['cached']}, "
            f"failed {stats['error']} in {time.time() - started:.1f}s",
        )
        if engine.last_error:
            _emit(listener, "on_log", f"[!] Last provider error: {engine.last_error}")

        # -- patch --------------------------------------------------------
        _emit(listener, "on_status", "patching", "Writing translations back...")
        translations = {
            value: res.translated
            for value, res in results.items()
            if res.status == "done" and res.translated and res.translated != value
        }

        changed_files = 0
        for path, props in prop_files:
            touched = False
            for entry in props.entries:
                new_value = translations.get(entry.value)
                if new_value and new_value != entry.value:
                    entry.value = new_value
                    touched = True
            if touched and patcher.save_properties(props, path):
                changed_files += 1

        smali_changed = 0
        for path, content in smali_files:
            new_content, replaced, _unmatched = patch_smali_literals(content, translations)
            if replaced and new_content != content:
                patcher.write_bytes(path, new_content.encode("utf-8", errors="surrogateescape"))
                smali_changed += replaced
        _emit(
            listener, "on_log",
            f"[*] Rewrote {changed_files} properties files, {smali_changed} smali literals",
        )

        # -- reports ------------------------------------------------------
        merged_origins: Dict[str, List[str]] = {}
        for value in pending:
            merged_origins[value] = (
                list(prop_strings.origins.get(value, []))
                + list(smali_strings.origins.get(value, []))
            )
        merged = Collected()
        merged.total_seen = prop_strings.total_seen + smali_strings.total_seen
        merged.origins = merged_origins
        found_path, done_path = write_reports(report_dir, merged, results)
        _emit(listener, "on_log", f"[*] Reports: {found_path}, {done_path}")

        if dry_run:
            message = (
                f"Dry run complete — {produced}/{total} strings translated, "
                f"archive not written. Reports in {report_dir}"
            )
            result["success"] = True
            result["message"] = message
            _emit(listener, "on_status", "done", message)
            _emit(listener, "on_done", True, "", message)
            return result

        # -- rebuild ------------------------------------------------------
        _emit(listener, "on_status", "rebuilding", "Repacking archive...")
        patcher.rebuild(output_path)
        if not os.path.isfile(output_path):
            return fail("Rebuild reported success but produced no file.")

        elapsed = time.time() - started
        size_kb = os.path.getsize(output_path) // 1024
        message = f"Patched archive: {output_path} ({size_kb} KB, {elapsed:.0f}s)"
        if produced == 0:
            message += " — note: no translation was produced, archive is unchanged."
        result["success"] = True
        result["output_path"] = output_path
        result["message"] = message
        _emit(listener, "on_log", f"[✓] {message}")
        _emit(listener, "on_status", "done", message)
        _emit(listener, "on_done", True, output_path, message)
        return result

    except Exception as exc:  # noqa: BLE001 - UI boundary, never raise to Java
        detail = f"{type(exc).__name__}: {exc}"
        _emit(listener, "on_log", f"[!] {detail}")
        _emit(listener, "on_log", traceback.format_exc(limit=5))
        return fail(detail)
    finally:
        if engine is not None:
            try:
                engine.close()
            except Exception:
                pass
        if patcher is not None and not os.environ.get("KUROPATCH_KEEP_WORKSPACE"):
            try:
                patcher.close()
            except Exception:
                pass
