#!/usr/bin/env python3
"""kuropatch-qa: seven QA gates for the KuroPatch localization pipeline.

Subcommands: recon, delta, glossary, typo, glyph, sample, keystore.
All gates are report-only. JSON on stdout.
Exit 0 = pass, 1 = issues found, 2 = gate could not run.
Stdlib only.
"""
import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET


def out(verdict, **kw):
    print(json.dumps({"verdict": verdict, **kw}, indent=1,
                     ensure_ascii=False))


# ---------------------------------------------------------------- recon
def cmd_recon(args):
    try:
        z = zipfile.ZipFile(args.apk)
    except Exception as e:
        out("ERROR", detail="cannot open apk: %s" % e)
        return 2
    names = z.namelist()
    libs = [n for n in names if n.startswith("lib/") and n.endswith(".so")]
    archs = sorted({n.split("/")[1] for n in libs if len(n.split("/")) > 2})

    framework = "native"
    if any("libflutter.so" in n for n in libs):
        framework = "flutter"
    elif any("libreactnativejni.so" in n for n in libs):
        framework = "react-native"
    elif any("libunity.so" in n for n in libs):
        framework = "unity"
    elif any("/xamarin." in n or "libmonodroid" in n for n in libs):
        framework = "xamarin"
    elif any(n.endswith("classes.dex") for n in names):
        framework = "native-dex"

    sv = [n for n in names if n.endswith("res/values/strings.xml")]
    n_strings = None
    if sv:
        try:
            root = ET.fromstring(z.read(sv[0]))
            n_strings = len(root.findall("string"))
        except Exception:
            pass
    dex = sum(1 for n in names if n.endswith(".dex"))
    out("PASS", framework=framework, native_archs=archs,
        strings_xml="res/values/strings.xml" if sv else None,
        string_count=n_strings, dex_files=dex,
        total_entries=len(names))
    return 0


# ---------------------------------------------------------------- delta
def load_strings(path):
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        return None, "cannot parse %s: %s" % (path, e)
    d = {}
    for s in root.findall("string"):
        name = s.get("name")
        if name:
            d[name] = "".join(s.itertext())
    return d, None


def cmd_delta(args):
    old, err = load_strings(args.old)
    if err:
        out("ERROR", detail=err)
        return 2
    new, err = load_strings(args.new)
    if err:
        out("ERROR", detail=err)
        return 2
    items = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            items.append({"name": name, "status": "NEW"})
        elif name not in new:
            items.append({"name": name, "status": "REMOVED"})
        elif old[name] != new[name]:
            items.append({"name": name, "status": "CHANGED"})
        else:
            items.append({"name": name, "status": "UNCHANGED"})
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    actionable = [i for i in items if i["status"] in ("NEW", "CHANGED")]
    out("PASS" if not actionable else "ISSUES", counts=counts,
        needs_translation=[i["name"] for i in actionable])
    return 0 if not actionable else 1


# ------------------------------------------------------------- glossary
def cmd_glossary(args):
    try:
        with open(args.glossary, encoding="utf-8") as f:
            lines = [l.rstrip("\n") for l in f if l.strip()
                     and not l.startswith("#")]
    except OSError as e:
        out("ERROR", detail="cannot read glossary: %s" % e)
        return 2
    terms = []  # (concept, approved, [variants])
    for l in lines:
        parts = l.split("\t")
        if len(parts) >= 2:
            terms.append((parts[0], parts[1],
                          [v.strip() for v in parts[2].split(",")]
                          if len(parts) > 2 else []))
    if not terms:
        out("ERROR", detail="glossary empty or bad format")
        return 2
    try:
        with open(args.strings, encoding="utf-8") as f:
            s_lines = f.read().splitlines()
    except OSError as e:
        out("ERROR", detail="cannot read strings: %s" % e)
        return 2
    violations = []
    for concept, approved, variants in terms:
        for v in variants:
            if not v:
                continue
            pat = re.compile(r"\b%s\b" % re.escape(v))
            for i, line in enumerate(s_lines, 1):
                if pat.search(line):
                    violations.append(
                        {"line": i, "concept": concept, "found": v,
                         "approved": approved,
                         "context": line.strip()[:100]})
    out("PASS" if not violations else "ISSUES",
        violations=violations, checked_terms=len(terms))
    return 0 if not violations else 1

# ----------------------------------------------------------------- typo
TYPO_RULES = [
    # (pattern, replacement_fn, message)
    (re.compile(r"\bdi ([a-z]{3,}(?:kan|i))\b"),
     lambda m: "di" + m.group(1),
     "prefix di- should be attached"),
    (re.compile(r"\bmens([aiueo][a-z]*)\b"),
     lambda m: "meny" + m.group(1),
     "me- assimilation: mens+V -> meny+V"),
    (re.compile(r"\b([a-z]{3,}) \1\b"),
     lambda m: m.group(1) + "-" + m.group(1),
     "doubled word needs hyphen"),
]
DI_OK = {"mana", "sana", "sini", "situ", "penuhi"}
FORMAL_FLAGS = re.compile(
    r"\b(gue|gw|lo|lu|nggak|gak|dongs?|kok|sih|aja|emang|udah|belom)\b")


def cmd_typo(args):
    try:
        with open(args.strings, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        out("ERROR", detail="cannot read strings: %s" % e)
        return 2
    findings = []
    for i, line in enumerate(lines, 1):
        for pat, fix, msg in TYPO_RULES:
            for m in pat.finditer(line):
                if pat is TYPO_RULES[0][0] and m.group(1) in DI_OK:
                    continue
                findings.append(
                    {"line": i, "rule": msg, "found": m.group(0),
                     "suggest": fix(m), "context": line.strip()[:100]})
        if args.formal:
            for m in FORMAL_FLAGS.finditer(line):
                findings.append(
                    {"line": i, "rule": "informal register with --formal",
                     "found": m.group(0), "suggest": None,
                     "context": line.strip()[:100]})
    out("PASS" if not findings else "ISSUES", findings=findings,
        note="heuristic regex, not a dictionary; false positives expected")
    return 0 if not findings else 1


# ---------------------------------------------------------------- glyph
def ttf_codepoints(path):
    """Return set of Unicode codepoints in a TTF/OTF cmap (formats 4, 12)."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 12 or data[:4] not in (b"\x00\x01\x00\x00", b"OTTO",
                                          b"true", b"typ1"):
        raise ValueError("not a TTF/OTF font")
    num = struct.unpack(">H", data[4:6])[0]
    cmap_off = None
    for i in range(num):
        e = data[12 + i * 16: 12 + (i + 1) * 16]
        if e[:4] == b"cmap":
            cmap_off = struct.unpack(">I", e[8:12])[0]
            break
    if cmap_off is None:
        raise ValueError("no cmap table")
    nc = struct.unpack(">H", data[cmap_off + 2:cmap_off + 4])[0]
    best = None
    for i in range(nc):
        e = data[cmap_off + 4 + i * 8: cmap_off + 12 + i * 8]
        pid, eid, off = struct.unpack(">HHI", e)
        fmt = struct.unpack(">H", data[cmap_off + off:
                                       cmap_off + off + 2])[0]
        if (pid, eid) == (3, 1) and fmt == 4:
            best = cmap_off + off
            break
        if fmt == 12 and best is None:
            best = cmap_off + off
    if best is None:
        raise ValueError("no usable cmap subtable (need format 4 or 12)")
    fmt = struct.unpack(">H", data[best:best + 2])[0]
    cps = set()
    if fmt == 4:
        seg = struct.unpack(">H", data[best + 6:best + 8])[0] // 2
        ends = struct.unpack(">%dH" % seg, data[best + 14:best + 14 + 2 * seg])
        starts = struct.unpack(">%dH" % seg,
                               data[best + 16 + 2 * seg:
                                    best + 16 + 4 * seg])
        for s, e in zip(starts, ends):
            if e == 0xFFFF:
                continue
            cps.update(range(s, e + 1))
    elif fmt == 12:
        ng = struct.unpack(">I", data[best + 12:best + 16])[0]
        for i in range(ng):
            s, e = struct.unpack(">II", data[best + 16 + i * 12:
                                             best + 24 + i * 12][:8])
            cps.update(range(s, min(e, s + 0xFFFF) + 1))
    return cps


def cmd_glyph(args):
    strings, err = load_strings(args.strings)
    if err:
        out("ERROR", detail=err)
        return 2
    try:
        covered = ttf_codepoints(args.font)
    except Exception as e:
        out("SKIP", detail="font parse: %s" % e)
        return 2
    need = set()
    for v in strings.values():
        need.update(ord(c) for c in v if not c.isspace())
    missing = sorted(c for c in need if c not in covered)
    out("PASS" if not missing else "ISSUES",
        checked_chars=len(need),
        missing=[{"char": chr(c), "codepoint": "U+%04X" % c}
                 for c in missing[:50]],
        missing_total=len(missing))
    return 0 if not missing else 1


# --------------------------------------------------------------- sample
def cmd_sample(args):
    strings, err = load_strings(args.strings)
    if err:
        out("ERROR", detail=err)
        return 2
    names = sorted(strings)
    if args.n > len(names):
        out("ERROR", detail="TOO_FEW: only %d strings" % len(names))
        return 2
    with open(args.strings, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    # deterministic: sort by sha256(seed + name)
    ranked = sorted(names,
                    key=lambda n: hashlib.sha256(
                        ("%s:%s" % (args.seed, n)).encode()).hexdigest())
    picked = ranked[:args.n]
    out("PASS", seed=args.seed, sha256=digest, total=len(names),
        sample=[{"name": n, "value": strings[n][:80]} for n in picked])
    return 0


# -------------------------------------------------------------- keystore
def cmd_keystore(args):
    tool = None
    if shutil.which("apksigner"):
        tool = ["apksigner", "verify", "--print-certs", args.apk]
        fp_re = re.compile(r"SHA-256.*?([0-9A-Fa-f:]{95,})")
    elif shutil.which("keytool"):
        tool = ["keytool", "-printcert", "-jarfile", args.apk]
        fp_re = re.compile(r"SHA256:\s*([0-9A-Fa-f:]{95,})")
    else:
        out("SKIP", detail="neither apksigner nor keytool on PATH")
        return 2
    try:
        p = subprocess.run(tool, capture_output=True, text=True, timeout=60)
    except Exception as e:
        out("ERROR", detail="tool failed: %s" % e)
        return 2
    fps = {m.group(1).upper().replace(" ", "")
           for m in fp_re.finditer(p.stdout + p.stderr)}
    if not fps:
        out("ISSUES", detail="no certificate fingerprints parsed",
            raw=(p.stdout + p.stderr)[:200])
        return 1
    expect = args.expect.upper().replace(" ", "")
    if expect in fps:
        out("PASS", fingerprint=expect, signers=len(fps))
        return 0
    out("ISSUES", detail="WRONG_KEY or UNSIGNED",
        expected=expect, found=sorted(fps))
    return 1


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(prog="qa.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("recon"); p.add_argument("apk")

    p = sub.add_parser("delta")
    p.add_argument("--old", required=True); p.add_argument("--new", required=True)

    p = sub.add_parser("glossary")
    p.add_argument("--glossary", required=True)
    p.add_argument("--strings", required=True)

    p = sub.add_parser("typo")
    p.add_argument("--strings", required=True)
    p.add_argument("--formal", action="store_true")

    p = sub.add_parser("glyph")
    p.add_argument("--font", required=True)
    p.add_argument("--strings", required=True)

    p = sub.add_parser("sample")
    p.add_argument("--strings", required=True)
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--seed", default="1")

    p = sub.add_parser("keystore")
    p.add_argument("--apk", required=True)
    p.add_argument("--expect", required=True,
                   help="expected SHA-256 cert fingerprint")

    args = ap.parse_args()
    fn = {"recon": cmd_recon, "delta": cmd_delta,
          "glossary": cmd_glossary, "typo": cmd_typo,
          "glyph": cmd_glyph, "sample": cmd_sample,
          "keystore": cmd_keystore}[args.cmd]
    sys.exit(fn(args))


if __name__ == "__main__":
    main()
