"""JAR patching pipeline for KuroPatch v2 (stdlib only).

On-device target is ``.jar`` ONLY. Full APK patching needs apktool, which
requires a real JVM and cannot run on Android -- the Android app refuses
``.apk`` files and the PC CLI is the way for APKs.

Pipeline:
  1. :func:`collect_strings` -- unzip a jar, gather every translatable
     value: ``.properties`` values, smali ``const-string`` literals,
     ``.class`` constant-pool strings, Gameloft string packs.
     Returns ``{value: [origins]}`` (deduped: each distinct string once).
  2. :func:`patch_jar` -- apply a ``{source_value: translation}`` mapping
     back into all four sources and repack, preserving entry order,
     compression, timestamps and MANIFEST.MF (stale .SF/.RSA signatures
     are stripped by :class:`JarPatcher`).

Collection uses only cheap safety filters (empty / too-short /
no-letters / technical-token). The full skip heuristics (screen dims,
versions, URLs, paths, placeholders) live in ``translate.guard`` and
are applied at translate time by the translate engine -- collection
stays permissive so nothing translatable is lost early.
"""

import os
import re
import tempfile
import shutil

from .patcher import (
    JarPatcher,
    iter_class_strings,
    iter_smali_literals,
    patch_class_strings,
    patch_smali_literals,
)
from .stringpacks import (
    build_resource,
    is_string_pack_entry,
    parse_resource,
)

__all__ = ["collect_strings", "patch_jar", "should_collect"]

# Values that are never UI text in any language.
_TECHNICAL_TOKENS = frozenset({
    "true", "false", "null", "none", "yes", "no", "on", "off",
    "utf-8", "utf8", "ascii", "en", "id", "en-us",
})
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_CJK_RE = re.compile(r"[\u2e80-\u9fff]", re.UNICODE)


def should_collect(text, minimum=3, translate_all=False):
    """Cheap safety filter for collection. Returns (ok, reason)."""
    stripped = text.strip()
    if not stripped:
        return False, "empty"
    if stripped.lower() in _TECHNICAL_TOKENS:
        return False, "technical-token"
    if len(stripped) < minimum and not _CJK_RE.search(stripped):
        return False, "too-short"
    if not _HAS_LETTER_RE.search(stripped):
        return False, "no-letters"
    return True, ""


def collect_strings(jar_path, minimum=3, translate_all=False, workspace=None):
    """Collect translatable strings from a .jar.

    Returns ``{"strings": {value: [origins]}, "total_seen": n,
    "files": {"properties": n, "smali": n, "classes": n, "packs": n}}``.
    """
    tmp = workspace or tempfile.mkdtemp(prefix="kpatch-collect-")
    patcher = JarPatcher(jar_path, workspace=tmp)
    strings = {}
    total_seen = 0
    counts = {"properties": 0, "smali": 0, "classes": 0, "packs": 0}

    def _add(value, origin):
        nonlocal total_seen
        value = value.strip()
        if not value:
            return
        total_seen += 1
        ok, _reason = should_collect(value, minimum, translate_all)
        if not ok:
            return
        bucket = strings.setdefault(value, [])
        if origin not in bucket:
            bucket.append(origin)

    try:
        patcher.extract()
        for path in patcher.get_properties_files():
            props = patcher.load_properties(path)
            if not len(props):
                continue
            counts["properties"] += 1
            rel = os.path.relpath(path, patcher.workspace)
            for entry in props.entries:
                _add(entry.value, f"{rel}::{entry.key}")
        for path in patcher.get_smali_files():
            counts["smali"] += 1
            with open(path, "r", encoding="utf-8",
                      errors="surrogateescape") as fh:
                content = fh.read()
            rel = os.path.relpath(path, patcher.workspace)
            for _s, _e, decoded in iter_smali_literals(content):
                _add(decoded, f"{rel}::const-string")
        for path in patcher.get_class_files():
            counts["classes"] += 1
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(path, patcher.workspace)
            for index, decoded in iter_class_strings(data):
                _add(decoded, f"{rel}::ldc#{index}")
        for root, _dirs, files in os.walk(patcher.workspace):
            for filename in files:
                ws_path = os.path.join(root, filename)
                rel = os.path.relpath(ws_path, patcher.workspace).replace(os.sep, "/")
                if not is_string_pack_entry(rel):
                    continue
                try:
                    with open(ws_path, "rb") as fh:
                        packs = parse_resource(fh.read())
                except (OSError, ValueError):
                    continue
                counts["packs"] += 1
                for pi, pack in enumerate(packs):
                    for si, value in enumerate(pack.strings):
                        _add(value, f"{rel}::pack{pi}#{si}")
        return {"strings": strings, "total_seen": total_seen, "files": counts}
    finally:
        if workspace is None:
            shutil.rmtree(tmp, ignore_errors=True)


def _coerce_str_dict(mapping):
    """Coerce a mapping to a real ``{str: str}`` dict.

    Chaquopy passes Kotlin/Java maps to Python as Java ``Map`` proxies
    (e.g. ``LinkedHashMap``), which have no ``.items()`` -- using them
    like a Python dict raises
    ``AttributeError: 'LinkedHashMap' object has no attribute 'items'``.
    Convert via ``entrySet()`` when needed. Plain dicts pass through.

    CRITICAL: a real Chaquopy Java ``Set`` proxy is NOT Python-iterable
    -- ``for e in entry_set`` raises
    ``TypeError: 'LinkedHashMap$LinkedEntrySet' object is not iterable``.
    Traverse it ONLY via the explicit Java ``Iterator`` protocol:
    ``iterator()`` / ``hasNext()`` / ``next()`` -- plain method calls,
    no Python iteration protocol.
    """
    if isinstance(mapping, dict):
        return mapping
    try:
        entry_set = mapping.entrySet()
    except AttributeError:
        # Not a Java Map either -- let dict() raise a clear TypeError.
        return dict(mapping)
    result = {}
    try:
        it = entry_set.iterator()
    except AttributeError:
        # entrySet already Python-iterable (non-Chaquopy callers).
        for e in entry_set:
            result[str(e.getKey())] = str(e.getValue())
        return result
    while it.hasNext():
        e = it.next()
        result[str(e.getKey())] = str(e.getValue())
    return result


def _match_translation(raw, translations):
    """Match a raw string value against the (stripped-keyed) translations.

    Collection strips values, so the translation map is keyed by the
    stripped form -- but the raw value may carry surrounding whitespace
    that is semantically meaningful (e.g. the format fragment
    ``", num samples: "`` in a .class pool, where the trailing space
    separates it from the number concatenated at runtime). Match on the
    stripped key and re-apply the original surrounding whitespace to
    the translation. Returns None when there is no translation or the
    translation equals the source (no change needed).
    """
    stripped = raw.strip()
    if not stripped:
        return None
    t = translations.get(stripped)
    if not t or t == stripped:
        return None
    leading = raw[:len(raw) - len(raw.lstrip())]
    trailing = raw[len(raw.rstrip()):]
    return leading + t + trailing


def patch_jar(jar_path, translations, out_path, workspace=None):
    """Apply {source_value: translation} to a .jar and repack.

    Translations are matched by decoded source value across .properties,
    smali literals, .class constant pools and string packs. Returns a
    stats dict: {"properties": n, "smali": n, "classes": n, "packs": n,
    "out": out_path}.
    """
    translations = _coerce_str_dict(translations)
    if not translations:
        raise ValueError("translations must be a non-empty dict")
    tmp = workspace or tempfile.mkdtemp(prefix="kpatch-patch-")
    patcher = JarPatcher(jar_path, workspace=tmp)
    stats = {"properties": 0, "smali": 0, "classes": 0, "packs": 0}
    try:
        patcher.extract()
        for path in patcher.get_properties_files():
            props = patcher.load_properties(path)
            changed = False
            for entry in props.entries:
                new = _match_translation(entry.value, translations)
                if new is not None:
                    entry.value = new
                    changed = True
                    stats["properties"] += 1
            if changed:
                patcher.save_properties(props, path)
        for path in patcher.get_smali_files():
            with open(path, "r", encoding="utf-8",
                      errors="surrogateescape") as fh:
                content = fh.read()
            # raw-keyed map: literals may carry surrounding whitespace
            smali_map = {}
            for _s, _e, decoded in iter_smali_literals(content):
                new = _match_translation(decoded, translations)
                if new is not None:
                    smali_map[decoded] = new
            if not smali_map:
                continue
            new_content, replaced, _unmatched = patch_smali_literals(content, smali_map)
            if replaced:
                patcher.write_file(path, [new_content])
                stats["smali"] += replaced
        for path in patcher.get_class_files():
            data = patcher.read_bytes(path)
            # raw-keyed map: pool strings may carry surrounding whitespace
            class_map = {}
            for _idx, decoded in iter_class_strings(data):
                new = _match_translation(decoded, translations)
                if new is not None:
                    class_map[decoded] = new
            if not class_map:
                continue
            new_data, replaced = patch_class_strings(data, class_map)
            if replaced:
                patcher.write_bytes(path, new_data)
                stats["classes"] += replaced
        for root, _dirs, files in os.walk(patcher.workspace):
            for filename in files:
                ws_path = os.path.join(root, filename)
                rel = os.path.relpath(ws_path, patcher.workspace).replace(os.sep, "/")
                if not is_string_pack_entry(rel):
                    continue
                try:
                    with open(ws_path, "rb") as fh:
                        packs = parse_resource(fh.read())
                except (OSError, ValueError):
                    continue
                dirty = False
                for pack in packs:
                    for i, value in enumerate(pack.strings):
                        new = _match_translation(value, translations)
                        if new is not None:
                            pack.strings[i] = new
                            stats["packs"] += 1
                            dirty = True
                if dirty:
                    try:
                        rebuilt = build_resource(packs)
                    except ValueError:
                        continue
                    with open(ws_path, "wb") as fh:
                        fh.write(rebuilt)
        out = patcher.rebuild(out_path)
        stats["out"] = out
        return stats
    finally:
        if workspace is None:
            shutil.rmtree(tmp, ignore_errors=True)
