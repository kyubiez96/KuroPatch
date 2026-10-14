"""Gameloft ``TRAD_*.loc`` string-table parser / builder / validator.

Stdlib only. Format (verified byte-exact on all 6 shipped Castlevania
``.loc`` files, plus the game's own loader bytecode ``DOSMidlet.aE()``)::

    [u32 BE skip = 1206][u32 BE count = 400]
    [4 bytes ``10 08 00 00`` — constant, purpose unknown, preserved verbatim]
    [offset table: 400 x u24 BE]
        entries 0..398 = start(record[i+1]) - 1214
        entry 399 = 0 (sentinel)
    [u16 BE total string-data size = filesize - 1214]
    [400 records: [u16 BE byte-length][modified-UTF-8 bytes], no terminator]

The game reads the table sequentially (``readInt`` skip, ``readInt`` count,
``skipBytes``, then 400 x ``readUTF``) and never reads the offset table,
never checksums, never compares ``.loc`` strings with ``equals()`` — the
strings are display-only, addressed by fixed index. That makes the format
safe to rebuild, but every correctness burden falls on the patcher:
exact count, exact skip, pinned ``g[0]``, exact ``$N`` style codes,
valid modified UTF-8, untouched language names.

Strategy (approved): replace the English ``.loc`` content in place — the
player picks "English" and gets Indonesian text. ``TRAD.lang``,
``DOSMidlet.b`` language names and the ``l.class`` filename array are
never touched (a 7th language slot would need bytecode surgery).
"""

import re
import struct

SKIP = 1206
COUNT = 400
BASE = 1214  # 8-byte header + 1206 skipped bytes
HEADER_TAIL = b"\x10\x08\x00\x00"
MAX_ENCODED = 65535  # u16 length prefix / DataInputStream.readUTF cap
MAX_TOKEN_CHARS = 256  # renderer word-wraps into a char[256] buffer; a
# single whitespace-free token longer than that cannot be wrapped.
# (A per-\n-line cap is WRONG: shipped PT story line is 399 chars.)
MAX_RECORD_SANITY = 4096  # fail-closed sanity cap; longest shipped is 805
MAX_RECORDS = 400

# Indices that must stay byte-identical (cross-language identity test:
# Konami's own localizers left these untranslated in all 6 shipped
# localizations, so translating them would be wrong by definition).
# rec0 "0" dummy, rec1 language self-name (lookup key, like TRAD.lang
# names — translating it bricks language selection), rec64 title,
# rec65 copyright, rec70/71/79/81 empty, rec84 "GMG", rec96 "SOMA",
# rec114 "INT", rec159-166 character names, rec171 "Alastor",
# rec175 "Gaibon", rec199 "Bhuj", rec203 "Claimh Solais",
# rec204 "Chakram", rec205 "Tomahawk", rec209 "Gungner",
# rec274/277/296/315/322/328/338/346/347/351 name/punctuation
# dialog fragments, plus 6 edge cases identical in 5 of 6
# (one localization adapted): rec50/111/123/252/350/364.
DO_NOT_TRANSLATE = frozenset(
    [0, 1, 64, 65, 70, 71, 79, 81, 84, 96, 114]
    + list(range(159, 167))
    + [171, 175, 199, 203, 204, 205, 209]
    + [274, 277, 296, 315, 322, 328, 338, 346, 347, 351]
    + [50, 111, 123, 252, 350, 364]
)

_DOLLAR_CODE_RE = re.compile(r"\$[0-9]")


def decode_modified_utf8(data: bytes) -> str:
    """Decode Java modified UTF-8 (what DataInputStream.readUTF reads).

    Differences from standard UTF-8: NUL is ``C0 80``, astral chars are
    a 6-byte surrogate pair, and 4-byte sequences / lone surrogates /
    literal NUL bytes are invalid (readUTF throws UTFDataFormatException).
    """
    out = []
    i = 0
    n = len(data)
    while i < n:
        b0 = data[i]
        if b0 == 0:
            raise ValueError("literal NUL byte is invalid modified UTF-8")
        if b0 < 0x80:
            out.append(chr(b0))
            i += 1
        elif 0xC0 <= b0 < 0xE0:
            if i + 1 >= n:
                raise ValueError("truncated 2-byte modified UTF-8 sequence")
            b1 = data[i + 1]
            if b0 == 0xC0 and b1 == 0x80:
                out.append("\x00")
            elif b0 in (0xC0, 0xC1):
                raise ValueError("overlong modified UTF-8 sequence")
            else:
                if b1 & 0xC0 != 0x80:
                    raise ValueError("bad continuation byte in modified UTF-8")
                out.append(chr(((b0 & 0x1F) << 6) | (b1 & 0x3F)))
            i += 2
        elif 0xE0 <= b0 < 0xF0:
            if i + 2 >= n:
                raise ValueError("truncated 3-byte modified UTF-8 sequence")
            b1, b2 = data[i + 1], data[i + 2]
            if b1 & 0xC0 != 0x80 or b2 & 0xC0 != 0x80:
                raise ValueError("bad continuation byte in modified UTF-8")
            cp = ((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F)
            if 0xD800 <= cp <= 0xDBFF:
                # High surrogate: must be followed by a low surrogate.
                if i + 5 >= n:
                    raise ValueError("lone high surrogate in modified UTF-8")
                c0, c1, c2 = data[i + 3], data[i + 4], data[i + 5]
                if not (c0 == 0xED and 0xB0 <= c1 <= 0xBF and c2 & 0xC0 == 0x80):
                    raise ValueError("lone high surrogate in modified UTF-8")
                lo = ((c1 & 0x0F) << 6) | (c2 & 0x3F)
                out.append(chr(0x10000 + ((cp - 0xD800) << 10) + lo))
                i += 6
            elif 0xDC00 <= cp <= 0xDFFF:
                raise ValueError("lone low surrogate in modified UTF-8")
            else:
                out.append(chr(cp))
                i += 3
        else:
            raise ValueError(
                "4-byte sequence is invalid modified UTF-8 "
                "(astral chars must be surrogate pairs)"
            )
    return "".join(out)


def encode_modified_utf8(text: str) -> bytes:
    """Encode to Java modified UTF-8 (what DataOutputStream.writeUTF writes)."""
    out = bytearray()
    for ch in text:
        cp = ord(ch)
        if cp == 0:
            out += b"\xC0\x80"
        elif cp < 0x80:
            out.append(cp)
        elif cp < 0x800:
            out.append(0xC0 | (cp >> 6))
            out.append(0x80 | (cp & 0x3F))
        elif cp < 0x10000:
            if 0xD800 <= cp <= 0xDFFF:
                raise ValueError("lone surrogate cannot be encoded as modified UTF-8")
            out.append(0xE0 | (cp >> 12))
            out.append(0x80 | ((cp >> 6) & 0x3F))
            out.append(0x80 | (cp & 0x3F))
        else:
            cp -= 0x10000
            hi = 0xD800 + (cp >> 10)
            lo = 0xDC00 + (cp & 0x3FF)
            for s in (hi, lo):
                out.append(0xE0 | (s >> 12))
                out.append(0x80 | ((s >> 6) & 0x3F))
                out.append(0x80 | (s & 0x3F))
    return bytes(out)


def parse(data: bytes) -> list:
    """Parse a ``TRAD_*.loc`` file into 400 strings.

    Raises ValueError on any structural problem: short file, wrong
    skip/count, wrong header constant, bad modified UTF-8, offset-table
    mismatch, or trailing bytes after record 400.
    """
    if len(data) < BASE:
        raise ValueError(
            "loc file too short: %d bytes, need at least %d" % (len(data), BASE)
        )
    skip, count = struct.unpack(">II", data[:8])
    if skip != SKIP:
        raise ValueError("loc skip must be %d, got %d" % (SKIP, skip))
    if count != COUNT:
        raise ValueError("loc count must be %d, got %d" % (COUNT, count))
    if data[8:12] != HEADER_TAIL:
        raise ValueError("loc header bytes 8-12 must be %s" % HEADER_TAIL.hex())

    table = [
        int.from_bytes(data[12 + i * 3: 12 + i * 3 + 3], "big")
        for i in range(COUNT)
    ]
    total_size = struct.unpack(">H", data[BASE - 2: BASE])[0]
    if total_size != len(data) - BASE:
        raise ValueError(
            "loc total-size field %d != filesize - %d (%d)"
            % (total_size, BASE, len(data) - BASE)
        )

    strings = []
    starts = []
    p = BASE
    for _ in range(COUNT):
        if p + 2 > len(data):
            raise ValueError("loc truncated inside record length prefix")
        (length,) = struct.unpack(">H", data[p: p + 2])
        starts.append(p)
        p += 2
        if p + length > len(data):
            raise ValueError("loc truncated inside record bytes")
        strings.append(decode_modified_utf8(data[p: p + length]))
        p += length
    if p != len(data):
        raise ValueError(
            "loc has %d trailing bytes after record %d" % (len(data) - p, COUNT)
        )
    for i in range(COUNT - 1):
        if table[i] != starts[i + 1] - BASE:
            raise ValueError(
                "loc offset table entry %d is %d, expected %d"
                % (i, table[i], starts[i + 1] - BASE)
            )
    if table[COUNT - 1] != 0:
        raise ValueError(
            "loc offset table sentinel entry %d must be 0, got %d"
            % (COUNT - 1, table[COUNT - 1])
        )
    return strings


def build(strings: list) -> bytes:
    """Rebuild a ``TRAD_*.loc`` file from exactly 400 strings.

    Header preserved verbatim; offset table and total-size field are
    regenerated. Raises ValueError on wrong count or oversize records.
    """
    if len(strings) != COUNT:
        raise ValueError("loc needs exactly %d strings, got %d" % (COUNT, len(strings)))
    encoded = []
    for i, s in enumerate(strings):
        blob = encode_modified_utf8(s)
        if len(blob) > MAX_ENCODED:
            raise ValueError(
                "loc record %d encodes to %d bytes, over the %d cap"
                % (i, len(blob), MAX_ENCODED)
            )
        encoded.append(blob)
    total = sum(2 + len(b) for b in encoded)
    if total > MAX_ENCODED:
        raise ValueError(
            "loc string section is %d bytes, over the %d cap" % (total, MAX_ENCODED)
        )

    out = bytearray()
    out += struct.pack(">II", SKIP, COUNT)
    out += HEADER_TAIL
    starts = []
    pos = BASE
    for blob in encoded:
        starts.append(pos)
        pos += 2 + len(blob)
    for i in range(COUNT - 1):
        off = starts[i + 1] - BASE
        if off > 0xFFFFFF:
            raise ValueError("loc offset %d overflows u24" % off)
        out += off.to_bytes(3, "big")
    out += b"\x00\x00\x00"  # sentinel entry 399
    out += struct.pack(">H", total)
    for blob in encoded:
        out += struct.pack(">H", len(blob))
        out += blob
    return bytes(out)


def _dollar_codes(text: str) -> list:
    """``$N`` style codes in order (the renderer consumes them, never draws them)."""
    return _DOLLAR_CODE_RE.findall(text)


def _dollar_positions(text: str) -> list:
    """``(line_no, col_in_line)`` of each ``$N`` code.

    Codes must sit at line starts on the same lines as the source —
    the renderer consumes them while laying out lines, so a code moved
    off line-start (same sequence, different position) still breaks it.
    """
    out = []
    line = 0
    col = 0
    i = 0
    while i < len(text):
        if text[i] == "\n":
            line += 1
            col = 0
            i += 1
        elif text[i] == "$" and i + 1 < len(text) and text[i + 1].isdigit():
            out.append((line, col))
            i += 2
            col += 2
        else:
            i += 1
            col += 1
    return out


def validate(translated: list, original: list) -> None:
    """Validate translated ``.loc`` strings against the original table.

    Raises ValueError listing every violation. Passes silently when the
    translation is safe to ship (as far as static checks can tell — the
    emulator boot test is still required before release).
    """
    problems = []
    if len(original) != COUNT:
        problems.append("original has %d records, expected %d" % (len(original), COUNT))
        raise ValueError("; ".join(problems))
    if len(translated) != COUNT:
        problems.append("translated has %d records, expected %d" % (len(translated), COUNT))
        raise ValueError("; ".join(problems))

    for i, (new, old) in enumerate(zip(translated, original)):
        if new == old:
            continue  # untouched record: every check below passes trivially
        if i in DO_NOT_TRANSLATE:
            problems.append(
                "record %d is do-not-translate but changed (%r -> %r)"
                % (i, old, new)
            )
            continue  # further checks on this record are meaningless
        if old == "" and new != "":
            problems.append("record %d was empty but got text %r" % (i, new))
        if _dollar_codes(new) != _dollar_codes(old):
            problems.append(
                "record %d $N codes changed (%r -> %r)"
                % (i, _dollar_codes(old), _dollar_codes(new))
            )
        if new.count("\n") != old.count("\n"):
            problems.append(
                "record %d newline count changed (%d -> %d)"
                % (i, old.count("\n"), new.count("\n"))
            )
        if "\x00" in new:
            problems.append("record %d contains NUL" % i)
        if _dollar_positions(new) != _dollar_positions(old):
            problems.append("record %d $N code positions changed" % i)
        for tok in new.split():
            if len(tok) > MAX_TOKEN_CHARS:
                problems.append(
                    "record %d has a %d-char unwrappable token, over the %d cap"
                    % (i, len(tok), MAX_TOKEN_CHARS)
                )
                break
        new_non_ascii = sorted(
            {c for c in new if ord(c) >= 128}
            - {c for c in old if ord(c) >= 128}
        )
        if new_non_ascii:
            problems.append(
                "record %d introduces non-ASCII chars %r "
                "(translate with ASCII only; preserving original "
                "non-ASCII such as person names is fine)" % (i, new_non_ascii)
            )
        try:
            encoded_len = len(encode_modified_utf8(new))
        except ValueError as exc:
            problems.append("record %d cannot encode as modified UTF-8: %s" % (i, exc))
            continue
        if encoded_len > MAX_ENCODED:
            problems.append(
                "record %d encodes to %d bytes, over the %d cap"
                % (i, encoded_len, MAX_ENCODED)
            )
        elif encoded_len > MAX_RECORD_SANITY:
            problems.append(
                "record %d encodes to %d bytes, over the %d sanity cap"
                % (i, encoded_len, MAX_RECORD_SANITY)
            )
    if problems:
        raise ValueError("; ".join(problems))
