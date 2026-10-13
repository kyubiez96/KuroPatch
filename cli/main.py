"""kpatch -- KuroPatch v2 command line interface.

Commands:
  extract    Unpack an APK and report manifest info + string resources.
  translate  Apply a translations JSON ({key: value}) to a strings file, or
             auto-translate it with the translate engine
             (--auto, --provider passthrough|agnes; the agnes provider reads
             AGNES_API_KEY from the environment -- keys are never arguments).
  build      Repack an extracted tree into an APK (align + optional sign).
  patch      Full pipeline: extract -> translate -> repack -> align -> sign.

Run ``kpatch <command> --help`` for per-command options.
"""

import argparse
import json
import os
import sys

# Allow running as ``python -m cli.main`` from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import apk as _apk
from core import patcher as _patcher
from core import strings as _strings
from core.apk import AxmlError


def _load_translations_json(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"translations file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {exc}")
    if not isinstance(data, dict) or not data:
        raise ValueError(f"{path} must contain a non-empty JSON object")
    return {str(k): str(v) for k, v in data.items()}


def _default_translate_out(path):
    base, ext = os.path.splitext(path)
    return f"{base}.translated{ext or '.out'}"


def cmd_extract(args):
    out = _apk.extract_apk(args.apk, args.out or args.apk + ".extracted",
                           use_apktool=not args.no_apktool)
    print(f"extracted -> {out}")
    try:
        info = _apk.read_manifest(out)
        print(f"package:      {info.package}")
        print(f"version:      {info.version_name} ({info.version_code})")
        print(f"sdk:          min={info.min_sdk} target={info.target_sdk}")
        if info.permissions:
            print(f"permissions:  {', '.join(info.permissions[:8])}"
                  + (" ..." if len(info.permissions) > 8 else ""))
    except (AxmlError, FileNotFoundError) as exc:
        print(f"manifest: {exc}")
    files = _apk.list_string_resources(out)
    print(f"string resources ({len(files)}):")
    for f in files:
        print(f"  {os.path.relpath(f, out)}")


def _auto_translate(entries, args):
    """Translate via the translate engine; returns {key: value}."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from translate import (AgnesProvider, PassthroughProvider,
                           results_to_dict, translate_strings)
    if args.provider == "agnes":
        key = os.environ.get("AGNES_API_KEY", "")
        if not key:
            raise RuntimeError(
                "AGNES_API_KEY is not set in the environment. "
                "Export your own key first (BYOK); it is never stored.")
        provider = AgnesProvider(api_key=key)
    else:
        provider = PassthroughProvider()
    src = {e.key: e.value for e in entries if e.translatable}
    results = translate_strings(src, target_lang=args.lang,
                                source_lang=args.source_lang,
                                provider=provider)
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("auto-translate: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return results_to_dict(results)


def cmd_translate(args):
    entries = _strings.load_strings(args.strings)
    print(f"loaded {len(entries)} entries from {args.strings}")
    if args.auto:
        translations = _auto_translate(entries, args)
    elif args.translations:
        translations = _load_translations_json(args.translations)
    else:
        raise ValueError(
            "nothing to apply: pass --translations FILE.json or --auto "
            "(auto-translation lives in the translate/ module).")
    n = 0
    for e in entries:
        if e.key in translations and e.translatable:
            e.value = translations[e.key]
            n += 1
    out = args.out or _default_translate_out(args.strings)
    _strings.save_strings(out, entries)
    print(f"translated {n}/{len(entries)} entries -> {out}")


def cmd_build(args):
    out = _patcher.repack_apk(args.dir, args.out or args.dir.rstrip("/") + ".apk",
                              use_apktool=not args.no_apktool)
    print(f"repacked -> {out}")
    if not args.no_zipalign:
        out = _patcher.zipalign_apk(out)
        print("zipaligned")
    if args.sign:
        out = _patcher.sign_apk(out, keystore=args.keystore, alias=args.alias,
                                storepass=args.storepass, keypass=args.keypass)
        print(f"signed -> {out}")


def cmd_patch(args):
    translations = _load_translations_json(args.translations)
    result = _patcher.patch_apk(
        args.apk, translations,
        args.out or args.apk.replace(".apk", f"-{args.lang}.apk"),
        lang=args.lang, sign=not args.no_sign, keystore=args.keystore)
    stats = result["stats"]
    print(f"patched -> {result['apk']}")
    print(f"strings applied: {stats['applied']} in {stats['files']} file(s)")
    if stats["unmatched"]:
        print(f"warning: {len(stats['unmatched'])} translation keys not found in APK")


def build_parser():
    p = argparse.ArgumentParser(prog="kpatch",
                                description="KuroPatch v2 -- game localisation patcher")
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser("extract", help="unpack an APK")
    e.add_argument("apk", help="input APK file")
    e.add_argument("-o", "--out", default=None, help="output directory")
    e.add_argument("--no-apktool", action="store_true",
                   help="skip apktool even if installed")
    e.set_defaults(func=cmd_extract)

    t = sub.add_parser("translate", help="translate a strings file")
    t.add_argument("strings", help="strings file (.properties/.xml/.stringpack)")
    t.add_argument("--translations", default=None,
                   help="JSON file with {key: translated} dict")
    t.add_argument("--auto", action="store_true",
                   help="auto-translate with the translate engine")
    t.add_argument("--provider", default="passthrough",
                   choices=["passthrough", "agnes"],
                   help="translation provider for --auto")
    t.add_argument("--lang", default="id", help="target language (default: id)")
    t.add_argument("--source-lang", default="en", help="source language")
    t.add_argument("-o", "--out", default=None, help="output file")
    t.set_defaults(func=cmd_translate)

    b = sub.add_parser("build", help="repack an extracted tree into an APK")
    b.add_argument("dir", help="extracted APK directory")
    b.add_argument("-o", "--out", default=None, help="output APK path")
    b.add_argument("--no-apktool", action="store_true")
    b.add_argument("--no-zipalign", action="store_true")
    b.add_argument("--sign", action="store_true", help="sign with debug keystore")
    b.add_argument("--keystore", default=None)
    b.add_argument("--alias", default="androiddebugkey")
    b.add_argument("--storepass", default="android")
    b.add_argument("--keypass", default=None)
    b.set_defaults(func=cmd_build)

    pc = sub.add_parser("patch", help="full pipeline: extract+translate+repack")
    pc.add_argument("apk", help="input APK file")
    pc.add_argument("--lang", default="id", help="target language (default: id)")
    pc.add_argument("--translations", required=True,
                    help="JSON file with {key: translated} dict")
    pc.add_argument("-o", "--out", default=None, help="output APK path")
    pc.add_argument("--no-sign", action="store_true")
    pc.add_argument("--keystore", default=None)
    pc.set_defaults(func=cmd_patch)
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"kpatch: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
