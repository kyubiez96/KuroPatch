"""Apply translations and rebuild APKs for KuroPatch v2 (stdlib only).

Pipeline:
  1. :func:`apply_translations` -- replace string values in an extracted tree.
  2. :func:`repack_apk`          -- zip the tree back into an APK
     (``apktool b`` when available, manual ZIP otherwise).
  3. :func:`zipalign_apk`        -- 4-byte align (skipped with a warning when
     ``zipalign`` is missing).
  4. :func:`sign_apk`            -- sign with ``apksigner`` (explicit error
     when the tool is missing).

Translations are a plain ``dict[str, str]`` mapping string keys to their new
values. Translation *providers* live in the sibling ``translate/`` package;
this module never touches the network or API keys.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

from . import apk as _apk
from . import strings as _strings


def _run(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except FileNotFoundError:
        raise RuntimeError(f"tool not found: {cmd[0]}")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()[:500]}")
    return proc


def apply_translations(work_dir, translations, lang=None):
    """Apply a {key: value} dict to every string resource under work_dir.

    For Android ``res/values/strings.xml`` with ``lang`` set, translations
    are written to ``res/values-<lang>/strings.xml`` (proper Android
    localization) instead of overwriting the source. Other formats are
    patched in place.

    Returns a stats dict: {"files": n, "applied": n, "unmatched": [keys]}.
    Raises ValueError on an empty dict, FileNotFoundError when no string
    resources exist.
    """
    if not isinstance(translations, dict) or not translations:
        raise ValueError("translations must be a non-empty dict")
    files = _apk.list_string_resources(work_dir)
    if not files:
        raise FileNotFoundError(f"no string resources found under {work_dir}")

    seen = set()
    stats = {"files": 0, "applied": 0, "unmatched": []}
    for path in files:
        entries = _strings.load_strings(path)
        target = path
        if (lang and os.path.basename(path) == "strings.xml"
                and os.path.basename(os.path.dirname(path)) == "values"):
            res_dir = os.path.dirname(os.path.dirname(path))
            lang_dir = os.path.join(res_dir, f"values-{lang}")
            os.makedirs(lang_dir, exist_ok=True)
            target = os.path.join(lang_dir, "strings.xml")
        changed = False
        for e in entries:
            if e.key in translations and e.translatable:
                e.value = str(translations[e.key])
                seen.add(e.key)
                changed = True
                stats["applied"] += 1
        if changed:
            _strings.save_strings(target, entries)
            stats["files"] += 1
    stats["unmatched"] = sorted(set(translations) - seen)
    return stats


def repack_apk(work_dir, out_apk, use_apktool=True):
    """Repack an extracted tree into an APK. Returns out_apk.

    Uses ``apktool b`` when the tree came from apktool (``apktool.yml``
    present) and the tool exists; otherwise builds the ZIP manually
    (``resources.arsc`` stored uncompressed, everything else deflated).
    """
    if not os.path.isdir(work_dir):
        raise FileNotFoundError(f"not a directory: {work_dir}")
    out_apk = os.path.abspath(out_apk)
    os.makedirs(os.path.dirname(out_apk) or ".", exist_ok=True)
    if (use_apktool and _apk.apktool_available()
            and os.path.isfile(os.path.join(work_dir, "apktool.yml"))):
        _run(["apktool", "b", work_dir, "-o", out_apk])
        return out_apk
    with zipfile.ZipFile(out_apk, "w") as zf:
        for root, _dirs, files in os.walk(work_dir):
            for fname in sorted(files):
                full = os.path.join(root, fname)
                arc = os.path.relpath(full, work_dir).replace(os.sep, "/")
                compress = (zipfile.ZIP_STORED
                            if fname == "resources.arsc"
                            else zipfile.ZIP_DEFLATED)
                zf.write(full, arc, compress_type=compress)
    return out_apk


def zipalign_apk(apk_path, out_path=None):
    """4-byte align an APK with ``zipalign``. Returns the aligned APK path.

    When ``zipalign`` is not installed the APK is returned unaligned and a
    warning goes to stderr (alignment is an optimization, not a hard error).
    """
    if not os.path.isfile(apk_path):
        raise FileNotFoundError(f"APK not found: {apk_path}")
    exe = shutil.which("zipalign")
    if not exe:
        print("warning: zipalign not found on PATH, skipping alignment",
              file=sys.stderr)
        return apk_path
    if out_path is None or os.path.abspath(out_path) == os.path.abspath(apk_path):
        tmp = apk_path + ".zipalign.tmp"
        _run([exe, "-f", "-p", "4", apk_path, tmp])
        os.replace(tmp, apk_path)
        return apk_path
    _run([exe, "-f", "-p", "4", apk_path, out_path])
    return out_path


def _ensure_debug_keystore(path):
    keytool = shutil.which("keytool")
    if not keytool:
        raise RuntimeError(
            "keytool not found: cannot generate a debug keystore. "
            "Install a JDK or pass --keystore/--alias explicitly.")
    if not os.path.isfile(path):
        _run([keytool, "-genkeypair", "-keystore", path,
              "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", 2048,
              "-validity", "10950", "-storepass", "android",
              "-keypass", "android",
              "-dname", "CN=Android Debug,O=Android,C=US"])
    return path


def sign_apk(apk_path, keystore=None, alias="androiddebugkey",
             storepass="android", keypass=None, out_path=None):
    """Sign an APK with ``apksigner``. Returns the signed APK path.

    Without ``keystore`` a debug keystore is generated via ``keytool``.
    Raises RuntimeError with a clear message when ``apksigner`` is missing.
    """
    if not os.path.isfile(apk_path):
        raise FileNotFoundError(f"APK not found: {apk_path}")
    exe = shutil.which("apksigner")
    if not exe:
        raise RuntimeError(
            "apksigner not found on PATH: install Android build-tools "
            "(or sign the APK manually) to complete the build.")
    if keystore is None:
        keystore = _ensure_debug_keystore(
            os.path.join(tempfile.gettempdir(), "kpatch-debug.keystore"))
        alias, storepass = "androiddebugkey", "android"
    elif not os.path.isfile(keystore):
        raise FileNotFoundError(f"keystore not found: {keystore}")
    cmd = [exe, "sign", "--ks", keystore, "--ks-key-alias", alias,
           "--ks-pass:pass", storepass]
    if keypass:
        cmd += ["--key-pass:pass", keypass]
    if out_path is None or os.path.abspath(out_path) == os.path.abspath(apk_path):
        tmp = apk_path + ".signed.tmp"
        _run(cmd + ["--out", tmp, apk_path])
        os.replace(tmp, apk_path)
        return apk_path
    _run(cmd + ["--out", out_path, apk_path])
    return out_path


def patch_apk(apk_path, translations, out_apk, lang="id", work_dir=None,
              sign=True, keystore=None):
    """Full pipeline: extract -> translate -> repack -> align -> sign.

    Returns {"apk": out_apk, "stats": {...}}. Raises on any failure;
    the temporary work dir is always cleaned up.
    """
    if not isinstance(translations, dict) or not translations:
        raise ValueError("translations must be a non-empty dict")
    tmp = work_dir or tempfile.mkdtemp(prefix="kpatch-")
    try:
        _apk.extract_apk(apk_path, tmp)
        stats = apply_translations(tmp, translations, lang=lang)
        repack_apk(tmp, out_apk)
        zipalign_apk(out_apk)
        if sign:
            sign_apk(out_apk, keystore=keystore)
        return {"apk": os.path.abspath(out_apk), "stats": stats}
    finally:
        if work_dir is None:
            shutil.rmtree(tmp, ignore_errors=True)
