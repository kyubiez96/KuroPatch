"""kpatch -- KuroPatch v2 command line interface (.jar only).

Commands:
  collect    List translatable strings found in a .jar
             (.properties + smali const-string + .class constants + packs).
  translate  Apply a translations JSON ({source: translation}) to a .jar,
             or auto-translate it with the translate engine
             (--auto, --provider passthrough|agnes; the agnes provider reads
             AGNES_API_KEY from the environment -- keys are never arguments).
  patch      Full pipeline: collect -> auto-translate -> patch .jar,
             output ``<name>_<target>.jar`` by default.

Full APK patching needs apktool (real JVM) -- use the PC workflow for APKs;
the on-device app and this CLI target .jar game resources only.

Run ``kpatch <command> --help`` for per-command options.
"""

import argparse
import json
import os
import sys

# Allow running as ``python -m cli.main`` from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import jar as _jar


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


def _default_out(jar_path, target):
    base, _ext = os.path.splitext(jar_path)
    return f"{base}_{target}.jar"


def _auto_translate(strings, args):
    """Translate via the translate engine; returns {source: translation}."""
    from translate import (AgnesProvider, PassthroughProvider,
                           results_to_dict, translate_strings)
    if args.provider == "agnes":
        key = os.environ.get("AGNES_API_KEY", "")
        if not key:
            raise RuntimeError(
                "AGNES_API_KEY is not set in the environment. "
                "Export your own key first (BYOK); it is never stored.")
        # AgnesProvider takes no api_key arg: it reads AGNES_API_KEY
        # from the environment at call time.
        provider = AgnesProvider()
    else:
        provider = PassthroughProvider()
    src = {value: value for value in strings}
    results = translate_strings(src, target_lang=args.target,
                                source_lang=args.source,
                                provider=provider)
    counts = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("auto-translate: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return results_to_dict(results)


def cmd_collect(args):
    res = _jar.collect_strings(args.jar, minimum=args.minimum)
    strings = res["strings"]
    print(f"{len(strings)} translatable strings "
          f"(from {res['total_seen']} scanned values)")
    for value in sorted(strings):
        print(f"  {value!r}  <- {'; '.join(strings[value][:2])}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(strings, fh, ensure_ascii=False, indent=1)
        print(f"wrote {args.out}")


def cmd_translate(args):
    res = _jar.collect_strings(args.jar, minimum=args.minimum)
    strings = res["strings"]
    print(f"collected {len(strings)} strings from {args.jar}")
    if args.auto:
        translations = _auto_translate(strings, args)
    elif args.translations:
        translations = _load_translations_json(args.translations)
    else:
        raise ValueError(
            "nothing to apply: pass --translations FILE.json or --auto "
            "(auto-translation lives in the translate/ module).")
    out = args.out or _default_out(args.jar, args.target)
    stats = _jar.patch_jar(args.jar, translations, out)
    print(f"patched -> {out}  {stats}")


def cmd_patch(args):
    res = _jar.collect_strings(args.jar, minimum=args.minimum)
    strings = res["strings"]
    print(f"collected {len(strings)} strings from {args.jar}")
    translations = _auto_translate(strings, args)
    out = args.out or _default_out(args.jar, args.target)
    stats = _jar.patch_jar(args.jar, translations, out)
    print(f"patched -> {out}  {stats}")


def build_parser():
    p = argparse.ArgumentParser(prog="kpatch", description="KuroPatch v2 (.jar)")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn, help_text in [
        ("collect", cmd_collect, "list translatable strings in a .jar"),
        ("translate", cmd_translate, "translate a .jar (JSON map or --auto)"),
        ("patch", cmd_patch, "full pipeline: collect -> auto-translate -> patch"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("jar", help=".jar file to process")
        sp.add_argument("--out", "-o", help="output file (default: <name>_<target>.jar)")
        sp.add_argument("--minimum", type=int, default=3,
                        help="minimum string length to collect (default 3)")
        if name in ("translate", "patch"):
            sp.add_argument("--source", default="en", help="source language (default en)")
            sp.add_argument("--target", default="id", help="target language (default id)")
            sp.add_argument("--provider", default="passthrough",
                            choices=["passthrough", "agnes"],
                            help="translation provider (default passthrough)")
        if name == "translate":
            sp.add_argument("--auto", action="store_true",
                            help="auto-translate with the translate engine")
            sp.add_argument("--translations",
                            help="JSON file mapping source -> translation")
        sp.set_defaults(func=fn)
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
