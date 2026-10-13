"""APK handling for KuroPatch v2 (stdlib only).

- :func:`extract_apk` unpacks an APK. It prefers ``apktool`` when available
  (full resource decoding) and falls back to plain ZIP extraction otherwise.
- :func:`parse_axml` parses binary Android XML (e.g. ``AndroidManifest.xml``
  inside an APK) without external tools.
- :func:`parse_manifest` / :func:`read_manifest` extract package name,
  versions, SDK levels and permissions from binary *or* plain XML manifests.
- :func:`list_string_resources` finds translatable string files under an
  extracted tree (``strings.xml``, ``*.properties``, ``*.stringpack``).

All errors are explicit (``AxmlError``, ``FileNotFoundError``,
``ValueError``) -- nothing fails silently.
"""

import os
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET


class AxmlError(ValueError):
    """Raised when binary Android XML cannot be parsed."""


# --- binary XML chunk types -------------------------------------------------
_CHUNK_XML = 0x0003
_CHUNK_STRING_POOL = 0x0001
_CHUNK_START_NS = 0x0100
_CHUNK_END_NS = 0x0101
_CHUNK_START_EL = 0x0102
_CHUNK_END_EL = 0x0103
_CHUNK_CDATA = 0x0104

# --- Res_value data types ----------------------------------------------------
_TYPE_STRING = 0x03
_TYPE_FLOAT = 0x04
_TYPE_INT_DEC = 0x10
_TYPE_INT_HEX = 0x11
_TYPE_INT_BOOL = 0x12

_UTF8_FLAG = 0x00000100
_NO_ENTRY = -1


class _Reader:
    """Bounds-checked little-endian cursor over bytes."""

    __slots__ = ("d", "p")

    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def _need(self, n):
        if self.p + n > len(self.d):
            raise AxmlError(f"truncated binary XML at offset {self.p}")

    def u8(self):
        self._need(1)
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        self._need(2)
        v = struct.unpack_from("<H", self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        self._need(4)
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        self._need(4)
        v = struct.unpack_from("<i", self.d, self.p)[0]
        self.p += 4
        return v

    def skip(self, n):
        self._need(n)
        self.p += n


def _read_varlen(data, pos, wide):
    """Read a string-pool length prefix. Returns (length, new_pos)."""
    if wide:
        if pos + 2 > len(data):
            raise AxmlError("truncated UTF-16 length prefix")
        w = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if w & 0x8000:
            if pos + 2 > len(data):
                raise AxmlError("truncated UTF-16 long length prefix")
            w2 = struct.unpack_from("<H", data, pos)[0]
            return ((w & 0x7FFF) << 16) | w2, pos + 2
        return w, pos
    if pos + 1 > len(data):
        raise AxmlError("truncated UTF-8 length prefix")
    b = data[pos]
    pos += 1
    if b & 0x80:
        if pos + 1 > len(data):
            raise AxmlError("truncated UTF-8 long length prefix")
        return ((b & 0x7F) << 8) | data[pos], pos + 1
    return b, pos


def _pool_string(data, pos, utf8):
    if utf8:
        _, pos = _read_varlen(data, pos, False)      # char count (unused)
        blen, pos = _read_varlen(data, pos, False)  # byte count
        if pos + blen > len(data):
            raise AxmlError("string extends past end of data")
        return data[pos:pos + blen].decode("utf-8", errors="replace")
    clen, pos = _read_varlen(data, pos, True)
    if pos + clen * 2 > len(data):
        raise AxmlError("string extends past end of data")
    return data[pos:pos + clen * 2].decode("utf-16-le", errors="replace")


def _parse_string_pool(data, offset):
    """Returns (list_of_strings, chunk_size)."""
    r = _Reader(data, offset)
    typ, _hdr, chunk_size = r.u16(), r.u16(), r.u32()
    if typ != _CHUNK_STRING_POOL:
        raise AxmlError(f"expected string pool at {offset:#x}, got chunk {typ:#x}")
    string_count = r.u32()
    style_count = r.u32()
    flags = r.u32()
    strings_start = r.u32()
    r.skip(4)  # styles_start
    offsets = [r.u32() for _ in range(string_count)]
    r.skip(style_count * 4)
    utf8 = bool(flags & _UTF8_FLAG)
    base = offset + strings_start
    out = []
    for off in offsets:
        if base + off >= len(data):
            raise AxmlError("string pool offset out of range")
        out.append(_pool_string(data, base + off, utf8))
    return out, chunk_size


def _pool_get(strings, idx):
    if idx is None or idx < 0 or idx >= len(strings):
        return ""
    return strings[idx]


@dataclass
class XmlEvent:
    """One binary-XML event."""

    kind: str          # "start" or "end"
    name: str
    attrs: dict        # attribute local-name -> resolved string value
    depth: int = 0


def _resolve_attr(strings, raw_idx, dtype, data):
    if dtype == _TYPE_STRING:
        return _pool_get(strings, data)
    if dtype == _TYPE_INT_BOOL:
        return "true" if data != 0 else "false"
    if dtype == _TYPE_INT_DEC:
        v = data if data < 0x80000000 else data - 0x100000000
        return str(v)
    if dtype == _TYPE_INT_HEX:
        return hex(data)
    if dtype == _TYPE_FLOAT:
        return str(struct.unpack("<f", struct.pack("<I", data))[0])
    if 0 <= raw_idx < len(strings):
        return strings[raw_idx]
    return str(data)


def parse_axml(data):
    """Parse binary Android XML -> list[XmlEvent].

    Raises AxmlError on bad magic, truncation or corrupt chunks.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) < 8:
        raise AxmlError("data too short for binary XML")
    if struct.unpack_from("<H", data, 0)[0] != _CHUNK_XML:
        raise AxmlError("not a binary Android XML file (bad magic)")
    strings, pool_size = _parse_string_pool(data, 8)

    events = []
    pos, depth, n = 8 + pool_size, 0, len(data)
    while pos + 8 <= n:
        ctyp, _chdr, csize = struct.unpack_from("<HHI", data, pos)
        if csize < 8 or pos + csize > n:
            raise AxmlError(f"corrupt chunk at offset {pos:#x}")
        if ctyp == _CHUNK_START_EL:
            r = _Reader(data, pos + 8)
            r.skip(8)  # lineNumber, comment
            r.skip(4)  # ns
            name_idx = r.i32()
            r.skip(2)  # attributeStart
            attr_size = r.u16()
            attr_count = r.u16()
            r.skip(6)  # idIndex, classIndex, styleIndex
            name = _pool_get(strings, name_idx)
            attrs = {}
            for _ in range(attr_count):
                _a_ns = r.i32()
                a_name = r.i32()
                a_raw = r.i32()
                _v_size = r.u16()
                r.skip(1)
                v_type = r.u8()
                v_data = r.u32()
                key = _pool_get(strings, a_name) or f"attr@{a_name}"
                attrs[key] = _resolve_attr(strings, a_raw, v_type, v_data)
                if attr_size != 20:
                    r.skip(attr_size - 20)
            events.append(XmlEvent("start", name, attrs, depth))
            depth += 1
        elif ctyp == _CHUNK_END_EL:
            r = _Reader(data, pos + 8)
            r.skip(12)  # lineNumber, comment, ns
            name_idx = r.i32()
            depth = max(0, depth - 1)
            events.append(XmlEvent("end", _pool_get(strings, name_idx), {}, depth))
        # namespaces and CDATA carry no data we need: skip by chunk size
        pos += csize
    return events


@dataclass
class ManifestInfo:
    """Key facts extracted from AndroidManifest.xml."""

    package: str = ""
    version_code: str = ""
    version_name: str = ""
    min_sdk: str = ""
    target_sdk: str = ""
    permissions: list = field(default_factory=list)


def _strip_ns(name):
    return name.split("}", 1)[-1] if "}" in name else name


def parse_manifest(data):
    """Parse manifest bytes (binary AXML or plain XML) -> ManifestInfo."""
    info = ManifestInfo()
    if len(data) >= 2 and struct.unpack_from("<H", data, 0)[0] == _CHUNK_XML:
        for ev in parse_axml(data):
            if ev.kind != "start":
                continue
            if ev.name == "manifest":
                info.package = ev.attrs.get("package", "")
                info.version_code = ev.attrs.get("versionCode", "")
                info.version_name = ev.attrs.get("versionName", "")
            elif ev.name == "uses-sdk":
                info.min_sdk = ev.attrs.get("minSdkVersion", "")
                info.target_sdk = ev.attrs.get("targetSdkVersion", "")
            elif ev.name == "uses-permission":
                name = ev.attrs.get("name", "")
                if name:
                    info.permissions.append(name)
        return info
    # plain XML (e.g. after apktool decode)
    try:
        root = ET.fromstring(data.decode("utf-8-sig"))
    except ET.ParseError as exc:
        raise AxmlError(f"manifest is neither binary nor valid XML: {exc}")
    if _strip_ns(root.tag) != "manifest":
        raise AxmlError(f"expected <manifest> root, got <{root.tag}>")
    attrs = {_strip_ns(k): v for k, v in root.attrib.items()}
    info.package = attrs.get("package", "")
    info.version_code = attrs.get("versionCode", "")
    info.version_name = attrs.get("versionName", "")
    for child in root:
        tag = _strip_ns(child.tag)
        cattrs = {_strip_ns(k): v for k, v in child.attrib.items()}
        if tag == "uses-sdk":
            info.min_sdk = cattrs.get("minSdkVersion", "")
            info.target_sdk = cattrs.get("targetSdkVersion", "")
        elif tag == "uses-permission" and cattrs.get("name"):
            info.permissions.append(cattrs["name"])
    return info


def read_manifest(work_dir):
    """Read AndroidManifest.xml from an extracted tree -> ManifestInfo."""
    path = os.path.join(work_dir, "AndroidManifest.xml")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"AndroidManifest.xml not found in {work_dir}")
    with open(path, "rb") as fh:
        return parse_manifest(fh.read())


# --- extraction ------------------------------------------------------------


def apktool_available():
    """True when the ``apktool`` binary is on PATH."""
    return shutil.which("apktool") is not None


def _run(cmd):
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise RuntimeError(f"tool not found: {cmd[0]}")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()[:500]}")
    return proc


def extract_apk(apk_path, out_dir, use_apktool=True):
    """Unpack an APK into out_dir. Returns the absolute out_dir.

    Prefers apktool (decodes resources); falls back to raw ZIP extraction.
    Raises FileNotFoundError / ValueError on bad input, RuntimeError on
    tool failure.
    """
    apk_path = os.path.abspath(apk_path)
    if not os.path.isfile(apk_path):
        raise FileNotFoundError(f"APK not found: {apk_path}")
    if not zipfile.is_zipfile(apk_path):
        raise ValueError(f"not a valid APK/ZIP archive: {apk_path}")
    os.makedirs(out_dir, exist_ok=True)
    out_dir = os.path.abspath(out_dir)
    if use_apktool and apktool_available():
        _run(["apktool", "d", "-f", "-o", out_dir, apk_path])
    else:
        with zipfile.ZipFile(apk_path) as zf:
            zf.extractall(out_dir)
    return out_dir


def list_string_resources(work_dir):
    """Find string resource files under an extracted tree.

    Returns sorted absolute paths: ``strings.xml`` files, ``*.properties``
    and ``*.stringpack`` files. Raises FileNotFoundError for a bad dir.
    """
    if not os.path.isdir(work_dir):
        raise FileNotFoundError(f"not a directory: {work_dir}")
    hits = []
    for root, _dirs, files in os.walk(work_dir):
        for fname in files:
            low = fname.lower()
            if fname == "strings.xml" or low.endswith(".properties") \
                    or low.endswith(".stringpack"):
                hits.append(os.path.join(root, fname))
    return sorted(hits)
