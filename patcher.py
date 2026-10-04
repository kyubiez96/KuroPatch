"""Archive + Java ``.properties`` patching for KuroPatch.

This module knows two things, and nothing else:

1. how to take a ``.jar``/``.apk`` apart without corrupting it, and
2. how to read/write Java ``.properties`` resources losslessly.

The rest of the project (``main.py``, ``core.py``) builds on top of this.

Notes on correctness that the previous version got wrong:

* Extraction is guarded against Zip Slip (``../`` entries writing outside the
  workspace).
* A rebuild preserves the original entry order, compression method, timestamps
  and ``META-INF/MANIFEST.MF`` position, and it uses ``ZIP_DEFLATED``.
* Only files that were actually modified are re-read from disk; every other
  entry is copied byte-for-byte out of the source archive.
* ``.properties`` files are parsed into structured entries so that comments,
  separators, whitespace and escape sequences survive a round trip.
"""

from __future__ import annotations

import os
import re
import shutil
import zipfile
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = [
    "JarPatcher",
    "PropertiesFile",
    "PropertiesEntry",
    "PropertiesCodecError",
    "smali_unescape",
    "smali_escape",
    "iter_smali_literals",
    "patch_smali_literals",
]

# Characters that may appear literally in a properties key.
_KEY_RE = re.compile(r"^(?P<lead>[ \t\f]*)(?P<key>(?:[^=:\s]|\\.)+)[ \t\f]*(?P<sep>[=:])(?P<mid>[ \t\f]*)(?P<rest>.*)$")

_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "f": "\f",
    "b": "\b",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "#": "#",
    "!": "!",
    "=": "=",
    ":": ":",
    " ": " ",
    "u": None,  # handled specially: \uXXXX
}

# Inside a *value* only these two characters would silently change the meaning
# of the file (they start a comment). Everything else may stay literal, which
# keeps generated files readable. Leading whitespace is escaped separately.
_VALUE_SPECIAL = frozenset("#!")

_UNESCAPE_RE = re.compile(r"\\(u[0-9a-fA-F]{4}|.)", re.DOTALL)


class PropertiesCodecError(ValueError):
    """Raised when a ``.properties`` resource cannot be decoded."""


# ---------------------------------------------------------------------------
# Escape handling
# ---------------------------------------------------------------------------
def unescape(text: str) -> str:
    """Turn the escaped form of a ``.properties`` value into real text."""
    if "\\" not in text:
        return text

    def repl(match: re.Match) -> str:
        token = match.group(1)
        head = token[0]
        if head == "u" and len(token) == 5:
            try:
                return chr(int(token[1:], 16))
            except ValueError:  # pragma: no cover - regex guarantees hex
                return match.group(0)
        return _ESCAPES.get(head, head)

    return _UNESCAPE_RE.sub(repl, text)


def _needs_backslash(char: str) -> bool:
    return char in _VALUE_SPECIAL


def escape_value(text: str, charset: str = "utf-8") -> str:
    """Escape real text so it can be stored as a ``.properties`` value.

    ``charset`` decides how characters are represented: anything the charset
    cannot encode becomes a ``\\uXXXX`` escape, which keeps a UTF-8 translation
    readable by Java 8 (ISO-8859-1) while staying plain UTF-8 for Java 9+.

    Every character in the leading whitespace run is escaped, not just the first
    one: ``Properties.load`` skips whitespace after the separator, so escaping
    only the first space would silently drop the rest.
    """
    out: List[str] = []
    in_leading = True

    for char in text:
        if char == "\\":
            piece = "\\\\"
        elif char == "\n":
            piece = "\\n"
        elif char == "\r":
            piece = "\\r"
        elif char == "\t":
            piece = "\\t"
        elif char == "\f":
            piece = "\\f"
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            piece = "\\u%04x" % ord(char)
        elif _needs_backslash(char):
            piece = "\\" + char
        else:
            try:
                char.encode(charset)
            except UnicodeEncodeError:
                piece = "\\u%04x" % ord(char)
            else:
                piece = char

        # A bare leading space would be skipped by Properties.load, so it needs
        # an explicit "\ ". A tab or form feed is already written as an escape,
        # which starts the value, so those need no extra prefix.
        if in_leading and char == " ":
            piece = "\\" + piece
        elif char not in " ":
            in_leading = False

        out.append(piece)

    return "".join(out)


# ---------------------------------------------------------------------------
# Structured properties model
# ---------------------------------------------------------------------------
class PropertiesEntry:
    """One logical ``key=value`` entry, plus everything needed to write it back."""

    __slots__ = (
        "key", "value", "original", "sep", "lead", "mid", "trail", "comment", "lineno", "multiline",
    )

    def __init__(
        self,
        key: str,
        value: str,
        sep: str = "=",
        lead: str = "",
        mid: str = "",
        trail: str = "",
        comment: str = "",
        lineno: int = 0,
        multiline: bool = False,
    ) -> None:
        self.key = key
        self.value = value
        self.original = value
        self.sep = sep
        self.lead = lead
        self.mid = mid
        self.trail = trail
        self.comment = comment
        self.lineno = lineno
        self.multiline = multiline

    @property
    def dirty(self) -> bool:
        return self.value != self.original

    @property
    def escaped_key(self) -> str:
        return escape_value(self.key)

    @property
    def escaped_value(self) -> str:
        return escape_value(self.value)

    def render(self, charset: str = "utf-8") -> str:
        return f"{self.lead}{self.escaped_key}{self.sep}{self.mid}{self.escape(charset)}{self.trail}{self.comment}"

    def escape(self, charset: str = "utf-8") -> str:
        return escape_value(self.value, charset)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PropertiesEntry(key={self.key!r}, value={self.value!r})"


class PropertiesFile:
    """A parsed ``.properties`` resource.

    ``raw_lines`` holds every physical line exactly as read; ``entries`` holds
    the structured key/value pairs. Rendering uses :meth:`to_bytes`, which
    substitutes only the entries whose value changed and keeps everything else
    byte-identical.
    """

    def __init__(self, entries: Sequence[PropertiesEntry], raw: bytes, charset: str) -> None:
        self.entries: List[PropertiesEntry] = list(entries)
        self.raw = raw
        self.charset = charset

        # A pure-ASCII resource decodes identically on every Java version, so we
        # keep it that way and escape anything new as \uXXXX. Writing raw UTF-8
        # into it would silently change meaning for a Java 8 reader.
        self.write_charset = "ascii" if charset == "utf-8" and not any(byte > 0x7F for byte in raw) else charset

    # -- parsing -----------------------------------------------------------
    @classmethod
    def loads(cls, data: bytes) -> "PropertiesFile":
        try:
            text = data.decode("utf-8")
            charset = "utf-8"
        except UnicodeDecodeError:
            # Java <= 8 reads these as ISO-8859-1, which never fails.
            text = data.decode("iso-8859-1")
            charset = "iso-8859-1"

        entries: List[PropertiesEntry] = []
        lines = text.splitlines(keepends=True)
        index = 0
        lineno = 1
        while index < len(lines):
            raw_line = lines[index]
            stripped = raw_line.strip()
            if not stripped or stripped[0] in "#!":
                index += 1
                lineno += 1
                continue

            match = _KEY_RE.match(raw_line.rstrip("\r\n"))
            if not match:
                index += 1
                lineno += 1
                continue

            body = match.group("rest")
            value_part, comment = cls._split_comment(body)
            multiline = False

            # A value ending in an odd number of backslashes continues on the
            # next physical line.
            consumed = 1
            while _ends_with_continuation(value_part) and index + consumed < len(lines):
                nxt = lines[index + consumed].rstrip("\r\n")
                value_part = value_part[:-1] + nxt
                consumed += 1
                multiline = True

            trail = ""
            if comment:
                stripped_comment = value_part.rstrip(" \t\f")
                trail = value_part[len(stripped_comment):]
                value_part = stripped_comment

            entries.append(
                PropertiesEntry(
                    key=unescape(match.group("key")),
                    value=unescape(value_part),
                    sep=match.group("sep"),
                    lead=match.group("lead"),
                    mid=match.group("mid"),
                    trail=trail,
                    comment=comment,
                    lineno=lineno,
                    multiline=multiline,
                )
            )
            index += consumed
            lineno += consumed

        return cls(entries, data, charset)

    @staticmethod
    def _split_comment(body: str) -> Tuple[str, str]:
        """Split a value from a trailing ``#``/``!`` comment, respecting escapes."""
        escaped = False
        for position, char in enumerate(body):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char in "#!":
                return body[:position], body[position:]
        return body, ""

    # -- mutation ----------------------------------------------------------
    def __iter__(self):
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, key: str) -> Optional[str]:
        for entry in self.entries:
            if entry.key == key:
                return entry.value
        return None

    def items(self):
        return [(entry.key, entry.value) for entry in self.entries]

    def keys(self) -> List[str]:
        return [entry.key for entry in self.entries]

    def set(self, key: str, value: str) -> bool:
        """Replace a value. Returns True when the key existed."""
        for entry in self.entries:
            if entry.key == key:
                entry.value = value
                return True
        return False

    def changed(self) -> List[PropertiesEntry]:
        return [e for e in self.entries if e.dirty]

    # -- rendering ---------------------------------------------------------
    def to_bytes(self) -> bytes:
        """Serialise, replacing only entries whose value changed.

        The original physical layout is preserved: unchanged lines are emitted
        byte-for-byte, a changed entry replaces the physical line it started on,
        and any continuation lines it used to span are dropped (the new value is
        escaped onto a single line instead).
        """
        if not self.changed():
            return self.raw

        text = self.raw.decode("utf-8") if self.charset == "utf-8" else self.raw.decode("iso-8859-1")
        physical = text.splitlines(keepends=True)

        # entry.lineno is 1-based, physical indices are 0-based.
        replacements: Dict[int, str] = {}
        spans: Dict[int, int] = {}
        for entry in self.entries:
            if not entry.dirty:
                continue
            start = entry.lineno - 1
            if start < 0 or start >= len(physical):
                continue

            consumed = 1
            body = physical[start].rstrip("\r\n")
            match = _KEY_RE.match(body)
            if match:
                value_part, _ = self._split_comment(match.group("rest"))
                while _ends_with_continuation(value_part) and start + consumed < len(physical):
                    value_part = value_part[:-1] + physical[start + consumed].rstrip("\r\n")
                    consumed += 1

            replacements[start] = entry.render(self.write_charset)
            spans[start] = consumed

        rebuilt: List[str] = []
        index = 0
        while index < len(physical):
            if index in replacements:
                line = physical[index]
                newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                rebuilt.append(replacements[index] + newline)
                index += spans.get(index, 1)
                continue
            rebuilt.append(physical[index])
            index += 1

        return "".join(rebuilt).encode(self.write_charset, errors="strict")

    def to_text(self) -> str:
        """The decoded text a Java ``Properties`` reader would see."""
        return "".join(
            f"{e.lead}{unescape(e.escaped_key)}{e.sep}{e.mid}{unescape(e.escape(self.write_charset))}{e.trail}{e.comment}\n"
            for e in self.entries
        )


def _ends_with_continuation(value_part: str) -> bool:
    count = 0
    for char in reversed(value_part):
        if char == "\\":
            count += 1
        else:
            break
    return count % 2 == 1


# ---------------------------------------------------------------------------
# Smali literals
# ---------------------------------------------------------------------------
# const-string v0, "Hello"
# \x22 is a double quote, used so no quote escaping is needed inside the raw strings.
_CONST_STRING_RE = re.compile(
    r'(?P<head>\bconst-string(?:\s|/[\w$.-]+)*\s+[vp]\d+\s*,\s*\x22)(?P<body>(?:\\.|[^\x22\\])*)(?P<tail>\x22)'
)
_SMALI_LITERAL_RE = re.compile(r'\x22((?:\\.|[^\x22\\\n])*)\x22')

_SMALI_ESCAPES = {'"': '"', "'": "'", "\\": "\\", "n": "\n", "t": "\t", "b": "\b", "f": "\f", "r": "\r"}


def smali_unescape(literal: str) -> str:
    """Decode the body of a smali string literal (without the quotes)."""
    def repl(match: re.Match) -> str:
        token = match.group(1)
        if token[0] == "u" and len(token) == 5:
            try:
                return chr(int(token[1:], 16))
            except ValueError:
                return match.group(0)
        return _SMALI_ESCAPES.get(token, token)

    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", repl, literal, flags=re.DOTALL)


def smali_escape(text: str) -> str:
    """Encode text as a smali string body."""
    out: List[str] = []
    for char in text:
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif char == "\b":
            out.append("\\b")
        elif char == "\f":
            out.append("\\f")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append("\\u%04x" % ord(char))
        else:
            out.append(char)
    return "".join(out)


def iter_smali_literals(content: str):
    """Yield ``(start, end, decoded_text)`` for every const-string literal.

    Offsets address the *body* of the literal, so a caller can splice a
    replacement in without touching the surrounding instruction.
    """
    for match in _CONST_STRING_RE.finditer(content):
        body = match.group("body")
        start = match.start("body")
        yield start, start + len(body), smali_unescape(body)


def patch_smali_literals(content: str, mapping: Dict[str, str]) -> Tuple[str, int, int]:
    """Replace smali literals using ``mapping`` (decoded text -> translation).

    Returns ``(new_content, replaced, unmatched)``. Replacement happens right
    to left so earlier offsets stay valid, and every literal is compared
    *decoded* so ``\\u00e9`` and ``é`` match the same entry.
    """
    edits: List[Tuple[int, int, str]] = []
    matched: set = set()
    for start, end, decoded in iter_smali_literals(content):
        if decoded in mapping:
            edits.append((start, end, smali_escape(mapping[decoded])))
            matched.add(decoded)

    if not edits:
        return content, 0, len(mapping) - len(matched)

    pieces: List[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits):
        pieces.append(content[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(content[cursor:])
    return "".join(pieces), len(edits), len(mapping) - len(matched)


# ---------------------------------------------------------------------------
# Java .class constant-pool strings
# ---------------------------------------------------------------------------
# J2ME games (the bulk of real-world .jar games) keep UI text as string
# constants inside .class files — there are no .properties or .smali files.
# Only CONSTANT_String entries (tag 8, the ones an `ldc` can actually load)
# are translation candidates. A Utf8 that is *also* referenced as a class /
# method / field name is left alone: the pool de-duplicates identical
# strings, and translating a shared entry would rename code.
def _mutf8_decode(data: bytes) -> str:
    """Decode Java modified UTF-8 (``\\0`` as ``C0 80``, astral as surrogate pairs)."""
    text = data.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
    out: List[str] = []
    i = 0
    while i < len(text):
        code = ord(text[i])
        if 0xD800 <= code <= 0xDBFF and i + 1 < len(text):
            low = ord(text[i + 1])
            if 0xDC00 <= low <= 0xDFFF:
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _mutf8_encode(text: str) -> bytes:
    """Encode to Java modified UTF-8."""
    out = bytearray()
    for char in text:
        code = ord(char)
        if code == 0:
            out += b"\xc0\x80"
        elif code < 0x80:
            out.append(code)
        elif code < 0x800:
            out.append(0xC0 | (code >> 6))
            out.append(0x80 | (code & 0x3F))
        elif code < 0x10000:
            out.append(0xE0 | (code >> 12))
            out.append(0x80 | ((code >> 6) & 0x3F))
            out.append(0x80 | (code & 0x3F))
        else:
            code -= 0x10000
            for surrogate in (0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)):
                out.append(0xE0 | (surrogate >> 12))
                out.append(0x80 | ((surrogate >> 6) & 0x3F))
                out.append(0x80 | (surrogate & 0x3F))
    return bytes(out)


def _parse_constant_pool(data: bytes) -> Tuple[Dict[int, Tuple[int, str]], set]:
    """Parse a .class constant pool.

    Returns ``(utf8, string_only)`` where ``utf8`` maps a pool index to
    ``(length_field_offset, decoded_string)`` and ``string_only`` is the set
    of Utf8 indices referenced *only* by ``CONSTANT_String`` entries — safe
    to translate.
    """
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        raise ValueError("not a class file")
    count = int.from_bytes(data[8:10], "big")
    pos = 10
    utf8: Dict[int, Tuple[int, str]] = {}
    string_refs: set = set()
    code_refs: set = set()  # Utf8 indices used as names/descriptors: never translate
    index = 1
    while index < count:
        if pos >= len(data):
            raise ValueError("truncated constant pool")
        tag = data[pos]
        if tag == 1:  # Utf8
            length = int.from_bytes(data[pos + 1 : pos + 3], "big")
            raw = data[pos + 3 : pos + 3 + length]
            if len(raw) != length:
                raise ValueError("truncated Utf8 entry")
            utf8[index] = (pos + 1, _mutf8_decode(raw))
            pos += 3 + length
        elif tag == 8:  # String
            string_refs.add(int.from_bytes(data[pos + 1 : pos + 3], "big"))
            pos += 3
        elif tag in (7, 16, 19, 20):  # Class, MethodType, Module, Package
            code_refs.add(int.from_bytes(data[pos + 1 : pos + 3], "big"))
            pos += 3
        elif tag == 15:  # MethodHandle
            pos += 4
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            code_refs.add(int.from_bytes(data[pos + 1 : pos + 3], "big"))
            code_refs.add(int.from_bytes(data[pos + 3 : pos + 5], "big"))
            pos += 5
        elif tag in (5, 6):  # Long, Double: occupy two pool slots
            pos += 9
            index += 1
        else:
            raise ValueError(f"unknown constant pool tag {tag} at index {index}")
        index += 1
    return utf8, (string_refs - code_refs)


def iter_class_strings(data: bytes):
    """Yield ``(pool_index, decoded)`` for every loadable string constant.

    Never raises: a malformed class yields nothing instead of killing the run.
    """
    try:
        utf8, candidates = _parse_constant_pool(data)
    except (ValueError, IndexError):
        return
    for idx in sorted(candidates):
        entry = utf8.get(idx)
        if entry is not None:
            yield idx, entry[1]


def patch_class_strings(data: bytes, mapping: Dict[str, str]) -> Tuple[bytes, int]:
    """Replace string constants per ``mapping`` (decoded -> translation).

    Returns ``(new_data, replaced)``. Only the Utf8 entry's own length prefix
    changes, so no other pool offsets are disturbed. Malformed input is
    returned unchanged.
    """
    try:
        utf8, candidates = _parse_constant_pool(data)
    except (ValueError, IndexError):
        return data, 0
    edits: List[Tuple[int, int, bytes]] = []
    for idx in candidates:
        entry = utf8.get(idx)
        if entry is None:
            continue
        length_pos, decoded = entry
        if decoded in mapping:
            new_raw = _mutf8_encode(mapping[decoded])
            old_len = int.from_bytes(data[length_pos : length_pos + 2], "big")
            edits.append((length_pos, 2 + old_len, len(new_raw).to_bytes(2, "big") + new_raw))
    if not edits:
        return data, 0
    out = bytearray(data)
    for length_pos, old_total, new_entry in sorted(edits, reverse=True):
        out[length_pos : length_pos + old_total] = new_entry
    return bytes(out), len(edits)


# ---------------------------------------------------------------------------
# Archive patching
# ---------------------------------------------------------------------------
class JarPatcher:
    """Extract, modify and rebuild a ``.jar``/``.apk`` archive."""

    def __init__(self, jar_path: str, workspace: str = "jar_workspace") -> None:
        if not os.path.isfile(jar_path):
            raise FileNotFoundError(f"archive not found: {jar_path}")
        self.jar_path = jar_path
        self.workspace = workspace
        os.makedirs(self.workspace, exist_ok=True)

        self._infos: List[zipfile.ZipInfo] = []
        self._entries: Dict[str, str] = {}  # posix archive name -> absolute path
        self._encodings: Dict[str, str] = {}

    # -- extract -----------------------------------------------------------
    def extract(self) -> str:
        """Extract the archive into the workspace (Zip-Slip safe)."""
        if os.path.isdir(self.workspace):
            shutil.rmtree(self.workspace)
        os.makedirs(self.workspace, exist_ok=True)

        root = os.path.realpath(self.workspace)
        with zipfile.ZipFile(self.jar_path, "r") as archive:
            self._infos = archive.infolist()
            for info in self._infos:
                name = info.filename
                target = os.path.realpath(os.path.join(self.workspace, *name.split("/")))
                if target != root and not target.startswith(root + os.sep):
                    raise ValueError(f"refusing unsafe archive entry: {name!r}")
                if name.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with archive.open(info, "r") as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                self._entries[name] = target
        return self.workspace

    # -- discovery ---------------------------------------------------------
    def get_properties_files(self) -> List[str]:
        """Every ``.properties`` file inside the workspace."""
        found: List[str] = []
        for root, _dirs, filenames in os.walk(self.workspace):
            for filename in filenames:
                if filename.endswith(".properties"):
                    found.append(os.path.join(root, filename))
        found.sort()
        return found

    def get_smali_files(self) -> List[str]:
        found: List[str] = []
        for root, _dirs, filenames in os.walk(self.workspace):
            for filename in filenames:
                if filename.endswith(".smali"):
                    found.append(os.path.join(root, filename))
        found.sort()
        return found

    def get_class_files(self) -> List[str]:
        """Every ``.class`` file inside the workspace (J2ME game code)."""
        found: List[str] = []
        for root, _dirs, filenames in os.walk(self.workspace):
            for filename in filenames:
                if filename.endswith(".class"):
                    found.append(os.path.join(root, filename))
        found.sort()
        return found

    # -- line level API (kept for callers that just want text) ------------
    def read_file(self, path: str) -> List[str]:
        with open(path, "r", encoding=self._charset_for(path), errors="surrogateescape") as handle:
            return handle.readlines()

    def write_file(self, path: str, lines: Sequence[str]) -> None:
        with open(path, "w", encoding=self._charset_for(path), errors="surrogateescape", newline="") as handle:
            handle.writelines(lines)

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as handle:
            return handle.read()

    def write_bytes(self, path: str, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)

    def load_properties(self, path: str) -> PropertiesFile:
        return PropertiesFile.loads(self.read_bytes(path))

    def save_properties(self, props: PropertiesFile, path: str) -> bool:
        """Write the file back. Returns True when something actually changed."""
        data = props.to_bytes()
        if data == props.raw:
            return False
        self.write_bytes(path, data)
        self._encodings[path] = props.charset
        return True

    def _charset_for(self, path: str) -> str:
        return self._encodings.get(path, "utf-8")

    # -- rebuild -----------------------------------------------------------
    def rebuild(self, output_name: str) -> str:
        """Write a patched archive, preserving the original entry layout."""
        if not output_name:
            raise ValueError("output_name is required")
        output_path = os.path.abspath(output_name)
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # Never feed our own output back into the archive.
        workspace_real = os.path.realpath(self.workspace)
        output_real = os.path.realpath(output_path)
        skip = set()
        if output_real.startswith(workspace_real + os.sep):
            skip.add(output_real)

        ordered: List[zipfile.ZipInfo] = list(self._infos)

        # A patched JAR must never ship the original's signatures: the .SF/.RSA
        # digests no longer match the modified entries, so the archive would
        # fail verification (or worse, look signed while it is not).
        # MANIFEST.MF goes too — its per-file hashes are stale after patching.
        # The output is intentionally UNSIGNED; re-sign with jarsigner if the
        # game requires a signature.
        def _is_signature(name: str) -> bool:
            upper = name.upper()
            return upper.startswith("META-INF/") and (
                upper == "META-INF/MANIFEST.MF"
                or upper.endswith((".SF", ".RSA", ".DSA", ".EC"))
            )

        stripped = [i for i in ordered if _is_signature(i.filename)]
        if stripped:
            print(f"[*] Stripping {len(stripped)} stale signature entries "
                  f"(output will be unsigned): {', '.join(i.filename for i in stripped[:4])}"
                  f"{'...' if len(stripped) > 4 else ''}")
        ordered = [i for i in ordered if not _is_signature(i.filename)]

        with zipfile.ZipFile(self.jar_path, "r") as source, zipfile.ZipFile(
            output_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as target:
            for info in ordered:
                name = info.filename
                path = self._entries.get(name)
                if path is None or os.path.realpath(path) in skip or not os.path.isfile(path):
                    data = source.read(name)
                else:
                    with open(path, "rb") as handle:
                        data = handle.read()

                new_info = zipfile.ZipInfo(name, date_time=info.date_time)
                new_info.compress_type = info.compress_type or zipfile.ZIP_DEFLATED
                new_info.external_attr = info.external_attr
                new_info.internal_attr = info.internal_attr
                new_info.create_system = info.create_system
                new_info.comment = info.comment
                target.writestr(new_info, data)

        print(f"[*] Done! Saved as {output_path}")
        return output_path

    def close(self) -> None:
        if os.path.isdir(self.workspace):
            shutil.rmtree(self.workspace, ignore_errors=True)

    def __enter__(self) -> "JarPatcher":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
