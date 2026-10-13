"""Tests for core/apk.py, incl. the binary AndroidManifest.xml parser."""

import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import apk


# ---------------------------------------------------------------------------
# minimal binary-AXML builder (mirrors the format apk.parse_axml reads)
# ---------------------------------------------------------------------------

def _u8len(n):
    if n < 0x80:
        return struct.pack("B", n)
    return struct.pack(">H", n | 0x8000)


def build_string_pool(strings):
    enc = []
    for s in strings:
        b = s.encode("utf-8")
        enc.append(_u8len(len(b)) + _u8len(len(b)) + b + b"\x00")
    offsets, pos = [], 0
    for e in enc:
        offsets.append(pos)
        pos += len(e)
    body = b"".join(enc)
    body += b"\x00" * ((-len(body)) % 4)
    strings_start = 28 + 4 * len(strings)
    chunk_size = strings_start + len(body)
    out = struct.pack("<HHI", 0x0001, 28, chunk_size)
    out += struct.pack("<IIIII", len(strings), 0, 0x100, strings_start, 0)
    out += b"".join(struct.pack("<I", o) for o in offsets)
    return out + body


def build_start_element(name_idx, attrs):
    """attrs: list of (name_idx, value_string_idx)."""
    n = len(attrs)
    out = struct.pack("<HHI", 0x0102, 16, 36 + 20 * n)
    out += struct.pack("<I", 0)      # lineNumber
    out += struct.pack("<i", -1)     # comment
    out += struct.pack("<i", -1)     # ns
    out += struct.pack("<i", name_idx)
    out += struct.pack("<HHH", 20, 20, n)
    out += struct.pack("<HHH", 0xFFFF, 0xFFFF, 0xFFFF)
    for aname, aval in attrs:
        out += struct.pack("<i", -1)     # ns
        out += struct.pack("<i", aname)
        out += struct.pack("<i", -1)     # rawValue
        out += struct.pack("<HBBI", 8, 0, 0x03, aval)  # string value
    return out


def build_end_element(name_idx):
    out = struct.pack("<HHI", 0x0103, 16, 24)
    out += struct.pack("<I", 0)
    out += struct.pack("<i", -1)
    out += struct.pack("<i", -1)
    out += struct.pack("<i", name_idx)
    return out


def build_test_manifest():
    pool = ["manifest", "package", "com.example.game",
            "versionCode", "42", "versionName", "1.0",
            "uses-sdk", "minSdkVersion", "21", "targetSdkVersion", "34",
            "uses-permission", "name", "android.permission.INTERNET"]
    idx = {s: i for i, s in enumerate(pool)}
    chunks = [build_string_pool(pool)]
    chunks.append(build_start_element(idx["manifest"], [
        (idx["package"], idx["com.example.game"]),
        (idx["versionCode"], idx["42"]),
        (idx["versionName"], idx["1.0"]),
    ]))
    chunks.append(build_start_element(idx["uses-sdk"], [
        (idx["minSdkVersion"], idx["21"]),
        (idx["targetSdkVersion"], idx["34"]),
    ]))
    chunks.append(build_end_element(idx["uses-sdk"]))
    chunks.append(build_start_element(idx["uses-permission"], [
        (idx["name"], idx["android.permission.INTERNET"]),
    ]))
    chunks.append(build_end_element(idx["uses-permission"]))
    chunks.append(build_end_element(idx["manifest"]))
    body = b"".join(chunks)
    return struct.pack("<HHI", 0x0003, 8, 8 + len(body)) + body


# ---------------------------------------------------------------------------

class TestAxml(unittest.TestCase):
    def test_parse_manifest_binary(self):
        info = apk.parse_manifest(build_test_manifest())
        self.assertEqual(info.package, "com.example.game")
        self.assertEqual(info.version_code, "42")
        self.assertEqual(info.version_name, "1.0")
        self.assertEqual(info.min_sdk, "21")
        self.assertEqual(info.target_sdk, "34")
        self.assertEqual(info.permissions, ["android.permission.INTERNET"])

    def test_events(self):
        events = apk.parse_axml(build_test_manifest())
        starts = [e for e in events if e.kind == "start"]
        self.assertEqual([e.name for e in starts],
                         ["manifest", "uses-sdk", "uses-permission"])
        self.assertEqual(starts[0].attrs["package"], "com.example.game")

    def test_bad_magic_raises(self):
        with self.assertRaises(apk.AxmlError):
            apk.parse_axml(b"PK\x03\x04 not xml")

    def test_truncated_raises(self):
        with self.assertRaises(apk.AxmlError):
            apk.parse_axml(build_test_manifest()[:20])

    def test_plain_xml_manifest(self):
        data = (b'<?xml version="1.0"?>'
                b'<manifest xmlns:android="http://schemas.android.com/apk/res/android"'
                b' package="com.plain.app" android:versionCode="7"'
                b' android:versionName="2.1">'
                b'<uses-sdk android:minSdkVersion="24"/>'
                b'<uses-permission android:name="android.permission.CAMERA"/>'
                b'</manifest>')
        info = apk.parse_manifest(data)
        self.assertEqual(info.package, "com.plain.app")
        self.assertEqual(info.version_code, "7")
        self.assertEqual(info.min_sdk, "24")
        self.assertEqual(info.permissions, ["android.permission.CAMERA"])


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.apk_path = os.path.join(self.tmp, "game.apk")
        with zipfile.ZipFile(self.apk_path, "w") as zf:
            zf.writestr("AndroidManifest.xml", build_test_manifest())
            zf.writestr("res/values/strings.xml",
                        '<?xml version="1.0"?><resources>'
                        '<string name="hi">Hello</string></resources>')
            zf.writestr("assets/lang.properties", "k=v\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_extract_and_list(self):
        out = os.path.join(self.tmp, "out")
        got = apk.extract_apk(self.apk_path, out, use_apktool=False)
        self.assertTrue(os.path.isfile(os.path.join(got, "AndroidManifest.xml")))
        info = apk.read_manifest(got)
        self.assertEqual(info.package, "com.example.game")
        res = apk.list_string_resources(got)
        self.assertEqual(len(res), 2)
        self.assertTrue(any(p.endswith("strings.xml") for p in res))
        self.assertTrue(any(p.endswith(".properties") for p in res))

    def test_missing_apk_raises(self):
        with self.assertRaises(FileNotFoundError):
            apk.extract_apk(os.path.join(self.tmp, "nope.apk"), self.tmp)

    def test_not_a_zip_raises(self):
        bad = os.path.join(self.tmp, "bad.apk")
        with open(bad, "wb") as fh:
            fh.write(b"definitely not a zip")
        with self.assertRaises(ValueError):
            apk.extract_apk(bad, self.tmp, use_apktool=False)

    def test_missing_manifest_raises(self):
        with self.assertRaises(FileNotFoundError):
            apk.read_manifest(self.tmp)


if __name__ == "__main__":
    unittest.main()
