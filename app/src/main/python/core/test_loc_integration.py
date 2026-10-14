"""Integration tests for the Gameloft TRAD_*.loc pipeline.

Covers the seams between modules (Agnes's core/loc.py is tested
separately in tests/test_loc.py): collect + patch in core/jar.py,
$N placeholder protection in translate/guard.py, and the .loc rules
in the provider prompt (translate/providers.py).

No network, no API keys -- the "provider" is a marker stub.
The real-jar E2E test skips when Kovak's Castlevania jar is absent
(it is not in CI).
"""

import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import jar as jar_mod
from core import loc as loc_mod
from translate.engine import results_to_dict, translate_strings
from translate.guard import extract_placeholders
from translate.providers import AgnesProvider, BaseProvider
from translate.validator import validate

CASTLEVANIA_JAR = os.path.expanduser(
    "~/workspace/user/files/"
    "castlevania_dawn_of_sorrow_320x240-276636_2_5uug.jar"
)


class _MarkerProvider(BaseProvider):
    """Stub provider: appends a marker, preserves everything else."""

    name = "marker"

    def translate(self, text, source_lang="en", target_lang="id"):
        # The engine passes protect_hint(text), which appends a
        # "[Keep these terms ...]" note -- strip it back off.
        base = text.split("\n[Keep these terms")[0]
        return base + " [ID]"


class _DropDollarProvider(BaseProvider):
    """Stub provider that DROPS $N codes -- must fail validation."""

    name = "drop-dollar"

    def translate(self, text, source_lang="en", target_lang="id"):
        base = text.split("\n[Keep these terms")[0]
        return base.replace("$0", "").replace("$1", "").replace("$2", "")


def _make_loc_table():
    """A 400-record .loc table exercising every rule."""
    records = [""] * 400
    records[0] = "0"  # do-not-translate dummy
    records[1] = "English"  # do-not-translate language self-name
    records[2] = "Hello world"
    records[63] = "Line one of the story.\nLine two of the story.\nLine three."
    records[64] = "Castlevania\nDawn of Sorrow"  # do-not-translate title
    records[70] = ""  # do-not-translate empty
    records[100] = "$0Help line one\n$1Help line two"  # $N codes
    records[101] = "QUIT GAME"  # duplicates: same translation for both
    records[102] = "QUIT GAME"
    records[200] = "Press 4/LEFT to move"
    records[201] = "Celia... "  # trailing-space fragment
    return records


def _make_jar_with_loc(path, records=None, trad_lang=True, loc_bytes=None):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nMIDlet-Name: Game\n")
        if trad_lang:
            # Language index: names are lookup keys, never collected.
            zf.writestr("TRAD.lang", b"\x06English\x02\x00'\n")
        zf.writestr("TRAD_english.loc",
                    loc_bytes if loc_bytes is not None
                    else loc_mod.build(records if records is not None
                                       else _make_loc_table()))
    return path


class TestDollarCodePlaceholder(unittest.TestCase):
    def test_extract_dollar_codes(self):
        self.assertEqual(extract_placeholders("$0Help $1more"),
                         ["$0", "$1"])
        self.assertEqual(extract_placeholders("Price $5"), ["$5"])
        self.assertEqual(extract_placeholders("No codes here"), [])

    def test_validator_rejects_dropped_dollar_code(self):
        self.assertFalse(validate("$0Help text", "Bantuan"))
        self.assertFalse(validate("Price $5", "Harga 5"))

    def test_validator_accepts_preserved_dollar_code(self):
        self.assertTrue(validate("$0Help text", "$0Bantuan"))
        self.assertTrue(validate("Price $5", "Harga $5"))

    def test_ellipsis_fragment_not_identifier(self):
        # REGRESSION: dialog fragments ending in "..." were skipped as
        # "identifier" (the dots looked like separators), so they
        # stayed untranslated in every game. Trailing dots are
        # ellipsis punctuation; real identifiers keep separators
        # BETWEEN word chars and are still skipped.
        from translate.guard import should_skip
        self.assertFalse(should_skip("Wait...")[0])
        self.assertFalse(should_skip("Celia...")[0])
        self.assertTrue(should_skip("com.game.Main")[0])
        self.assertTrue(should_skip("menu_start")[0])
        self.assertTrue(should_skip("level-1")[0])

    def test_engine_fails_dollar_dropping_provider(self):
        # The engine's placeholder check must catch a provider that
        # drops $N -- the string stays untranslated, never ships
        # with a silently dropped style code.
        results = translate_strings({"k": "$0Help text"},
                                    provider=_DropDollarProvider())
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(results[0].translation, "$0Help text")


class TestProviderPrompt(unittest.TestCase):
    def test_prompt_has_loc_rules(self):
        prompt = AgnesProvider()._build_prompt("Hello", "en", "id")
        self.assertIn("$N", prompt)
        self.assertIn("line breaks", prompt)
        self.assertIn("ASCII", prompt)
        self.assertIn("proper nouns", prompt)
        self.assertIn("fragment", prompt)
        self.assertIn("credits", prompt)

    def test_prompt_ascii_scoped_to_indonesian(self):
        # Other targets may legitimately need accented characters.
        prompt_fr = AgnesProvider()._build_prompt("Hello", "en", "fr")
        self.assertNotIn("ASCII", prompt_fr)


class TestLocCollect(unittest.TestCase):
    def test_collect_finds_loc_strings(self):
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"))
            res = jar_mod.collect_strings(jar)
            self.assertEqual(res["files"]["loc"], 1)
            self.assertIn("Hello world", res["strings"])
            self.assertIn("Line one of the story.\nLine two of the story.\nLine three.",
                          res["strings"])
            self.assertIn("$0Help line one\n$1Help line two", res["strings"])
            origins = res["strings"]["Hello world"]
            self.assertTrue(any(o.startswith("TRAD_english.loc::loc#")
                                for o in origins))

    def test_collect_skips_do_not_translate(self):
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"))
            res = jar_mod.collect_strings(jar)
            # Title, language self-name, dummy, empty: never collected.
            self.assertNotIn("Castlevania\nDawn of Sorrow", res["strings"])
            self.assertNotIn("English", res["strings"])
            self.assertNotIn("0", res["strings"])
            self.assertNotIn("", res["strings"])

    def test_collect_loc_strings_not_dropped_as_data_keys(self):
        # REGRESSION: .loc records are their own maximal printable
        # runs -- without the .loc exclusion in _data_key_strings,
        # every .loc string would be dropped as a "lookup key" and
        # collect would find nothing.
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"))
            res = jar_mod.collect_strings(jar)
            self.assertIn("Hello world", res["strings"])
            self.assertIn("$0Help line one\n$1Help line two", res["strings"])

    def test_collect_malformed_loc_skipped_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"),
                                     loc_bytes=b"garbage, not a loc file")
            res = jar_mod.collect_strings(jar)
            self.assertEqual(res["files"]["loc"], 0)
            self.assertEqual(res["strings"], {})


class TestLocPatch(unittest.TestCase):
    def _collect_translate_patch(self, jar_path):
        res = jar_mod.collect_strings(jar_path)
        src = {v: v for v in res["strings"]}
        translations = results_to_dict(
            translate_strings(src, provider=_MarkerProvider()))
        out = jar_path + ".out.jar"
        stats = jar_mod.patch_jar(jar_path, translations, out)
        return out, stats

    def test_patch_end_to_end_synthetic(self):
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"))
            out, stats = self._collect_translate_patch(jar)
            self.assertEqual(stats["loc"], 7)  # rec 2, 63, 100, 101, 102, 200, 201
            self.assertEqual(stats["loc_reverted"], 0)
            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
                patched = loc_mod.parse(zf.read("TRAD_english.loc"))
                self.assertEqual(len(patched), 400)
                # Changed records carry the marker; $N / \n preserved.
                self.assertTrue(patched[2].endswith(" [ID]"))
                self.assertEqual(patched[63].count("\n"), 2)
                self.assertTrue(patched[63].endswith(" [ID]"))
                self.assertIn("$0", patched[100])
                self.assertIn("$1", patched[100])
                self.assertEqual(patched[100].count("\n"), 1)
                # Duplicates translated identically (by index, same map).
                self.assertEqual(patched[101], patched[102])
                # Trailing-space fragment: whitespace preserved.
                self.assertTrue(patched[201].endswith(" "))
                # Do-not-translate records byte-identical.
                with zipfile.ZipFile(jar) as zf_orig:
                    orig = loc_mod.parse(zf_orig.read("TRAD_english.loc"))
                for i in (0, 1, 64, 70):
                    self.assertEqual(patched[i], orig[i])
                # Manifest: the patcher normalizes line endings to
                # CRLF (existing sanitize behavior) -- content kept.
                manifest = zf.read("META-INF/MANIFEST.MF")
                self.assertIn(b"MIDlet-Name: Game", manifest)
                # TRAD.lang untouched.
                self.assertEqual(zf.read("TRAD.lang"), b"\x06English\x02\x00'\n")

    def test_patch_reverts_bad_translation_per_record(self):
        # Hand-made map: one good translation, one that drops $N.
        # The bad record reverts to source; the good one still lands.
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"))
            translations = {
                "Hello world": "Halo dunia",
                "$0Help line one\n$1Help line two": "Bantuan tanpa kode",
            }
            out = os.path.join(td, "out.jar")
            stats = jar_mod.patch_jar(jar, translations, out)
            self.assertEqual(stats["loc"], 1)
            self.assertEqual(stats["loc_reverted"], 1)
            with zipfile.ZipFile(out) as zf:
                patched = loc_mod.parse(zf.read("TRAD_english.loc"))
            self.assertEqual(patched[2], "Halo dunia")
            self.assertEqual(patched[100], "$0Help line one\n$1Help line two")

    def test_patch_never_touches_do_not_translate(self):
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"))
            translations = {
                "Castlevania\nDawn of Sorrow": "Castlevania\nFajar Kesedihan",
                "English": "Bahasa Inggris",
            }
            out = os.path.join(td, "out.jar")
            stats = jar_mod.patch_jar(jar, translations, out)
            self.assertEqual(stats["loc"], 0)
            with zipfile.ZipFile(out) as zf:
                patched = loc_mod.parse(zf.read("TRAD_english.loc"))
            self.assertEqual(patched[64], "Castlevania\nDawn of Sorrow")
            self.assertEqual(patched[1], "English")

    def test_patch_malformed_loc_skipped_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            jar = _make_jar_with_loc(os.path.join(td, "game.jar"),
                                     loc_bytes=b"garbage, not a loc file")
            out = os.path.join(td, "out.jar")
            stats = jar_mod.patch_jar(jar, {"Hello": "Halo"}, out)
            self.assertEqual(stats["loc"], 0)
            with zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
                self.assertEqual(zf.read("TRAD_english.loc"),
                                 b"garbage, not a loc file")


@unittest.skipUnless(os.path.exists(CASTLEVANIA_JAR),
                     "Kovak's Castlevania jar not present")
class TestRealCastlevaniaLoc(unittest.TestCase):
    def test_collect_real_jar(self):
        res = jar_mod.collect_strings(CASTLEVANIA_JAR)
        self.assertEqual(res["files"]["loc"], 1)
        # Story record 63 is collected; title / names are not.
        self.assertTrue(any("solar eclipse" in v for v in res["strings"]))
        self.assertNotIn("Castlevania\nDawn of Sorrow", res["strings"])
        self.assertNotIn("English", res["strings"])
        self.assertTrue(any(o.startswith("TRAD_english.loc::loc#")
                            for v in res["strings"].values() for o in v))

    def test_patch_real_jar_end_to_end(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "castlevania_id.jar")
            res = jar_mod.collect_strings(CASTLEVANIA_JAR)
            src = {v: v for v in res["strings"]}
            translations = results_to_dict(
                translate_strings(src, provider=_MarkerProvider()))
            stats = jar_mod.patch_jar(CASTLEVANIA_JAR, translations, out)
            self.assertGreater(stats["loc"], 0)
            # Reverted records (if any) are translations that failed
            # .loc validation (e.g. the "[ID]" marker pushed a
            # near-limit line over the 236-char cap) -- the final
            # table must validate cleanly either way.
            with zipfile.ZipFile(CASTLEVANIA_JAR) as zf_orig, \
                    zipfile.ZipFile(out) as zf:
                self.assertIsNone(zf.testzip())
                orig_names = zf_orig.namelist()
                self.assertEqual(zf.namelist(), orig_names)
                orig = loc_mod.parse(zf_orig.read("TRAD_english.loc"))
                patched = loc_mod.parse(zf.read("TRAD_english.loc"))
                self.assertEqual(len(patched), 400)
                # The shipped table validates: every changed record
                # passed $N / \n / ASCII / line-length checks (bad
                # ones were reverted to source by patch).
                loc_mod.validate(patched, orig)
                # Do-not-translate records byte-identical.
                for i in loc_mod.DO_NOT_TRANSLATE:
                    self.assertEqual(patched[i], orig[i], f"record {i}")
                # $N codes and \n counts preserved on every record.
                for i, (new, old) in enumerate(zip(patched, orig)):
                    if new == old:
                        continue
                    self.assertEqual(
                        loc_mod._dollar_codes(new), loc_mod._dollar_codes(old),
                        f"record {i} $N")
                    self.assertEqual(new.count("\n"), old.count("\n"),
                                     f"record {i} newlines")
                # Manifest identical; TRAD.lang and the other five
                # .loc tables byte-identical. (.class / .properties /
                # smali entries legitimately change -- the marker
                # translations apply to them too; those sources are
                # covered by tests/test_jar.py.)
                self.assertEqual(
                    zf.read("META-INF/MANIFEST.MF"),
                    zf_orig.read("META-INF/MANIFEST.MF"))
                self.assertEqual(zf.read("TRAD.lang"),
                                 zf_orig.read("TRAD.lang"))
                for name in orig_names:
                    if name.endswith(".loc") and name != "TRAD_english.loc":
                        self.assertEqual(zf.read(name), zf_orig.read(name), name)


if __name__ == "__main__":
    unittest.main()
