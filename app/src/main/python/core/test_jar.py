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

    def test_escaped_non_ascii_key_in_ascii_file(self):
        # Luna audit: an ASCII file whose key carries a \uXXXX escape
        # for a non-ASCII char ("caf\u00e9") parses to key "café".
        # The old render() escaped the key for utf-8 (raw "café")
        # and to_bytes() then crashed on the strict ASCII encode.
        from core.patcher import PropertiesFile
        pf = PropertiesFile.loads(b"caf\\u00e9=old\n")
        self.assertEqual(pf.entries[0].key, "café")
        self.assertEqual(pf.write_charset, "ascii")
        for entry in pf.entries:
            if entry.key == "café":
                entry.value = "new"
        self.assertEqual(pf.to_bytes(), b"caf\\u00e9=new\n")

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

    def test_bom_preserved_when_first_entry_changes(self):
        # Round-3 audit: replacing the first physical line dropped
        # the BOM (render() has no BOM), so an edited file lost its
        # signature bytes. Cosmetic, but byte churn for no reason.
        from core.patcher import PropertiesFile
        pf = PropertiesFile.loads(
            b"\xef\xbb\xbf" + b"first=one \\\n    two\nsecond=hello\n")
        pf.set("first", "eins zwei")
        out = pf.to_bytes()
        self.assertTrue(out.startswith(b"\xef\xbb\xbf"))
        pf2 = PropertiesFile.loads(out)
        self.assertEqual(pf2.get("first"), "eins zwei")
        self.assertEqual(pf2.get("second"), "hello")

    def test_empty_and_dot_entry_names_do_not_crash_extract(self):
        # Round-3 audit: an entry named "" or "." made extract()
        # target the workspace directory itself (IsADirectoryError),
        # crashing collect/patch on a malformed jar.
        with tempfile.TemporaryDirectory() as td:
            jar = os.path.join(td, "weird.jar")
            with zipfile.ZipFile(jar, "w") as z:
                z.writestr("", b"weird")
                z.writestr(".", b"dot")
                z.writestr("ok.properties", b"msg=Hello world\n")
            res = jar_mod.collect_strings(jar)
            self.assertIn("Hello world", res["strings"])

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

    def test_rebuild_removes_stale_tmp(self):
        # Round-3 (Haiku): a previous run killed mid-rebuild leaves
        # out.jar.tmp behind. Rebuild must clean it up, not let .tmp
        # files pile up in the output dir.
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("plain.txt", "hello")
            out = os.path.join(td, "out.jar")
            with open(out + ".tmp", "wb") as fh:
                fh.write(b"junk from a killed run")
            p = JarPatcher(src, workspace=os.path.join(td, "ws"))
            try:
                p.extract()
                p.rebuild(out)
            finally:
                p.close()
            self.assertFalse(os.path.exists(out + ".tmp"))
            with zipfile.ZipFile(out) as z:
                self.assertIsNone(z.testzip())

    def test_rebuild_tmp_path_not_spliced_as_entry(self):
        # Round-3 (Haiku): the rebuild tmp file lives next to the
        # output. If the archive contains an entry whose workspace
        # path IS that tmp file, the half-written tmp must never be
        # read back as entry data -- the entry must come from the
        # source archive. (Guards the tmp skip-set line; the
        # pre-audit code had no tmp file at all.)
        with tempfile.TemporaryDirectory() as td:
            ws = os.path.join(td, "ws")
            src = os.path.join(td, "in.jar")
            out = os.path.join(ws, "out.jar")  # output inside workspace
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("out.jar.tmp", "ORIGINAL")
                z.writestr("plain.txt", "hello")
            p = JarPatcher(src, workspace=ws)
            try:
                p.extract()
                p.rebuild(out)
                # Read BEFORE close(): close() wipes the workspace,
                # and the output lives inside it here.
                with zipfile.ZipFile(out) as z:
                    got = z.read("out.jar.tmp")
            finally:
                p.close()
            self.assertEqual(got, b"ORIGINAL")

    def test_pack_stats_not_counted_on_failed_build(self):
        # Luna audit: patch_jar incremented stats["packs"] BEFORE
        # build_resource succeeded -- a failed build left the file
        # untouched but the stats claimed replacements that never
        # landed. Here the translation overflows the pack's 1-byte
        # offset width (strdata > 255), so the build must fail and
        # stats["packs"] must stay 0.
        from core.stringpacks import StringPack, build_resource
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.jar")
            resource = build_resource(
                [StringPack(es=1, strings=["Hello", "World"])])
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("RES_STRINGS", resource)
            out = os.path.join(td, "out.jar")
            stats = jar_mod.patch_jar(src, {"Hello": "X" * 300}, out)
            self.assertEqual(stats["packs"], 0)
            with zipfile.ZipFile(out) as z:
                self.assertEqual(z.read("RES_STRINGS"), resource)

    def test_lzma_decompression_cap(self):
        # Luna audit: parse_resource decompressed with no cap -- a
        # crafted resource could OOM the process (decompression bomb).
        from core import stringpacks
        from core.stringpacks import (
            StringPack, build_resource, parse_resource)
        raw = build_resource([StringPack(es=1, strings=["Hello"])])
        blob = raw  # build_resource already returns LZMA-compressed bytes
        old_cap = stringpacks._MAX_DECOMPRESSED
        try:
            stringpacks._MAX_DECOMPRESSED = 4  # smaller than the pack
            with self.assertRaises(ValueError):
                parse_resource(blob)
        finally:
            stringpacks._MAX_DECOMPRESSED = old_cap
        # The real cap still accepts the small valid pack.
        self.assertEqual(parse_resource(blob)[0].strings, ["Hello"])

    def test_patch_jar_rejects_non_string_translation(self):
        # Round-4 (Luna): passing collect's raw {value: [origins]}
        # dict to patch_jar died deep in _match_translation with a
        # raw TypeError (str + list). Must fail loud at the boundary.
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "in.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("a.properties", "x=Hello\n")
            with self.assertRaises(ValueError) as ctx:
                jar_mod.patch_jar(src, {"Hello": ["in.jar::x"]},
                                  os.path.join(td, "out.jar"))
            self.assertIn("not a string", str(ctx.exception))


class TestLanguageNameKeysNotTranslated(unittest.TestCase):
    """On-device bug (Castlevania): patching translated language names
    ('English' -> 'Inggris') into .class pools bricked the game.

    The names are logic keys, not display text: DOSMidlet.aE() reads
    names from TRAD.lang via readUTF() and matches them with
    String.equals() against a static String[] built in <clinit>.
    Translated keys never match -> language lookup finds nothing ->
    blank white screen in J2ME Loader. So .class pools must never
    have language names translated -- neither collected nor patched.
    """

    def test_class_language_names_not_collected(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("Game.class",
                           _make_class_with_string("English"))
                z.writestr("Other.class",
                           _make_class_with_string("DEUTSCH"))
                z.writestr("Third.class",
                           _make_class_with_string("Hello Player"))
            got = jar_mod.collect_strings(src)
            self.assertNotIn("English", got["strings"])
            self.assertNotIn("DEUTSCH", got["strings"])
            self.assertIn("Hello Player", got["strings"])

    def test_class_language_names_not_patched(self):
        # Defense in depth: even a hand-made map containing language
        # names must not rewrite them in .class pools.
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            out = os.path.join(td, "out.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("Game.class",
                           _make_class_with_string("English"))
                z.writestr("a.properties", "greet=Hello Player\n")
            jar_mod.patch_jar(src, {"English": "Inggris",
                                    "Hello Player": "Halo Pemain"}, out)
            with zipfile.ZipFile(out) as z:
                self.assertIn(b"English", z.read("Game.class"))
                self.assertNotIn(b"Inggris", z.read("Game.class"))
                self.assertIn(b"Halo Pemain", z.read("a.properties"))

    def test_non_class_language_names_not_collected(self):
        # Final audit: the .class-only guard missed .properties and
        # smali. A game matching `language.name=English` from a
        # .properties file (or a smali const-string "Deutsch") via
        # equals() bricks exactly like Castlevania's .class case.
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            smali = (
                ".class public LGame;\n"
                ".method public start()V\n"
                '    const-string v0, "Deutsch"\n'
                '    const-string v1, "Start Game"\n'
                ".end method\n"
            )
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("game.properties",
                           "language.name=English\nwelcome=Welcome\n")
                z.writestr("Game.smali", smali)
            got = jar_mod.collect_strings(src)
            self.assertNotIn("English", got["strings"])
            self.assertNotIn("Deutsch", got["strings"])
            self.assertIn("Welcome", got["strings"])
            self.assertIn("Start Game", got["strings"])

    def test_non_class_language_names_not_patched(self):
        # Defense in depth: a hand-made map with language names must
        # not rewrite them in .properties or smali either, while real
        # translations still land.
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            out = os.path.join(td, "out.jar")
            smali = (
                ".class public LGame;\n"
                ".method public start()V\n"
                '    const-string v0, "Deutsch"\n'
                '    const-string v1, "Start Game"\n'
                ".end method\n"
            )
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("game.properties",
                           "language.name=English\nwelcome=Welcome\n")
                z.writestr("Game.smali", smali)
            jar_mod.patch_jar(src, {"English": "Inggris",
                                    "Deutsch": "Jerman",
                                    "Welcome": "Selamat datang",
                                    "Start Game": "Mulai Game"}, out)
            with zipfile.ZipFile(out) as z:
                props = z.read("game.properties").decode()
                self.assertIn("language.name=English", props)
                self.assertNotIn("Inggris", props)
                self.assertIn("Selamat datang", props)
                smali_out = z.read("Game.smali").decode()
                self.assertIn('"Deutsch"', smali_out)
                self.assertNotIn("Jerman", smali_out)
                self.assertIn("Mulai Game", smali_out)

    def test_data_key_prose_embedded_candidate_not_dropped(self):
        # _data_key_strings must only flag a candidate stored as its
        # OWN entry in a data file. A candidate embedded in a longer
        # prose run ("Help: Press SELECT LANGUAGE to continue now!")
        # is display text, not a key -- it must still be collected.
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("game.properties",
                           "sel=SELECT LANGUAGE\n")
                z.writestr(
                    "data.dat",
                    b"Help: Press SELECT LANGUAGE to continue now!\x00")
            got = jar_mod.collect_strings(src)
            self.assertIn("SELECT LANGUAGE", got["strings"])
            self.assertEqual(got["data_keys_dropped"], 0)

    def test_real_castlevania_language_keys_survive(self):
        # The exact broken jar: the 12 language-name strings must not
        # be collected, and a patch map containing them must leave the
        # .class entries byte-identical (so the equals() match in
        # DOSMidlet.aE() still succeeds against TRAD.lang).
        import core.jar as jar_mod
        src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "user", "files",
            "castlevania_dawn_of_sorrow_320x240-276636_2_5uug.jar")
        src = os.path.normpath(src)
        if not os.path.exists(src):
            self.skipTest("Kovak's Castlevania jar not present")
        got = jar_mod.collect_strings(src)
        lang_names = ["Deutsch", "English", "Español", "Français",
                      "Italiano", "Português", "DEUTSCH", "ENGLISH",
                      "ESPAÑOL", "FRANÇAIS", "ITALIANO", "PORTUGUÊS"]
        for name in lang_names:
            self.assertNotIn(name, got["strings"],
                             f"language key {name!r} must not be collected")
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "out.jar")
            evil = {n: "X" + n for n in lang_names}
            evil["DOSBoss loading exception"] = "Pengecualian pemuatan"
            jar_mod.patch_jar(src, evil, out)
            with zipfile.ZipFile(src) as zo, zipfile.ZipFile(out) as zn:
                # Each class must keep exactly the language keys it
                # originally had (DOSMidlet: mixed-case, c: ALL-CAPS).
                # A translated key breaks the equals() match in
                # DOSMidlet.aE() against TRAD.lang.
                for entry in ("DOSMidlet.class", "c.class"):
                    orig = zo.read(entry)
                    new = zn.read(entry)
                    for name in lang_names:
                        if name.encode("utf-8") in orig:
                            self.assertIn(name.encode("utf-8"), new,
                                          f"{entry} lost language key {name!r}")


class TestDataKeyDelimiters(unittest.TestCase):
    """Final audit round 1: _data_key_strings exact-run matching missed
    keys stored as key=value, a;b;c and "quoted" entries -- only bare
    runs were caught. Runs are now also split on structural
    delimiters (= ; : , | " ' ...). Spaces stay non-delimiters so
    prose words are never flagged (see sibling's prose test)."""

    def _jar(self, td, data_content, candidate):
        src = os.path.join(td, "game.jar")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("data.txt", data_content)
            z.writestr("Game.class", _make_class_with_string(candidate))
        return src

    def test_key_value_style_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "item=SwordOfFire\nlevel=3\n", "SwordOfFire")
            got = jar_mod.collect_strings(src)
            self.assertNotIn("SwordOfFire", got["strings"])

    def test_semicolon_list_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "SwordOfFire;IceShield;FireBow",
                             "SwordOfFire")
            got = jar_mod.collect_strings(src)
            self.assertNotIn("SwordOfFire", got["strings"])

    def test_quoted_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, 'selected="SwordOfFire"\n', "SwordOfFire")
            got = jar_mod.collect_strings(src)
            self.assertNotIn("SwordOfFire", got["strings"])

    def test_colon_style_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "item: SwordOfFire\n", "SwordOfFire")
            got = jar_mod.collect_strings(src)
            self.assertNotIn("SwordOfFire", got["strings"])

    def test_multi_pair_with_spaces_dropped(self):
        # "key=value; other=thing": spaces AFTER a delimiter are
        # delimiter-adjacent, never inside a token -- still key-shaped,
        # still dropped.
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "item=SwordOfFire; qty=3\n", "SwordOfFire")
            got = jar_mod.collect_strings(src)
            self.assertNotIn("SwordOfFire", got["strings"])


class TestDataKeyProseGate(unittest.TestCase):
    """Final audit round 2: the round-1 delimiter split over-skipped
    ordinary prose words -- "Welcome, traveler!" splits on "," to a
    "Welcome" token, "Help! ..." to "Help", "Menu (press ...)" to
    "Menu", "Tom & Jerry" to "Tom". Token matching now applies only
    to key-shaped runs (no token contains a space, no sentence
    boundary, no trailing !/?). Each test below FAILED on the
    round-1 code and PASSES with the gate."""

    def _jar(self, td, data_content, candidate):
        src = os.path.join(td, "game.jar")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("data.txt", data_content)
            z.writestr("Game.class", _make_class_with_string(candidate))
        return src

    def test_comma_prose_not_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Welcome, traveler! Enjoy your stay.\n", "Welcome")
            self.assertIn("Welcome", jar_mod.collect_strings(src)["strings"])

    def test_bang_prose_not_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Help! Press start to continue.\n", "Help")
            self.assertIn("Help", jar_mod.collect_strings(src)["strings"])

    def test_paren_prose_not_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Menu (press START) to continue.\n", "Menu")
            self.assertIn("Menu", jar_mod.collect_strings(src)["strings"])

    def test_amp_prose_not_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Tom & Jerry are friends.\n", "Tom")
            self.assertIn("Tom", jar_mod.collect_strings(src)["strings"])

    def test_staccato_prose_not_dropped(self):
        # Single-word tokens only ("Run! Fight! Win!" splits on "!"
        # to bare tokens), but "! " sentence boundaries make it
        # prose, not a key list. ("Go" would also be dropped by the
        # minimum-length filter, so the candidate is "Run".)
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Run! Fight! Win!\n", "Run")
            self.assertIn("Run", jar_mod.collect_strings(src)["strings"])

    def test_single_word_exclamation_not_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Welcome!\n", "Welcome")
            self.assertIn("Welcome", jar_mod.collect_strings(src)["strings"])


class TestDataKeyUTF16(unittest.TestCase):
    """Final audit round 2: _data_key_strings only scanned UTF-8 runs.
    A key stored as UTF-16LE / UTF-16BE text in a data file (a
    UTF-16 TRAD-style index) was invisible -- the .class twin got
    collected and translated, breaking the lookup the same way the
    Castlevania language names did. Runs are now also extracted in
    UTF-16LE/BE (Latin-1 range) and matched the same way."""

    def _jar(self, td, data_bytes, candidate):
        src = os.path.join(td, "game.jar")
        with zipfile.ZipFile(src, "w") as z:
            z.writestr("data.dat", data_bytes)
            z.writestr("Game.class", _make_class_with_string(candidate))
        return src

    def test_utf16le_bare_key_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, b"\x00\x00" + "CrystalSword".encode("utf-16-le")
                            + b"\x00\x00", "CrystalSword")
            self.assertNotIn("CrystalSword", jar_mod.collect_strings(src)["strings"])

    def test_utf16be_bare_key_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, b"\x00\x00" + "CrystalSword".encode("utf-16-be")
                            + b"\x00\x00", "CrystalSword")
            self.assertNotIn("CrystalSword", jar_mod.collect_strings(src)["strings"])

    def test_utf16le_key_value_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "item=CrystalSword\n".encode("utf-16-le"),
                            "CrystalSword")
            self.assertNotIn("CrystalSword", jar_mod.collect_strings(src)["strings"])

    def test_utf16le_non_ascii_key_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "CaféSword".encode("utf-16-le"), "CaféSword")
            self.assertNotIn("CaféSword", jar_mod.collect_strings(src)["strings"])

    def test_utf16le_with_bom_dropped(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, b"\xff\xfe" + "CrystalSword".encode("utf-16-le"),
                            "CrystalSword")
            self.assertNotIn("CrystalSword", jar_mod.collect_strings(src)["strings"])

    def test_utf16le_prose_not_dropped(self):
        # Prose-safe must hold in UTF-16 too: a candidate embedded in a
        # UTF-16 sentence is NOT a key (spaces are not delimiters).
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = self._jar(td, "Help me find the CrystalSword today".encode("utf-16-le"),
                            "CrystalSword")
            self.assertIn("CrystalSword", jar_mod.collect_strings(src)["strings"])


class TestReverseLanguageNames(unittest.TestCase):
    """Final audit round 1: _is_language_name only recognised the
    English/autonym form ('English'). A double-patch (patching an
    already-patched jar) or a game shipping Indonesian labels
    ('Inggris' in .class) would have its language names
    re-translated. Indonesian language names are language names too."""

    def test_indonesian_names_not_collected(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("Game.class", _make_class_with_string("Inggris"))
                z.writestr("Other.class", _make_class_with_string("Jerman"))
                z.writestr("Third.class",
                           _make_class_with_string("Hello Player"))
            got = jar_mod.collect_strings(src)
            self.assertNotIn("Inggris", got["strings"])
            self.assertNotIn("Jerman", got["strings"])
            self.assertIn("Hello Player", got["strings"])

    def test_indonesian_names_not_patched(self):
        import core.jar as jar_mod
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "game.jar")
            out = os.path.join(td, "out.jar")
            with zipfile.ZipFile(src, "w") as z:
                z.writestr("Game.class", _make_class_with_string("Inggris"))
                z.writestr("a.properties", "greet=Hello Player\n")
            jar_mod.patch_jar(src, {"Inggris": "English",
                                    "Hello Player": "Halo Pemain"}, out)
            with zipfile.ZipFile(out) as z:
                self.assertIn(b"Inggris", z.read("Game.class"))
                self.assertNotIn(b"English", z.read("Game.class"))
                self.assertIn(b"Halo Pemain", z.read("a.properties"))

    def test_region_qualified_indonesian_name(self):
        import core.jar as jar_mod
        self.assertTrue(jar_mod._is_language_name("Inggris (US)"))
        self.assertTrue(jar_mod._is_language_name("English (US)"))
        self.assertFalse(jar_mod._is_language_name("English version"))



if __name__ == "__main__":
    unittest.main()
