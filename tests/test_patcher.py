"""Tests for core/patcher.py."""

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import patcher


def _make_tree(base):
    """Fake extracted APK tree with strings.xml + a .properties file."""
    res = os.path.join(base, "res", "values")
    os.makedirs(res)
    with open(os.path.join(res, "strings.xml"), "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="utf-8"?><resources>'
                 b'<string name="hello">Hello</string>'
                 b'<string name="bye">Bye %d</string>'
                 b"</resources>")
    assets = os.path.join(base, "assets")
    os.makedirs(assets)
    with open(os.path.join(assets, "game.properties"), "wb") as fh:
        fh.write(b"title=My Game\n")
    with open(os.path.join(base, "resources.arsc"), "wb") as fh:
        fh.write(b"FAKE-ARSC")
    return base


class TestApply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_tree(os.path.join(self.tmp, "work"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_android_lang_variant(self):
        work = os.path.join(self.tmp, "work")
        stats = patcher.apply_translations(work, {"hello": "Halo", "bye": "Dadah %d"},
                                           lang="id")
        self.assertEqual(stats["applied"], 2)  # hello + bye
        self.assertEqual(stats["files"], 1)    # only strings.xml changed
        # android: new values-id file, original untouched
        newp = os.path.join(work, "res", "values-id", "strings.xml")
        self.assertTrue(os.path.isfile(newp))
        with open(newp, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("Halo", body)
        self.assertIn("Dadah %d", body)
        with open(os.path.join(work, "res", "values", "strings.xml"),
                  encoding="utf-8") as fh:
            self.assertIn("Hello", fh.read())

    def test_inplace_without_lang(self):
        work = os.path.join(self.tmp, "work")
        patcher.apply_translations(work, {"title": "Gim Saya"})
        with open(os.path.join(work, "assets", "game.properties"),
                  encoding="utf-8") as fh:
            self.assertIn("Gim Saya", fh.read())

    def test_untranslatable_not_applied(self):
        from core import strings as S
        work = os.path.join(self.tmp, "work")
        p = os.path.join(work, "res", "values", "strings.xml")
        entries = S.load_strings(p)
        for e in entries:
            e.translatable = False
        S.save_strings(p, entries)
        stats = patcher.apply_translations(work, {"hello": "Halo"}, lang="id")
        self.assertEqual(stats["applied"], 0)
        # nothing changed -> no values-id file created at all
        self.assertFalse(os.path.exists(
            os.path.join(work, "res", "values-id", "strings.xml")))

    def test_unmatched_keys_reported(self):
        work = os.path.join(self.tmp, "work")
        stats = patcher.apply_translations(work, {"nope": "x"}, lang="id")
        self.assertEqual(stats["unmatched"], ["nope"])
        self.assertEqual(stats["applied"], 0)

    def test_empty_dict_raises(self):
        with self.assertRaises(ValueError):
            patcher.apply_translations(os.path.join(self.tmp, "work"), {})


class TestRepack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_tree(os.path.join(self.tmp, "work"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manual_repack(self):
        out = os.path.join(self.tmp, "out.apk")
        got = patcher.repack_apk(os.path.join(self.tmp, "work"), out,
                                 use_apktool=False)
        self.assertTrue(zipfile.is_zipfile(got))
        with zipfile.ZipFile(got) as zf:
            names = zf.namelist()
            self.assertIn("res/values/strings.xml", names)
            info = zf.getinfo("resources.arsc")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED)


class TestTools(unittest.TestCase):
    def test_zipalign_missing_warns_not_crashes(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "a.apk")
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("x", "y")
            with mock.patch("shutil.which", return_value=None):
                self.assertEqual(patcher.zipalign_apk(p), p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_sign_missing_tool_raises_clear_error(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "a.apk")
            open(p, "wb").close()
            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    patcher.sign_apk(p)
            self.assertIn("apksigner", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_patch_apk_rejects_empty_translations(self):
        with self.assertRaises(ValueError):
            patcher.patch_apk("/nonexistent.apk", {}, "/tmp/o.apk")


if __name__ == "__main__":
    unittest.main()
