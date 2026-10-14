"""Tests for the .jar pipeline (core/jar.py + core/patcher.py).

unittest TestCase style (QA runs unittest discover).

Builds a fixture .jar with .properties + smali + .class strings,
then verifies collect -> patch round-trips, zip-slip refusal and
stale-signature stripping. No network, no API keys.
"""

import os
import struct
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import jar as jar_mod
from core.patcher import JarPatcher


def _make_class_with_string(text):
    """Minimal .class file with one CONSTANT_String entry."""
    raw = text.encode("utf-8")
    cp = struct.pack(">H", 3)  # constant_pool_count (index 0 unused)
    cp += b"\x01" + struct.pack(">H", len(raw)) + raw  # Utf8 #1
    cp += b"\x08" + struct.pack(">H", 1)  # String #2 -> #1
    out = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 52) + cp
    out += struct.pack(">HHHHHH", 0x0021, 2, 2, 0, 0, 0)  # header, no members
    return out


def _make_fixture(tmp):
    path = os.path.join(tmp, "game.jar")
    smali = (
        ".class public LGame;\n"
        ".method public start()V\n"
        '    const-string v0, "Start Game"\n'
        '    const-string v1, "Quit"\n'
        ".end method\n"
    )
    props = (
        "# game strings\n"
        "menu.start=Start Game\n"
        "menu.quit=Quit\n"
        "menu.score=Your score: {0}\n"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nMIDlet-Name: Game\n")
        zf.writestr("META-INF/GAME.SF", "Signature-Version: 1.0\n")
        zf.writestr("strings/messages.properties", props)
        zf.writestr("smali/Game.smali", smali)
        zf.writestr("Game.class", _make_class_with_string("Hello Player"))
    return path


class TestJarPipeline(unittest.TestCase):
    def test_collect_finds_all_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            res = jar_mod.collect_strings(jar)
            found = set(res["strings"])
            self.assertIn("Start Game", found)
            self.assertIn("Quit", found)
            self.assertIn("Your score: {0}", found)
            self.assertIn("Hello Player", found)
            self.assertEqual(res["files"]["properties"], 1)
            self.assertEqual(res["files"]["smali"], 1)
            self.assertEqual(res["files"]["classes"], 1)

    def test_patch_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            out = os.path.join(tmp, "game_id.jar")
            stats = jar_mod.patch_jar(jar, {
                "Start Game": "Mulai Game",
                "Quit": "Keluar",
                "Hello Player": "Halo Pemain",
                "Your score: {0}": "Skor kamu: {0}",
            }, out)
            self.assertGreaterEqual(stats["properties"], 3)
            self.assertGreaterEqual(stats["smali"], 2)
            self.assertGreaterEqual(stats["classes"], 1)
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                self.assertNotIn("META-INF/GAME.SF", names)  # stale sig stripped
                self.assertIn("META-INF/MANIFEST.MF", names)
                props = zf.read("strings/messages.properties").decode()
                self.assertIn("Mulai Game", props)
                self.assertIn("Keluar", props)
                smali = zf.read("smali/Game.smali").decode()
                self.assertIn("Mulai Game", smali)
            # patched jar re-collects translated strings
            res2 = jar_mod.collect_strings(out)
            self.assertIn("Mulai Game", res2["strings"])

    def test_zip_slip_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = os.path.join(tmp, "evil.jar")
            with zipfile.ZipFile(jar, "w") as zf:
                zf.writestr("../../evil.txt", "boom")
            patcher = JarPatcher(jar, workspace=os.path.join(tmp, "ws"))
            with self.assertRaises(ValueError):
                patcher.extract()

    def test_identical_mapping_no_rewrite(self):
        """A mapping that changes nothing must report 0 replacements."""
        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            out = os.path.join(tmp, "game_same.jar")
            stats = jar_mod.patch_jar(jar, {
                "Start Game": "Start Game",
                "Quit": "Quit",
                "Hello Player": "Hello Player",
            }, out)
            self.assertEqual(stats["properties"], 0)
            self.assertEqual(stats["smali"], 0)
            self.assertEqual(stats["classes"], 0)

    def test_whitespace_fragment_patched_and_preserved(self):
        # .class pool strings may carry meaningful surrounding whitespace
        # (format fragments like ", num samples: "). Collection strips
        # keys, so patch must match on the stripped key AND re-apply the
        # original whitespace -- otherwise the translation never lands.
        with tempfile.TemporaryDirectory() as tmp:
            jar = os.path.join(tmp, "ws.jar")
            smali = (
                ".class public LGame;\n"
                ".method public start()V\n"
                '    const-string v0, "Score: "\n'
                ".end method\n"
            )
            with zipfile.ZipFile(jar, "w") as zf:
                zf.writestr("Game.class",
                            _make_class_with_string(", num samples: "))
                zf.writestr("Game.smali", smali)
            out = os.path.join(tmp, "ws_id.jar")
            stats = jar_mod.patch_jar(jar, {
                ", num samples:": "[ID] , num samples:",
                "Score:": "[ID] Score:",
            }, out)
            self.assertGreaterEqual(stats["classes"], 1)
            self.assertGreaterEqual(stats["smali"], 1)
            # verify the RAW pool/literal values (collect strips keys, so
            # read the raw entries to confirm whitespace was preserved)
            from core.patcher import iter_class_strings, iter_smali_literals
            with zipfile.ZipFile(out) as zf:
                class_vals = [v for _i, v in
                              iter_class_strings(zf.read("Game.class"))]
                smali_vals = [v for _s, _e, v in
                              iter_smali_literals(
                                  zf.read("Game.smali").decode())]
            self.assertIn("[ID] , num samples: ", class_vals)
            self.assertIn("[ID] Score: ", smali_vals)
        ok, _ = jar_mod.should_collect("Start Game")
        self.assertTrue(ok)
        ok, reason = jar_mod.should_collect("")
        self.assertFalse(ok)
        self.assertEqual(reason, "empty")
        ok, _ = jar_mod.should_collect("ab")  # below minimum=3
        self.assertFalse(ok)
        ok, _ = jar_mod.should_collect("12345")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
