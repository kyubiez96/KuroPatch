"""Gameloft IGP string-pack support (``RES_STRINGS*`` files).

J2ME Gameloft games (Miami Nights 2 and friends) keep almost all their UI
text and dialogue in LZMA-compressed string-pack resources named
``RES_STRINGS``, ``RES_STRINGS1``, ``RES_STRINGS2``, ... — not in the Java
constant pool. A resource holds one or more packs back to back::

    pack   := es:u8, count:u-es-BE, table[count]:u-es-BE, strings
    strings:= null-terminated UTF-8, offsets relative to the string region

String content uses inline codes that must survive translation verbatim:

* ``\\m..\\m`` / ``\\f..\\f`` — male / female gender variants
* ``\\p1`` ``\\p2`` ``\\p7`` ``\\p8`` — highlight / parameter markers
* ``\\x`` — player-name placeholder, ``\\n`` — newline, ``\\d`` — misc code
* ``\\v1..\\v1`` ``\\v600..\\v600`` — variant blocks
* ``]`` — inline line break

These are protected by :func:`translator.protect_segments` before a string
reaches a translation provider.
"""

from __future__ import annotations

import lzma
import re
from dataclasses import dataclass, field
from typing import List


_PACK_NAME_RE = re.compile(r"^RES_STRINGS\d*$")


def is_string_pack_entry(name: str) -> bool:
    """True for Gameloft ``RES_STRINGS*`` resource entries (any directory)."""
    base = name.rsplit("/", 1)[-1]
    return _PACK_NAME_RE.match(base) is not None


@dataclass
class StringPack:
    """One pack: ``es`` is the offset width in bytes, ``strings`` the texts."""

    es: int
    strings: List[str] = field(default_factory=list)


def parse_resource(raw: bytes) -> List[StringPack]:
    """LZMA-decompress ``raw`` and parse every pack it contains.

    Raises :class:`ValueError` when the data is not a string-pack resource.
    """
    try:
        data = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    except (lzma.LZMAError, EOFError) as exc:
        raise ValueError(f"not LZMA-compressed: {exc}") from exc

    packs: List[StringPack] = []
    pos = 0
    size = len(data)
    while pos < size:
        if pos + 2 > size:
            raise ValueError(f"truncated pack header at {pos}")
        es = data[pos]
        if es == 0 or es > 4:
            raise ValueError(f"bad offset width {es} at {pos}")
        if pos + 1 + es > size:
            raise ValueError(f"truncated pack count at {pos}")
        count = int.from_bytes(data[pos + 1 : pos + 1 + es], "big")
        if count == 0 or count > 100_000:
            raise ValueError(f"bad string count {count} at {pos}")
        table = pos + 1 + es
        sbase = table + es * count
        if sbase > size:
            raise ValueError(f"pack table overruns at {pos}")
        strings: List[str] = []
        for i in range(count):
            off = int.from_bytes(data[table + i * es : table + (i + 1) * es], "big")
            start = sbase + off
            if start >= size:
                raise ValueError(f"string {i} offset out of range at pack {pos}")
            end = data.index(b"\x00", start)
            strings.append(data[start:end].decode("utf-8", errors="replace"))
        packs.append(StringPack(es=es, strings=strings))
        # Advance past the string data.
        maxend = sbase
        for i in range(count):
            off = int.from_bytes(data[table + i * es : table + (i + 1) * es], "big")
            maxend = max(maxend, data.index(b"\x00", sbase + off) + 1)
        pos = maxend

    if not packs:
        raise ValueError("no packs found")
    return packs


def build_resource(packs: List[StringPack]) -> bytes:
    """Rebuild packs and LZMA-compress them back to resource bytes."""
    out = bytearray()
    for pack in packs:
        es = pack.es
        count = len(pack.strings)
        limit = (1 << (8 * es)) - 1
        table = bytearray()
        strdata = bytearray()
        for text in pack.strings:
            encoded = text.encode("utf-8") + b"\x00"
            if len(strdata) + len(encoded) > limit:
                raise ValueError(
                    f"string data ({len(strdata) + len(encoded)} bytes) exceeds "
                    f"{es}-byte offset width"
                )
            table += len(strdata).to_bytes(es, "big")
            strdata += encoded
        out += bytes([es])
        out += count.to_bytes(es, "big")
        out += table
        out += strdata
    return lzma.compress(
        bytes(out), format=lzma.FORMAT_ALONE, preset=6
    )


def flatten(packs: List[StringPack]) -> List[str]:
    """All strings in pack order (for collection)."""
    return [text for pack in packs for text in pack.strings]
