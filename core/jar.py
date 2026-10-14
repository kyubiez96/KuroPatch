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


def _is_language_name(value):
    """True if `value` IS a language name ('English', 'DEUTSCH', ...).

    Language names in .class constant pools are NOT display-only text:
    Gameloft J2ME games (proven on Castlevania) store them in a static
    String[] and match them with String.equals() against the names
    read from TRAD.lang. Translating them ('English' -> 'Inggris')
    breaks the match, the language lookup finds nothing, and the game
    loads no strings at all (blank white screen in J2ME Loader).
    The trade-off is deliberate: a skipped language name costs one
    untranslated word; a translated one costs a bricked game.

    The guard is TARGET-INDEPENDENT by design: the game's internal
    lookup uses the ORIGINAL names whatever the target language, so
    translating them breaks the lookup for every target. The only
    cost is cosmetic -- a language menu may show 'Deutsch' instead
    of 'German' in a non-Indonesian build -- and the game still works.

    Also matches the Indonesian names themselves ('Inggris', 'Jerman',
    ...): a double-patch (patching an already-patched jar) or a game
    that ships with Indonesian labels must not have its language
    names re-translated either.

    Lazy import: core must not depend on translate at module load
    time (translate/ never imports core/, so no cycle, but keep the
    boundary explicit). Cached after the first call.
    """
    cached = getattr(_is_language_name, "_glossary", None)
    if cached is None:
        try:
            from translate.glossary import Glossary, LANGUAGE_NAMES_ID
        except ImportError:
            try:
                from ..translate.glossary import Glossary, LANGUAGE_NAMES_ID
            except ImportError:
                return False
        cached = Glossary()
        _is_language_name._glossary = cached
        # Reverse set: Indonesian names of languages. A value that IS
        # an Indonesian language name is still a language name (a
        # key), not text to translate.
        _is_language_name._id_names = frozenset(
            v.strip().lower() for v in LANGUAGE_NAMES_ID.values())
    if cached.lookup_language(value) is not None:
        return True
    base = _TRAILING_PAREN_RE.sub("", value.strip()).strip().lower()
    return base in _is_language_name._id_names

# Values that are never UI text in any language.
_TECHNICAL_TOKENS = frozenset({
    "true", "false", "null", "none", "yes", "no", "on", "off",
    "utf-8", "utf8", "ascii", "en", "id", "en-us",
})
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_CJK_RE = re.compile(r"[\u2e80-\u9fff]", re.UNICODE)
# Maximal printable runs (UTF-8 text incl. high bytes) for data-key
# extraction. Runs shorter than 3 bytes are noise.
_DATA_RUN_RE = re.compile(rb"[\x20-\x7e\x80-\xff]{3,}")
# Structural delimiters inside a data run: keys are stored as
# key=value, a;b;c, "quoted", key: value, ... Splitting runs on
# these finds keys that exact-run matching misses. Deliberately
# NOT space / '/' / '-' / '.' -- those appear inside prose and
# paths, and splitting on them would flag ordinary words as keys.
_DATA_DELIM_RE = re.compile(rb"[=;:,|\"'\t<>{}()\[\]&!]+")
# Keys are short identifiers; truncate over-long runs before matching.
_MAX_KEY_LEN = 200
# One trailing "(...)" suffix (region qualifier: "Inggris (US)").
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def should_collect(text, minimum=3, translate_all=False):
    """Cheap safety filter for collection. Returns (ok, reason).

    With ``translate_all`` the cheap filters are bypassed (only the
    empty check remains) -- the caller wants everything, including
    technical tokens and short strings. Previously the flag was
    accepted but had no effect (dead parameter).
    """
    stripped = text.strip()
    if not stripped:
        return False, "empty"
    if translate_all:
        return True, ""
    if stripped.lower() in _TECHNICAL_TOKENS:
        return False, "technical-token"
    if len(stripped) < minimum and not _CJK_RE.search(stripped):
        return False, "too-short"
    if not _HAS_LETTER_RE.search(stripped):
        return False, "no-letters"
    return True, ""


def _data_key_strings(workspace, candidates):
    """Candidates stored as separate entries in non-code resources: keys.

    A candidate string that appears as its own entry in a non-code
    resource (a length-prefixed / delimiter-separated entry in a data
    file) is a LOOKUP KEY, not UI text -- translating it breaks the
    lookup. Proven on-device: Castlevania's language names
    (English/Deutsch/...) live in ``TRAD.lang`` (the language index);
    the patched .class said "Inggris" while ``TRAD.lang`` still said
    "English", so the language never loaded and the game showed a
    blank screen in J2ME Loader.

    Only maximal printable runs are matched, so a candidate embedded
    in prose (a help text inside a .dat) is NOT flagged -- keys are
    stored as separate entries, prose is not. Runs are ALSO split on
    structural delimiters (``= ; : , | " '`` ...) so ``key=value``,
    ``a;b;c`` and ``"quoted"`` entries are caught -- exact-run
    matching alone missed all three (proven by adversarial probe).
    Spaces are deliberately NOT delimiters: splitting on them would
    flag ordinary prose words as keys. .class/.properties/smali
    (translation sources), string packs (translation targets) and
    META-INF metadata are excluded from the data blob. False positives
    fail SAFE: an over-skipped string just stays untranslated, the
    game still works. False negatives remain possible for keys stored
    in UTF-16 or other non-UTF-8 encodings.
    """
    cand_bytes = {}
    for c in candidates:
        try:
            cand_bytes[c.encode("utf-8")] = c
        except UnicodeEncodeError:
            continue
    if not cand_bytes:
        return set()
    keys = set()
    for root, _dirs, files in os.walk(workspace):
        for filename in files:
            ws_path = os.path.join(root, filename)
            rel = os.path.relpath(ws_path, workspace).replace(os.sep, "/")
            if rel.startswith("META-INF/"):
                continue
            if rel.lower().endswith((".class", ".properties", ".smali")):
                continue
            if is_string_pack_entry(rel):
                continue
            try:
                with open(ws_path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            for m in _DATA_RUN_RE.finditer(raw):
                run = m.group(0)
                if len(run) > _MAX_KEY_LEN:
                    run = run[:_MAX_KEY_LEN]
                hit = cand_bytes.get(run)
                if hit is not None:
                    keys.add(hit)
                    continue
                # Token-split: a key stored as key=value, a;b;c or
                # "quoted" is inside a longer run, not equal to it.
                # Tokens are stripped: "item: SwordOfFire" splits to
                # " SwordOfFire" (leading space after the colon) and
                # must still match the stripped candidate.
                for tok in _DATA_DELIM_RE.split(run):
                    tok = tok.strip()
                    if not tok:
                        continue
                    hit = cand_bytes.get(tok)
                    if hit is not None:
                        keys.add(hit)
    return keys


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
        # Language names are logic keys, never display text (see
        # _is_language_name) -- enforced HERE, once, for every
        # source (.properties / smali / .class / packs). The 4fb0954
        # fix only guarded .class pools; a game storing its language
        # names in .properties or smali would brick the same way.
        if _is_language_name(value):
            return
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
        # Drop data-key strings: candidates stored as separate entries
        # in non-code resources are lookup keys, not UI text.
        dropped_keys = 0
        if strings:
            keys = _data_key_strings(patcher.workspace, set(strings))
            for k in keys:
                if strings.pop(k, None) is not None:
                    dropped_keys += 1
        return {"strings": strings, "total_seen": total_seen,
                "files": counts, "data_keys_dropped": dropped_keys}
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
    # Language names are lookup keys, never display text (see
    # _is_language_name) -- enforced HERE, once, for every source
    # (.properties / smali / .class / packs). A hand-made / stale
    # translation map containing language names must not reach ANY
    # of them: collect already skips them, but patch must be safe on
    # its own. The 4fb0954 fix only guarded .class pools.
    if _is_language_name(stripped):
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
    # Values MUST be strings. A caller passing collect's raw
    # {value: [origins]} dict would otherwise die deep in
    # _match_translation with a raw TypeError (str + list).
    # Reject loudly at the boundary instead.
    for key, value in translations.items():
        if not isinstance(value, str):
            raise ValueError(
                f"translation for {key!r} is {type(value).__name__}, "
                "not a string -- did you pass a `collect --out` file? "
                "That file maps values to origin lists, not translations."
            )
    # Language names are logic keys in EVERY source, not just .class
    # pools: a hand-made / stale map containing them must not rewrite
    # a .properties `language.name=English` or a smali const-string
    # "Deutsch" either. Drop them from the map up front so all four
    # patch loops are covered at once.
    translations = {k: v for k, v in translations.items()
                    if not _is_language_name(k)}
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
                replaced_here = 0
                for pack in packs:
                    for i, value in enumerate(pack.strings):
                        new = _match_translation(value, translations)
                        if new is not None:
                            pack.strings[i] = new
                            replaced_here += 1
                            dirty = True
                if dirty:
                    try:
                        rebuilt = build_resource(packs)
                    except ValueError:
                        continue
                    # Only count after the rebuild succeeded -- a failed
                    # build leaves the file untouched, so the stats must
                    # not claim replacements that never landed.
                    stats["packs"] += replaced_here
                    with open(ws_path, "wb") as fh:
                        fh.write(rebuilt)
        out = patcher.rebuild(out_path)
        stats["out"] = out
        return stats
    finally:
        if workspace is None:
            shutil.rmtree(tmp, ignore_errors=True)
