"""Tests for core/strings.py."""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import strings as S


class TestProperties(unittest.TestCase):
    def test_escapes_roundtrip(self):
        data = (
            "# a comment\n"
            "key1=hello\\nworld\\u0021\n"
            "key2 = value with\\ttab\\\\backslash\n"
            "key\\ with\\ spaces=value\n"
        ).encode("utf-8")
        entries = S.parse_properties(data)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].key, "key1")
        self.assertEqual(entries[0].value, "hello\nworld!")
        self.assertEqual(entries[0].comment, "a comment")
        self.assertEqual(entries[1].value, "value with\ttab\\backslash")
        self.assertEqual(entries[2].key, "key with spaces")
        out = S.dump_properties(entries)
        again = S.parse_properties(out)
        self.assertEqual([(e.key, e.value) for e in again],
                         [(e.key, e.value) for e in entries])

    def test_continuation(self):
        data = b"long=first part\\\n   second part\n"
        entries = S.parse_properties(data)
        self.assertEqual(entries[0].value, "first partsecond part")

    def test_format_specifiers_preserved(self):
        data = "msg=You have %d coins, %1$s!\\n".encode("utf-8")
        entries = S.parse_properties(data)
        self.assertIn("%d", entries[0].value)
        self.assertIn("%1$s", entries[0].value)

    def test_colon_separator(self):
        entries = S.parse_properties(b"a:b\n")
        self.assertEqual((entries[0].key, entries[0].value), ("a", "b"))

    def test_unknown_extension_raises(self):
        with self.assertRaises(S.StringParseError):
            S.detect_format("/tmp/something.weird")


class TestStringsXml(unittest.TestCase):
    XML = b"""<?xml version="1.0" encoding="utf-8"?>
<resources>
    <!-- greeting shown on launch -->
    <string name="welcome">Hello <b>%1$s</b>!</string>
    <string name="esc">a &lt; b &amp; c</string>
    <string name="noxliff" translatable="false">DoNotTouch</string>
    <string-array name="opts">
        <item>One</item>
        <item>Two</item>
    </string-array>
    <plurals name="coins">
        <item quantity="one">%d coin</item>
        <item quantity="other">%d coins</item>
    </plurals>
    <color name="brand">#ff0000</color>
</resources>
"""

    def test_parse(self):
        entries = S.parse_strings_xml(self.XML)
        keys = [e.key for e in entries]
        self.assertIn("welcome", keys)
        self.assertIn("esc", keys)
        self.assertIn("noxliff", keys)
        self.assertIn("opts[0]", keys)
        self.assertIn("opts[1]", keys)
        self.assertIn("coins{one}", keys)
        self.assertIn("coins{other}", keys)
        # colors are not translatable resources: must not appear
        self.assertNotIn("brand", keys)

        w = next(e for e in entries if e.key == "welcome")
        self.assertIn("<b>", w.value)
        self.assertIn("%1$s", w.value)
        self.assertEqual(w.comment, "greeting shown on launch")

        nt = next(e for e in entries if e.key == "noxliff")
        self.assertFalse(nt.translatable)

        esc = next(e for e in entries if e.key == "esc")
        self.assertEqual(esc.value, "a &lt; b &amp; c")

    def test_roundtrip(self):
        entries = S.parse_strings_xml(self.XML)
        out = S.dump_strings_xml(entries)
        again = S.parse_strings_xml(out)
        self.assertEqual({e.key: e.value for e in again},
                         {e.key: e.value for e in entries})
        # translatable=false survives
        nt = next(e for e in again if e.key == "noxliff")
        self.assertFalse(nt.translatable)

    def test_bad_xml_raises(self):
        with self.assertRaises(S.StringParseError):
            S.parse_strings_xml(b"<resources><string>oops")


class TestStringpack(unittest.TestCase):
    def test_roundtrip(self):
        entries = [
            S.StringEntry(key="a", value="Alpha %d", comment="c1"),
            S.StringEntry(key="b", value="Bravo <b>x</b>",
                          translatable=False),
        ]
        out = S.dump_stringpack(entries)
        again = S.parse_stringpack(out)
        self.assertEqual([(e.key, e.value, e.translatable) for e in again],
                         [(e.key, e.value, e.translatable) for e in entries])
        self.assertEqual(again[0].comment, "c1")

    def test_bad_json_raises(self):
        with self.assertRaises(S.StringParseError):
            S.parse_stringpack(b"{not json")
        with self.assertRaises(S.StringParseError):
            S.parse_stringpack(b'{"format": "nope", "entries": []}')


class TestDispatch(unittest.TestCase):
    def test_detect_format(self):
        self.assertEqual(S.detect_format("a.properties"), "properties")
        self.assertEqual(S.detect_format("res/values/strings.xml"), "strings-xml")
        self.assertEqual(S.detect_format("game.stringpack"), "stringpack")

    def test_load_save_files(self):
        tmp = tempfile.mkdtemp()
        try:
            p1 = os.path.join(tmp, "s.properties")
            with open(p1, "wb") as fh:
                fh.write(b"k=v %d\n")
            entries = S.load_strings(p1)
            self.assertEqual(entries[0].value, "v %d")

            p2 = os.path.join(tmp, "g.stringpack")
            S.save_strings(p2, entries)
            again = S.load_strings(p2)
            self.assertEqual(again[0].key, "k")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_empty_file_raises(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "e.properties")
            open(p, "wb").close()
            with self.assertRaises(S.StringParseError):
                S.load_strings(p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
