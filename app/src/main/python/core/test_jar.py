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

    def test_patch_accepts_java_map(self):
        # Regression: Kotlin passes a Java LinkedHashMap to patch_jar
        # via Chaquopy; it has no .items() and fails isinstance(dict).
        # A REAL Chaquopy Java Set proxy is NOT Python-iterable either
        # -- ``for e in entry_set`` raises TypeError. FakeJavaSet
        # reproduces that: traversal ONLY via .iterator()/hasNext()/next().
        class FakeEntry:
            def __init__(self, k, v):
                self._k, self._v = k, v

            def getKey(self):
                return self._k

            def getValue(self):
                return self._v

        class FakeJavaSet:
            def __init__(self, entries):
                self._entries = list(entries)

            def __iter__(self):
                raise TypeError(
                    "'LinkedHashMap$LinkedEntrySet' object is not iterable")

            def iterator(self):
                return FakeJavaIterator(self._entries)

        class FakeJavaIterator:
            def __init__(self, entries):
                self._entries = entries
                self._i = 0

            def hasNext(self):
                return self._i < len(self._entries)

            def next(self):
                if not self.hasNext():
                    raise StopIteration
                e = self._entries[self._i]
                self._i += 1
                return e

        class FakeJavaMap:
            def __init__(self, data):
                self._data = dict(data)

            def entrySet(self):
                return FakeJavaSet([FakeEntry(k, v)
                                    for k, v in self._data.items()])

            def get(self, k):
                return self._data.get(k)

            def __len__(self):
                return len(self._data)

            def __bool__(self):
                return bool(self._data)

        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            out = os.path.join(tmp, "out.jar")
            # Fidelity lock: a real Chaquopy Set raises TypeError on
            # `for`. If this stops raising, the double no longer
            # reproduces the on-device crash.
            with self.assertRaises(TypeError):
                list(FakeJavaMap(
                    {"Start Game": "Mulai Game"}).entrySet())
            stats = jar_mod.patch_jar(
                jar, FakeJavaMap({"Start Game": "Mulai Game"}), out)
            self.assertGreaterEqual(stats["properties"], 1)
            with zipfile.ZipFile(out) as zf:
                self.assertIn("Mulai Game",
                              zf.read("strings/messages.properties").decode())

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

    def test_rebuild_refuses_same_file(self):
        # Rebuilding onto the input path truncates the source while the
        # source ZipFile still needs it (directory entries) -- the
        # input would be left as a 22-byte empty zip. Refuse loudly.
        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            with open(jar, "rb") as fh:
                before = fh.read()
            with self.assertRaises(ValueError):
                jar_mod.patch_jar(jar, {"Quit": "Keluar"}, jar)
            with open(jar, "rb") as fh:
                self.assertEqual(fh.read(), before)

    def test_rebuild_refuses_symlink_alias(self):
        # Round-2 audit: the same-file check used abspath, so a
        # symlinked alias of the input (link.jar -> real.jar) slipped
        # through and truncated the source. realpath catches it.
        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            link = os.path.join(tmp, "link.jar")
            os.symlink(jar, link)
            with open(jar, "rb") as fh:
                before = fh.read()
            with self.assertRaises(ValueError):
                jar_mod.patch_jar(link, {"Quit": "Keluar"}, jar)
            with open(jar, "rb") as fh:
                self.assertEqual(fh.read(), before)

    def test_rebuild_is_atomic_on_mid_write_failure(self):
        # Round-3 audit: rebuild wrote directly to the output path,
        # so a crash / disk-full mid-write left a PARTIAL zip there --
        # and the app's restoreLastOutput offers any *.jar in the
        # output dir, so the user could Save/Share a corrupt jar.
        # Now: write to .tmp + os.replace. A mid-write failure must
        # leave the previous output byte-identical, with no .tmp left.
        from core.patcher import JarPatcher
        with tempfile.TemporaryDirectory() as tmp:
            jar = _make_fixture(tmp)
            out = os.path.join(tmp, "out.jar")
            with open(out, "wb") as fh:
                fh.write(b"PREVIOUS COMPLETE OUTPUT")
            ws = os.path.join(tmp, "ws")
            patcher = JarPatcher(jar, workspace=ws)
            patcher.extract()
            orig_writestr = zipfile.ZipFile.writestr
            def boom(self, *a, **k):
                raise OSError("simulated disk-full")
            zipfile.ZipFile.writestr = boom
            try:
                with self.assertRaises(OSError):
                    patcher.rebuild(out)
            finally:
                zipfile.ZipFile.writestr = orig_writestr
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"PREVIOUS COMPLETE OUTPUT")
            self.assertFalse(os.path.exists(out + ".tmp"))
            # And a normal rebuild still works + leaves no .tmp.
            out2 = os.path.join(tmp, "out2.jar")
            patcher.rebuild(out2)
            self.assertTrue(zipfile.is_zipfile(out2))
            self.assertFalse(os.path.exists(out2 + ".tmp"))

    def test_class_oversize_translation_skipped(self):
        # A translation encoding to >65535 bytes cannot fit a Utf8
        # length field -- skip it (leave source) instead of OverflowError.
        from core.patcher import patch_class_strings
        data = _make_class_with_string("Hello Player")
        out, n = patch_class_strings(data, {"Hello Player": "X" * 70000})
        self.assertEqual(n, 0)
        self.assertEqual(out, data)
        out, n = patch_class_strings(data, {"Hello Player": "Halo"})
        self.assertEqual(n, 1)

    def test_translate_all_bypasses_filters(self):
        ok, _ = jar_mod.should_collect("12345", translate_all=True)
        self.assertTrue(ok)
        ok, _ = jar_mod.should_collect("ab", translate_all=True)
        self.assertTrue(ok)
        ok, reason = jar_mod.should_collect("", translate_all=True)
        self.assertFalse(ok)
        self.assertEqual(reason, "empty")


class TestPropertiesEdgeCases(unittest.TestCase):
    def test_continuation_strips_leading_whitespace(self):
        # Haiku audit: Java's Properties.load skips ALL leading
        # whitespace on a continuation line; KuroPatch kept it.
        from core.patcher import PropertiesFile
        pf = PropertiesFile.loads(b"key=first \\\n    second\n")
        self.assertEqual(pf.entries[0].value, "first second")

    def test_cr_only_line_endings_preserved(self):
        # Haiku audit: to_bytes() gave "" for \r-only lines, JOINING
        # them on rewrite (corruption of old-Mac .properties files).
        from core.patcher import PropertiesFile
        pf = PropertiesFile.loads(b"key=old\rkey2=v2\r")
        for entry in pf.entries:
            if entry.key == "key":
                entry.value = "new"
        self.assertEqual(pf.to_bytes(), b"key=new\rkey2=v2\r")

    def test_escape_value_astral_surrogate_pair(self):
        # Haiku audit: emoji -> \u1f600 (FIVE hex digits), which Java
        # reads as \u1f60 + literal "0". Must be a surrogate pair.
        from core.patcher import escape_value, unescape
        self.assertEqual(escape_value("\U0001F600", "ascii"),
                         "\\ud83d\\ude00")
        # Java strings ARE UTF-16, so a surrogate pair is the correct
        # representation there: unescape yields the two code units.
        self.assertEqual(unescape("\\ud83d\\ude00"), "\ud83d\ude00")

    def test_integer_value_bytes_not_pool_refs(self):
        # Haiku audit: Integer/Float payload bytes were added to
        # code_refs as if they were pool indices, wrongly excluding a
        # String Utf8 whose index coincided (missed translation).
        from core.patcher import _parse_constant_pool
        raw = b"Hello"
        cp = struct.pack(">H", 4)
        cp += b"\x01" + struct.pack(">H", len(raw)) + raw  # #1 Utf8
        cp += b"\x08" + struct.pack(">H", 1)  # #2 String -> #1
        cp += b"\x03" + struct.pack(">I", 0x00010000)  # #3 Integer
        data = b"\xca\xfe\xba\xbe" + struct.pack(">HH", 0, 52) + cp
        data += struct.pack(">HHHHHH", 0x0021, 2, 2, 0, 0, 0)
        _utf8, string_only = _parse_constant_pool(data)
        self.assertIn(1, string_only)

    def test_bom_stripped_from_first_key(self):
        # Round-2 audit: a UTF-8 BOM made the first key parse as
        # "\ufeffhello", so key lookups for "hello" missed.
        from core.patcher import PropertiesFile
        pf = PropertiesFile.loads(b"\xef\xbb\xbfhello=world\n")
        self.assertEqual([e.key for e in pf.entries], ["hello"])
        # Unchanged file round-trips byte-for-byte (BOM preserved).
        self.assertEqual(pf.to_bytes(), b"\xef\xbb\xbfhello=world\n")
        # Changed file still patches the value.
        pf.entries[0].value = "dunia"
        self.assertIn(b"dunia", pf.to_bytes())

    def test_class_oversize_boundary(self):
        # Round-2 audit: the 64KB skip is a boundary -- exactly 65535
        # bytes must patch, 65536 must skip. (The earlier probe used a
        # fixture whose Utf8 was also the class name, so it never
        # exercised this path at all.)
        from core.patcher import patch_class_strings
        data = _make_class_with_string("Hello Player")
        out, n = patch_class_strings(data, {"Hello Player": "X" * 65535})
        self.assertEqual(n, 1)
        out, n = patch_class_strings(data, {"Hello Player": "X" * 65536})
        self.assertEqual(n, 0)
        self.assertEqual(out, data)
        # Multibyte: 40000 U+00E9 = 80000 bytes -- skip on BYTES, not chars.
        out, n = patch_class_strings(data, {"Hello Player": "\u00e9" * 40000})
        self.assertEqual(n, 0)

    def test_smali_unterminated_string_does_not_swallow_next_line(self):
        # Round-2 (Agnes): the const-string body regex used to allow
        # \n, so an unterminated string swallowed the next line's
        # instruction into one bogus literal (corruption risk on
        # malformed smali).
        from core.patcher import iter_smali_literals
        content = 'const-string v0, "abc\n    const-string v1, "def"\n'
        self.assertEqual(
            [d for _, _, d in iter_smali_literals(content)], ["def"])

    def test_rebuild_preserves_stored_compression(self):
        # Round-2 (Agnes): rebuild used `compress_type or ZIP_DEFLATED`,
        # and ZIP_STORED is 0 (falsy) -- every stored entry was
        # silently recompressed to deflated.
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr(zipfile.ZipInfo("stored.txt"), "hello",
                           compress_type=zipfile.ZIP_STORED)
            out = os.path.join(td, "out.jar")
            p = JarPatcher(src, workspace=os.path.join(td, "ws"))
            try:
                p.extract()
                p.rebuild(out)
            finally:
                p.close()
            with zipfile.ZipFile(out) as z:
                self.assertEqual(
                    z.infolist()[0].compress_type, zipfile.ZIP_STORED)


if __name__ == "__main__":
    unittest.main()
