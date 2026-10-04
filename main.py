#!/usr/bin/env python3
"""KuroPatch — translate a Java game's localisation resources.

    python main.py game.jar -o game_ID.jar --target id

What this actually does, step by step:

1. unpack ``game.jar`` into a workspace (safely),
2. collect every ``.properties`` value *and* every smali string literal,
3. translate each distinct string exactly once (the cache makes reruns free),
4. write the translations back, preserving comments, escapes and layout,
5. repack the archive with its original entry order and compression,
6. write ``found_strings.txt`` / ``translated_strings.txt`` reports.

Exit codes: ``0`` success, ``1`` error, ``2`` nothing to translate,
``3`` nothing could be translated.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

from patcher import (
    JarPatcher,
    PropertiesFile,
    iter_class_strings,
    patch_class_strings,
    patch_smali_literals,
)
from stringpacks import (
    StringPack,
    build_resource,
    is_string_pack_entry,
    parse_resource,
)
from translator import (
    DEFAULT_SOURCE,
    DEFAULT_TARGET,
    TranslationResult,
    TranslatorEngine,
    should_translate,
)

try:  # progress bars are nice, never required
    from tqdm import tqdm  # type: ignore

    _HAS_TQDM = True
except ImportError:  # pragma: no cover
    _HAS_TQDM = False


def _progress(iterable, desc: str, total: int):
    if _HAS_TQDM:
        return tqdm(iterable, desc=desc, total=total, unit="str")
    return iterable


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class Collected:
    """Where each translatable string came from."""

    def __init__(self) -> None:
        # decoded value -> list of "path::key" origins
        self.origins: Dict[str, List[str]] = {}
        self.total_seen = 0

    def add(self, value: str, origin: str, minimum: int, translate_all: bool = False) -> bool:
        value = value.strip()
        if not value:
            return False
        self.total_seen += 1
        if len(value) < minimum:
            return False
        ok, _reason = should_translate(value, translate_all=translate_all)
        if not ok:
            return False
        bucket = self.origins.setdefault(value, [])
        if origin not in bucket:
            bucket.append(origin)
        return True


def collect_from_properties(patcher: JarPatcher, minimum: int, translate_all: bool = False) -> Tuple[List[Tuple[str, PropertiesFile]], Collected]:
    """Return ``[(path, parsed)]`` for every properties resource, plus the values."""
    files: List[Tuple[str, PropertiesFile]] = []
    collected = Collected()

    for path in patcher.get_properties_files():
        props = patcher.load_properties(path)
        if not len(props):
            continue
        files.append((path, props))
        rel = os.path.relpath(path, patcher.workspace)
        for entry in props.entries:
            collected.add(entry.value, f"{rel}::{entry.key}", minimum, translate_all)

    return files, collected


def collect_from_smali(patcher: JarPatcher, minimum: int, translate_all: bool = False) -> Tuple[List[Tuple[str, str]], Collected]:
    """Return ``[(path, content)]`` for every smali file, plus its literals."""
    files: List[Tuple[str, str]] = []
    collected = Collected()

    from patcher import iter_smali_literals

    for path in patcher.get_smali_files():
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as handle:
            content = handle.read()
        files.append((path, content))
        rel = os.path.relpath(path, patcher.workspace)
        for _start, _end, decoded in iter_smali_literals(content):
            collected.add(decoded, f"{rel}::const-string", minimum, translate_all)

    return files, collected


def collect_from_classes(patcher: JarPatcher, minimum: int, translate_all: bool = False) -> Tuple[List[Tuple[str, bytes]], Collected]:
    """Return ``[(path, data)]`` for every .class file, plus its string constants.

    This is where J2ME games keep their UI text: the constant pool's
    ``CONSTANT_String`` entries. Only pool entries used *purely* as string
    constants are collected — class/method/field names are never touched.
    """
    files: List[Tuple[str, bytes]] = []
    collected = Collected()

    for path in patcher.get_class_files():
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        files.append((path, data))
        rel = os.path.relpath(path, patcher.workspace)
        for index, decoded in iter_class_strings(data):
            collected.add(decoded, f"{rel}::ldc#{index}", minimum, translate_all)

    return files, collected


def collect_from_packs(patcher: JarPatcher, minimum: int) -> Tuple[List[Tuple[str, str, List["StringPack"]]], Collected]:
    """Return ``[(workspace_path, archive_name, packs)]`` for Gameloft string packs.

    ``RES_STRINGS*`` resources hold LZMA-compressed string tables — this is
    where Gameloft J2ME games keep dialogue and UI text. Every entry in a
    pack is display text by construction, so only the safety filters apply
    (empty / too short / technical token), never the content heuristics.
    """
    pack_files: List[Tuple[str, str, List["StringPack"]]] = []
    collected = Collected()

    for root, _dirs, files in os.walk(patcher.workspace):
        for filename in files:
            ws_path = os.path.join(root, filename)
            rel = os.path.relpath(ws_path, patcher.workspace).replace(os.sep, "/")
            if not is_string_pack_entry(rel):
                continue
            try:
                with open(ws_path, "rb") as handle:
                    packs = parse_resource(handle.read())
            except (OSError, ValueError):
                continue
            pack_files.append((ws_path, rel, packs))
            for pack_index, pack in enumerate(packs):
                for str_index, value in enumerate(pack.strings):
                    collected.add(value, f"{rel}::pack{pack_index}#{str_index}", minimum, True)

    return pack_files, collected


def patch_pack_files(pack_files: List[Tuple[str, str, List["StringPack"]]],
                     translations: Dict[str, str]) -> int:
    """Apply ``translations`` to pack strings and rewrite the resource files.

    Returns the number of strings replaced. Files that fail to rebuild are
    left untouched.
    """
    replaced = 0
    for ws_path, _rel, packs in pack_files:
        dirty = False
        for pack in packs:
            for index, value in enumerate(pack.strings):
                new_value = translations.get(value)
                if new_value and new_value != value:
                    pack.strings[index] = new_value
                    replaced += 1
                    dirty = True
        if dirty:
            try:
                rebuilt = build_resource(packs)
            except ValueError:
                continue
            try:
                with open(ws_path, "wb") as handle:
                    handle.write(rebuilt)
            except OSError:
                continue
    return replaced


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def write_reports(
    report_dir: str,
    collected: Collected,
    results: Dict[str, TranslationResult],
) -> Tuple[str, str]:
    os.makedirs(report_dir, exist_ok=True)
    found_path = os.path.join(report_dir, "found_strings.txt")
    done_path = os.path.join(report_dir, "translated_strings.txt")

    with open(found_path, "w", encoding="utf-8") as found, open(done_path, "w", encoding="utf-8") as done:
        found.write(f"# KuroPatch: {len(collected.origins)} translatable strings "
                    f"(from {collected.total_seen} scanned values)\n")
        done.write("# original => translation [status]\n")
        for value in sorted(collected.origins):
            origins = ", ".join(collected.origins[value])
            found.write(f"{value}\n    <- {origins}\n")

            result = results.get(value)
            if result is None:
                done.write(f"{value} => {value} [skipped]\n")
            elif result.status == "done":
                done.write(f"{value} => {result.translated}\n")
            else:
                done.write(f"{value} => {value} [{result.status}]\n")

    return found_path, done_path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kuropatch",
        description="Translate a Java game's .properties / smali resources.",
    )
    parser.add_argument("jar", help="path to the game .jar (or .apk)")
    parser.add_argument("-o", "--output", help="output archive (default: <input>_ID.jar)")
    parser.add_argument("-s", "--source", default=DEFAULT_SOURCE, help=f"source language (default: {DEFAULT_SOURCE})")
    parser.add_argument("-t", "--target", default=DEFAULT_TARGET, help=f"target language (default: {DEFAULT_TARGET})")
    parser.add_argument("--provider", default="google-web",
                        choices=["google-web", "google-v2", "libretranslate", "none"],
                        help="translation backend (default: google-web)")
    parser.add_argument("--api-key", help="API key for google-v2 / libretranslate")
    parser.add_argument("--endpoint", help="custom endpoint for libretranslate")
    parser.add_argument("--cache", default="translation_cache.json", help="translation cache file")
    parser.add_argument("--no-cache", action="store_true", help="do not read or write the cache")
    parser.add_argument("--delay", type=float, default=0.35, help="minimum seconds between provider calls")
    parser.add_argument("--workers", type=int, default=1, help="parallel lookups (provider calls stay serialised)")
    parser.add_argument("--retries", type=int, default=3, help="attempts per string")
    parser.add_argument("--min-length", type=int, default=3, help="skip values shorter than this")
    parser.add_argument("--translate-all", action="store_true",
                        help="disable the skip heuristics (translates keys, paths and tokens too)")
    parser.add_argument("--no-smali", action="store_true", help="ignore smali string literals")
    parser.add_argument("--dry-run", action="store_true", help="translate and report, but do not write the archive")
    parser.add_argument("--workspace", default="jar_workspace", help="scratch directory")
    parser.add_argument("--report-dir", default=".", help="where the .txt reports go")
    parser.add_argument("--keep-workspace", action="store_true", help="do not delete the scratch directory")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not os.path.isfile(args.jar):
        print(f"[!] No such file: {args.jar}", file=sys.stderr)
        return 1
    if not zipfile.is_zipfile(args.jar):
        print(f"[!] Not a zip archive: {args.jar}", file=sys.stderr)
        return 1

    target = args.target
    output = args.output or args.jar.replace(".jar", f"_{target.upper()}.jar")
    if output == args.jar:
        print("[!] Refusing to overwrite the input archive", file=sys.stderr)
        return 1

    print(f"[*] Input    : {args.jar}")
    print(f"[*] Output   : {output}")
    print(f"[*] Languages: {args.source} -> {target} via {args.provider}")

    patcher = JarPatcher(args.jar, workspace=args.workspace)
    engine: Optional[TranslatorEngine] = None

    try:
        patcher.extract()
        print(f"[*] Unpacked to {patcher.workspace}")

        prop_files, prop_strings = collect_from_properties(patcher, args.min_length, args.translate_all)
        print(f"[*] {len(prop_files)} properties files, "
              f"{len(prop_strings.origins)} translatable values")

        smali_files: List[Tuple[str, str]] = []
        smali_strings = Collected()
        if not args.no_smali:
            smali_files, smali_strings = collect_from_smali(patcher, args.min_length, args.translate_all)
            print(f"[*] {len(smali_files)} smali files, "
                  f"{len(smali_strings.origins)} translatable literals")

        class_files, class_strings = collect_from_classes(patcher, args.min_length, args.translate_all)
        print(f"[*] {len(class_files)} class files, "
              f"{len(class_strings.origins)} translatable string constants")

        pack_files, pack_strings = collect_from_packs(patcher, args.min_length)
        if pack_files:
            print(f"[*] {len(pack_files)} Gameloft string-pack resources, "
                  f"{len(pack_strings.origins)} translatable pack strings")

        # One translation per distinct string, even when it appears 200 times.
        pending = sorted(set(prop_strings.origins) | set(smali_strings.origins) | set(class_strings.origins) | set(pack_strings.origins))

        # Merge provenance: the same string may live in several places,
        # and the report should say where.
        merged = Collected()
        merged.total_seen = prop_strings.total_seen + smali_strings.total_seen + class_strings.total_seen + pack_strings.total_seen
        for value in pending:
            merged.origins[value] = (
                list(prop_strings.origins.get(value, []))
                + list(smali_strings.origins.get(value, []))
                + list(class_strings.origins.get(value, []))
                + list(pack_strings.origins.get(value, []))
            )

        if not pending:
            print("[!] Nothing translatable found.", file=sys.stderr)
            return 2

        cache_file = "" if args.no_cache else args.cache
        engine = TranslatorEngine(
            cache_file=cache_file,
            source=args.source,
            target=target,
            provider_name=args.provider,
            delay=args.delay,
            max_retries=args.retries,
            api_key=args.api_key,
            endpoint=args.endpoint,
            translate_all=args.translate_all,
        )

        print(f"[*] Translating {len(pending)} strings...")
        started = time.time()
        results: Dict[str, TranslationResult] = {}
        done = 0

        def on_result(result: TranslationResult) -> None:
            nonlocal done
            done += 1
            results[result.original] = result

        engine.translate_many(pending, on_result=on_result, workers=args.workers)
        elapsed = time.time() - started

        stats = engine.stats
        # A fully cached run translated nothing *this time* but still produced
        # every translation, so it counts as a success.
        produced = stats["done"] + stats["cached"]
        print(f"[*] Translated {stats['done']}, cached {stats['cached']}, "
              f"failed {stats['error']} in {elapsed:.1f}s")
        if engine.last_error:
            print(f"[!] Last error: {engine.last_error}")

        # -- write translations back -------------------------------------
        changed_files = 0
        translations: Dict[str, str] = {}
        for value, result in results.items():
            # "cached" is a produced translation, not a miss.
            if result.status in ("done", "cached") and result.translated != value:
                translations[value] = result.translated

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
        if smali_files:
            for path, content in smali_files:
                new_content, replaced, _unmatched = patch_smali_literals(content, translations)
                if replaced and new_content != content:
                    patcher.write_bytes(path, new_content.encode("utf-8", errors="surrogateescape"))
                    smali_changed += replaced

        class_changed = 0
        if class_files:
            for path, data in class_files:
                new_data, replaced = patch_class_strings(data, translations)
                if replaced and new_data != data:
                    patcher.write_bytes(path, new_data)
                    class_changed += replaced

        pack_changed = patch_pack_files(pack_files, translations) if pack_files else 0

        print(f"[*] Rewrote {changed_files} properties files, {smali_changed} smali literals, "
              f"{class_changed} class string constants, {pack_changed} string-pack strings")

        found_path, done_path = write_reports(args.report_dir, merged, results)
        print(f"[*] Reports: {found_path}, {done_path}")

        if args.dry_run:
            print("[*] Dry run — archive not written.")
            return 0 if produced else 3

        patcher.rebuild(output)
        print(f"[✓] Patched archive: {output}")

        if produced == 0:
            print("[!] No translation was produced; the archive is unchanged.", file=sys.stderr)
            return 3
        return 0

    except KeyboardInterrupt:
        print("\n[!] Interrupted.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"[!] Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.close()
        if not args.keep_workspace:
            patcher.close()


if __name__ == "__main__":
    sys.exit(main())
