#!/usr/bin/env python3
"""KuroPatch APK front end — patch an Android game's string resources.

    python apk.py game.apk -t id -o out/

This is the entry point for `.apk` files. (`.jar` files go through main.py:
a plain unzip cannot turn an APK's classes.dex into smali, so main.py would
find nothing.)

Pipeline: preflight -> apktool decompile -> extract -> translate -> patch ->
rebuild -> sign (v1+v2+v3) -> verify.

Exit codes: 0 success, 1 error, 2 nothing translatable, 3 output failed
verification.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from typing import List, Optional, Sequence

from core import GameTranslator
from translator import DEFAULT_SOURCE, DEFAULT_TARGET

try:  # progress bars are nice, never required
    from tqdm import tqdm  # type: ignore

    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    _HAS_TQDM = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kuropatch-apk",
        description="Translate an Android APK's string resources.",
    )
    parser.add_argument("apk", help="path to the game .apk")
    parser.add_argument("-o", "--output", default=None,
                        help="output directory (default: <apk-dir>/kuropatch_out)")
    parser.add_argument("-s", "--source", default=DEFAULT_SOURCE,
                        help=f"source language (default: {DEFAULT_SOURCE})")
    parser.add_argument("-t", "--target", default=DEFAULT_TARGET,
                        help=f"target language (default: {DEFAULT_TARGET})")
    parser.add_argument("--provider", default=None,
                        choices=["google-web", "google-v2", "libretranslate", "none"],
                        help="translation backend (default: google-v2 with --api-key, else google-web)")
    parser.add_argument("--api-key", default=None,
                        help="API key for google-v2 / libretranslate (or TRANSLATE_API_KEY)")
    parser.add_argument("--tools", default="./tools",
                        help="directory for apktool/uber-apk-signer (default: ./tools)")
    parser.add_argument("--no-download", action="store_true",
                        help="do not download missing tools; fail instead")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.35,
                        help="minimum seconds between provider calls")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--keystore", default=None,
                        help="sign with this keystore instead of the debug key")
    parser.add_argument("--ks-alias", default=None)
    parser.add_argument("--ks-pass", default=None)
    parser.add_argument("--ks-key-pass", default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="decompile and list strings, translate nothing, build nothing")
    parser.add_argument("--translate-all", action="store_true",
                        help="translate in dry-run too (still builds nothing)")
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument("--report-dir", default=None,
                        help="where found_strings.txt goes (default: the output dir)")
    return parser


def ensure_tools(engine: GameTranslator, tools_dir: str, allow_download: bool) -> bool:
    """Make sure apktool exists; fetch tools when allowed."""
    os.makedirs(tools_dir, exist_ok=True)
    if engine.setup_tools(tools_dir) and engine.tools.get("apktool"):
        return True
    if not allow_download:
        print("[!] apktool not found and --no-download was given.", file=sys.stderr)
        return False
    print("[*] Fetching missing tools...")
    results = engine.download_tools(tools_dir)
    if not results.get("apktool"):
        print("[!] Could not obtain apktool.", file=sys.stderr)
        return False
    if not results.get("uber_apk_signer") and not results.get("apksigner"):
        print("[!] No v2-capable signer available; output may not install.", file=sys.stderr)
    return True


def run_dry_run(engine: GameTranslator, apk_path: str, output_dir: str,
                report_dir: Optional[str], translate_all: bool) -> int:
    print("[*] Dry run — decompiling and collecting strings only.")
    if not engine.decompile_apk(apk_path):
        print("[!] Decompile failed.", file=sys.stderr)
        return 1
    strings = engine.extract_strings()
    print(f"[*] {len(strings)} resource strings, "
          f"{sum(1 for s in engine.sources if s.kind == 'smali')} smali literals")
    where = report_dir or output_dir
    report = engine.write_report(os.path.join(where, "found_strings.txt"))
    print(f"[*] Report: {report}")
    if not strings:
        return 2
    if translate_all:
        print("[!] --translate-all with --dry-run only lists; nothing is translated.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.isfile(args.apk):
        print(f"[!] No such file: {args.apk}", file=sys.stderr)
        return 1
    if not zipfile.is_zipfile(args.apk):
        print(f"[!] Not a zip archive: {args.apk}", file=sys.stderr)
        return 1

    provider = args.provider or ("google-v2" if args.api_key else "google-web")
    print(f"[*] Input    : {args.apk}")
    print(f"[*] Languages: {args.source} -> {args.target} via {provider}")

    engine = GameTranslator(keep_workspace=args.keep_workspace)
    engine.source_lang = args.source
    engine.target_lang = args.target
    engine.workers = max(1, args.workers)
    engine.max_retries = max(1, args.retries)
    if args.api_key:
        engine.api_key = args.api_key
    if args.keystore:
        engine.keystore_path = args.keystore
        if args.ks_alias:
            engine.alias = args.ks_alias
        if args.ks_pass:
            engine.keystore_pass = args.ks_pass
        if args.ks_key_pass:
            engine.key_pass = args.ks_key_pass
    # TranslatorEngine reads this attribute for pacing.
    engine.request_delay = args.delay

    engine.set_callbacks(
        status=lambda status, details: print(f"[{status}] {details}"),
        progress=lambda done, total: None,
        log=print,
    )

    try:
        if not ensure_tools(engine, args.tools, not args.no_download):
            return 1

        problems = engine.preflight(args.apk, args.tools)
        if problems:
            print("[!] Preflight failed:", file=sys.stderr)
            for problem in problems:
                print(f"    - {problem}", file=sys.stderr)
            return 1

        output_dir = args.output or engine.resolve_output_dir(args.apk)

        if args.dry_run:
            return run_dry_run(engine, args.apk, output_dir, args.report_dir, args.translate_all)

        result = engine.run_full_pipeline(args.apk, output_dir)
        if args.report_dir and result.get("report"):
            import shutil

            named = os.path.join(args.report_dir, "found_strings.txt")
            os.makedirs(args.report_dir, exist_ok=True)
            shutil.copy2(result["report"], named)
            print(f"[*] Report copied to {named}")

        if not result["success"]:
            print("[!] Pipeline failed:", file=sys.stderr)
            for error in result["errors"]:
                print(f"    - {error}", file=sys.stderr)
            return 1

        verdict = result.get("verification", {})
        print(f"[✓] Patched APK: {result['output_apk']}")
        print(f"[*] Verified: {verdict.get('size', 0):,} bytes, "
              f"v1={verdict.get('v1')} v2={verdict.get('v2')}")
        if not result.get("stats", {}).get("done"):
            print("[!] Nothing was translated; output carries the original strings.",
                  file=sys.stderr)
            return 3
        return 0

    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"[!] Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine.engine is not None:
            engine.engine.close()
        if not args.keep_workspace:
            engine.clean_workspace()


if __name__ == "__main__":
    sys.exit(main())
