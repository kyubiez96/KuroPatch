"""String resource parsing for KuroPatch v2 (stdlib only).

Supported formats:
  - Java ``.properties`` files (with ``\\uXXXX`` / line-continuation support)
  - Android ``strings.xml`` resource files (incl. ``<string-array>``,
    ``<plurals>``, inline HTML markup such as ``<b>``)
  - KuroPatch v2 ``.stringpack`` files (JSON envelope)

Design rules:
  - Values are treated as opaque text. Format specifiers (``%d``, ``%s``,
    ``%1$s``), inline markup (``<b>``) and escape sequences are preserved
    by exact round-tripping, never by re-interpretation.
  - Every parser is total on its format: malformed input raises
    ``StringParseError`` with a clear message -- never silent data loss.
  - ``<string-array>`` items map to keys ``name[0]``, ``name[1]``, ... and
    ``<plurals>`` items to ``name{quantity}`` (e.g. ``coins{other}``).
"""

import json
import os
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape


class StringParseError(ValueError):
    """Raised when a string resource file cannot be parsed."""


@dataclass
class StringEntry:
    """One translatable string."""

    key: str
    value: str
    comment: str = ""
    translatable: bool = True
    extra: dict = field(default_factory=dict)  # format-specific attributes


# ---------------------------------------------------------------------------
# .properties
# ---------------------------------------------------------------------------

_UNESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f", "\\": "\\"}


def _prop_unescape(text):
    """Apply Java-properties backslash escapes."""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "u":
            hexpart = text[i + 2:i + 6]
            if len(hexpart) == 4 and all(c in "0123456789abcdefABCDEF" for c in hexpart):
                out.append(chr(int(hexpart, 16)))
                i += 6
                continue
            out.append(nxt)  # malformed \u -- keep literal, never crash
            i += 2
            continue
        out.append(_UNESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _prop_escape_key(key):
    out = []
    for i, ch in enumerate(key):
        if ch == "\\":
            out.append("\\\\")
        elif ch in "=: \t\f" or (i == 0 and ch in "#!"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


def _prop_escape_value(value):
    out = []
    for i, ch in enumerate(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\f":
            out.append("\\f")
        elif ch in " \t\f" and i == 0:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _is_continuation(line):
    """True if the line ends with an odd number of backslashes."""
    n = 0
    for ch in reversed(line):
        if ch == "\\":
            n += 1
        else:
            break
    return n % 2 == 1


def _split_key_value(line):
    """Split a logical properties line into (key, value), unescaped."""
    i, n = 0, len(line)
    key_chars = []
    while i < n:
        ch = line[i]
        if ch == "\\" and i + 1 < n:
            key_chars.append(ch)
            key_chars.append(line[i + 1])
            i += 2
            continue
        if ch in "=: \t\f":
            break
        key_chars.append(ch)
        i += 1
    # skip separator: [ \t\f]* [:=]? [ \t\f]*
    while i < n and line[i] in " \t\f":
        i += 1
    if i < n and line[i] in "=: ":
        i += 1
    while i < n and line[i] in " \t\f":
        i += 1
    return _prop_unescape("".join(key_chars)), _prop_unescape(line[i:])


def parse_properties(data):
    """Parse Java .properties bytes -> list[StringEntry]."""
    if isinstance(data, bytes):
        text = data.decode("utf-8-sig")
    else:
        text = data
    entries = []
    comment_buf = []
    logical = ""
    continued = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw
        if continued:
            line = line.lstrip(" \t\f")
            logical += line
        else:
            logical = line
        if _is_continuation(logical):
            logical = logical[:-1]  # drop the continuation backslash
            continued = True
            continue
        continued = False
        stripped = logical.strip(" \t\f")
        if not stripped:
            comment_buf.clear()
            continue
        if stripped[0] in "#!":
            comment_buf.append(stripped[1:].strip())
            continue
        try:
            key, value = _split_key_value(logical)
        except Exception as exc:
            raise StringParseError(f"line {lineno}: cannot split key/value: {exc}")
        if not key:
            raise StringParseError(f"line {lineno}: empty key")
        entries.append(StringEntry(key=key, value=value,
                                   comment="\n".join(comment_buf)))
        comment_buf.clear()
    if continued:
        raise StringParseError("file ends with a line continuation")
    return entries


def dump_properties(entries):
    """Serialize entries -> .properties bytes (UTF-8)."""
    lines = []
    for e in entries:
        if e.comment:
            for cl in e.comment.splitlines():
                lines.append("# " + cl)
        lines.append(f"{_prop_escape_key(e.key)}={_prop_escape_value(e.value)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# strings.xml
# ---------------------------------------------------------------------------

_ARRAY_RE = re.compile(r"^(.*)\[(\d+)\]$")
_PLURAL_RE = re.compile(r"^(.*)\{([A-Za-z]+)\}$")


def _inner_xml(elem):
    """Reconstruct inner XML of <string>, preserving markup, escaping text.

    Note: ET.tostring() serializes an element *including* its tail text,
    so the tail must not be appended a second time.
    """
    parts = []
    if elem.text:
        parts.append(_xml_escape(elem.text))
    for child in elem:
        parts.append(ET.tostring(child, encoding="unicode"))
    return "".join(parts)


def _parse_with_comments(data):
    """Parse XML keeping comments; returns root element."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    try:
        return ET.fromstring(data, parser=parser)
    except ET.ParseError as exc:
        raise StringParseError(f"invalid XML: {exc}")


def parse_strings_xml(data):
    """Parse Android strings.xml bytes -> list[StringEntry]."""
    if isinstance(data, bytes):
        raw = data
    else:
        raw = data.encode("utf-8")
    root = _parse_with_comments(raw)
    if root.tag != "resources":
        raise StringParseError(f"expected <resources> root, got <{root.tag}>")
    entries = []
    pending_comment = ""
    for child in root:
        if child.tag is ET.Comment:
            pending_comment = (child.text or "").strip()
            continue
        if child.tag == "string":
            name = child.get("name")
            if not name:
                raise StringParseError("<string> without a name attribute")
            extra = {k: v for k, v in child.attrib.items()
                     if k not in ("name", "translatable")}
            entries.append(StringEntry(
                key=name,
                value=_inner_xml(child),
                comment=pending_comment,
                translatable=child.get("translatable", "true").lower() != "false",
                extra=extra,
            ))
        elif child.tag == "string-array":
            name = child.get("name")
            if not name:
                raise StringParseError("<string-array> without a name attribute")
            for i, item in enumerate(child.findall("item")):
                entries.append(StringEntry(
                    key=f"{name}[{i}]", value=_inner_xml(item),
                    comment=pending_comment if i == 0 else ""))
        elif child.tag == "plurals":
            name = child.get("name")
            if not name:
                raise StringParseError("<plurals> without a name attribute")
            for item in child.findall("item"):
                q = item.get("quantity", "other")
                entries.append(StringEntry(key=f"{name}{{{q}}}",
                                           value=_inner_xml(item)))
        # other resource types (colors, dimens, ...) are not translatable:
        # keep them out of the entry list so translators never touch them.
        pending_comment = ""
    return entries


def dump_strings_xml(entries):
    """Serialize entries -> strings.xml bytes (UTF-8, regrouped)."""
    # regroup arrays / plurals
    singles, arrays, plurals, order = [], {}, {}, []
    for e in entries:
        m = _ARRAY_RE.match(e.key)
        if m:
            base, idx = m.group(1), int(m.group(2))
            arrays.setdefault(base, {})[idx] = e
            if base not in order:
                order.append(("array", base))
            continue
        m = _PLURAL_RE.match(e.key)
        if m:
            base, q = m.group(1), m.group(2)
            plurals.setdefault(base, {})[q] = e
            if base not in order:
                order.append(("plural", base))
            continue
        singles.append(e)
        order.append(("single", e.key))

    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<resources>"]
    seen_bases = set()
    single_map = {e.key: e for e in singles}
    for kind, key in order:
        if kind == "single":
            e = single_map[key]
            attrs = "".join(f' {k}="{_xml_escape(v, {chr(34): chr(34)})}"'
                            for k, v in e.extra.items())
            if not e.translatable:
                attrs += ' translatable="false"'
            if e.comment:
                lines.append(f"    <!-- {_xml_escape(e.comment)} -->")
            lines.append(f'    <string name="{_xml_escape(key, {chr(34): chr(34)})}"{attrs}>'
                         f"{e.value}</string>")
        elif kind == "array" and key not in seen_bases:
            seen_bases.add(key)
            items = arrays[key]
            lines.append(f'    <string-array name="{_xml_escape(key, {chr(34): chr(34)})}">')
            for i in sorted(items):
                lines.append(f"        <item>{items[i].value}</item>")
            lines.append("    </string-array>")
        elif kind == "plural" and key not in seen_bases:
            seen_bases.add(key)
            items = plurals[key]
            lines.append(f'    <plurals name="{_xml_escape(key, {chr(34): chr(34)})}">')
            for q in ("zero", "one", "two", "few", "many", "other"):
                if q in items:
                    lines.append(f'        <item quantity="{q}">{items[q].value}</item>')
            for q in sorted(set(items) - {"zero", "one", "two", "few", "many", "other"}):
                lines.append(f'        <item quantity="{q}">{items[q].value}</item>')
            lines.append("    </plurals>")
    lines.append("</resources>")
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# .stringpack (KuroPatch v2 JSON envelope)
# ---------------------------------------------------------------------------

STRINGPACK_MAGIC = "kpatch-stringpack"
STRINGPACK_VERSION = 2


def parse_stringpack(data):
    """Parse .stringpack bytes -> list[StringEntry]."""
    if isinstance(data, bytes):
        text = data.decode("utf-8-sig")
    else:
        text = data
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StringParseError(f"invalid stringpack JSON: {exc}")
    if not isinstance(doc, dict) or doc.get("format") != STRINGPACK_MAGIC:
        raise StringParseError("not a kpatch-stringpack file")
    entries = []
    raw_entries = doc.get("entries")
    if not isinstance(raw_entries, list):
        raise StringParseError("stringpack 'entries' must be a list")
    for i, item in enumerate(raw_entries):
        if not isinstance(item, dict) or "key" not in item or "value" not in item:
            raise StringParseError(f"stringpack entry #{i} missing key/value")
        entries.append(StringEntry(
            key=str(item["key"]),
            value=str(item["value"]),
            comment=str(item.get("comment", "")),
            translatable=bool(item.get("translatable", True)),
        ))
    return entries


def dump_stringpack(entries):
    """Serialize entries -> .stringpack bytes (UTF-8 JSON)."""
    doc = {
        "format": STRINGPACK_MAGIC,
        "version": STRINGPACK_VERSION,
        "entries": [
            {"key": e.key, "value": e.value, "comment": e.comment,
             "translatable": e.translatable}
            for e in entries
        ],
    }
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def detect_format(path):
    """Detect string resource format from file name (and content for .json)."""
    low = os.path.basename(path).lower()
    if low.endswith(".properties"):
        return "properties"
    if low.endswith(".stringpack"):
        return "stringpack"
    if low.endswith(".xml"):
        return "strings-xml"
    if low.endswith(".json"):
        try:
            with open(path, "rb") as fh:
                head = fh.read(512).decode("utf-8", errors="ignore")
            if STRINGPACK_MAGIC in head:
                return "stringpack"
        except OSError:
            pass
    raise StringParseError(f"cannot detect string format for: {path}")


_PARSERS = {
    "properties": parse_properties,
    "strings-xml": parse_strings_xml,
    "stringpack": parse_stringpack,
}
_DUMPERS = {
    "properties": dump_properties,
    "strings-xml": dump_strings_xml,
    "stringpack": dump_stringpack,
}


def load_strings(path, format=None):
    """Load a string resource file -> list[StringEntry]."""
    fmt = format or detect_format(path)
    if fmt not in _PARSERS:
        raise StringParseError(f"unsupported format: {fmt}")
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.strip():
        raise StringParseError(f"empty string resource file: {path}")
    return _PARSERS[fmt](data)


def save_strings(path, entries, format=None):
    """Save entries to a string resource file (format auto-detected)."""
    fmt = format or detect_format(path)
    if fmt not in _DUMPERS:
        raise StringParseError(f"unsupported format: {fmt}")
    data = _DUMPERS[fmt](entries)
    with open(path, "wb") as fh:
        fh.write(data)
