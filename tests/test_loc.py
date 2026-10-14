"""Tests for the Gameloft TRAD_*.loc pipeline (core/loc.py).

No network, no API keys. Round-trip fixtures use Kovak's original
Castlevania jar (read-only) — the 6 shipped .loc files must parse and
rebuild byte-identical.
"""

import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import loc as loc_mod

JAR = os.path.expanduser(
    "~/workspace/user/files/"
    "castlevania_dawn_of_sorrow_320x240-276636_2_5uug.jar"
)
LOCS = (
    "TRAD_english.loc",
    "TRAD_french.loc",
    "TRAD_german.loc",
    "TRAD_italian.loc",
    "TRAD_portuguese.loc",
    "TRAD_spanish.loc",
)


def _read_loc(name):
    with zipfile.ZipFile(JAR) as z:
        return z.read(name)


def _english():
    return loc_mod.parse(_read_loc("TRAD_english.loc"))


@unittest.skipUnless(os.path.exists(JAR), "Kovak's Castlevania jar not present")
class TestLocParseBuild(unittest.TestCase):
    def test_round_trip_identity_all_six_files(self):
        # Strongest structural proof: parse + rebuild with NO changes
        # must reproduce the shipped bytes exactly.
        for name in LOCS:
            raw = _read_loc(name)
            self.assertEqual(loc_mod.build(loc_mod.parse(raw)), raw, name)

    def test_parse_spot_checks(self):
        strings = _english()
        self.assertEqual(len(strings), 400)
        self.assertEqual(strings[0], "0")
        self.assertEqual(strings[1], "English")
        self.assertTrue(strings[63].startswith("Ever since the solar eclipse"))
        self.assertIn("Soma, can I ask you something?", strings[256])

    def test_parse_rejects_bad_header(self):
        raw = bytearray(_read_loc("TRAD_english.loc"))
        raw[0:4] = (999).to_bytes(4, "big")  # skip != 1206
        with self.assertRaisesRegex(ValueError, "skip"):
            loc_mod.parse(bytes(raw))
        raw = bytearray(_read_loc("TRAD_english.loc"))
        raw[4:8] = (399).to_bytes(4, "big")  # count != 400
        with self.assertRaisesRegex(ValueError, "count"):
            loc_mod.parse(bytes(raw))
        raw = bytearray(_read_loc("TRAD_english.loc"))
        raw[8:12] = b"\x00\x00\x00\x00"  # header constant changed
        with self.assertRaisesRegex(ValueError, "header"):
            loc_mod.parse(bytes(raw))

    def test_parse_rejects_truncation_and_trailing(self):
        raw = _read_loc("TRAD_english.loc")
        with self.assertRaises(ValueError):
            loc_mod.parse(raw[:-1])
        with self.assertRaises(ValueError):
            loc_mod.parse(raw + b"\x00")

    def test_build_rejects_wrong_count(self):
        with self.assertRaisesRegex(ValueError, "exactly 400"):
            loc_mod.build(["x"] * 399)
        with self.assertRaisesRegex(ValueError, "exactly 400"):
            loc_mod.build(["x"] * 401)


@unittest.skipUnless(os.path.exists(JAR), "Kovak's Castlevania jar not present")
class TestLocModifiedUtf8(unittest.TestCase):
    def test_nul_round_trip(self):
        self.assertEqual(loc_mod.decode_modified_utf8(b"\xc0\x80"), "\x00")
        self.assertEqual(loc_mod.encode_modified_utf8("\x00"), b"\xc0\x80")

    def test_astral_round_trip(self):
        # U+1F600 as a Java surrogate pair, NOT a 4-byte sequence.
        self.assertEqual(
            loc_mod.encode_modified_utf8("\U0001f600"),
            b"\xed\xa0\xbd\xed\xb8\x80",
        )
        self.assertEqual(
            loc_mod.decode_modified_utf8(b"\xed\xa0\xbd\xed\xb8\x80"), "\U0001f600"
        )

    def test_latin1_round_trip(self):
        self.assertEqual(loc_mod.decode_modified_utf8(b"\xc3\xa9"), "é")
        self.assertEqual(loc_mod.encode_modified_utf8("é"), b"\xc3\xa9")

    def test_invalid_sequences_rejected(self):
        for bad in (
            b"\x00",  # literal NUL
            b"\xf0\x9f\x98\x80",  # 4-byte sequence
            b"\xed\xa0\xbd",  # lone high surrogate
            b"\xed\xb8\x80",  # lone low surrogate
            b"\xc0\x81",  # overlong
            b"\xc3",  # truncated
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                loc_mod.decode_modified_utf8(bad)


def _mutated(fn):
    strings = _english()
    fn(strings)
    return strings


@unittest.skipUnless(os.path.exists(JAR), "Kovak's Castlevania jar not present")
class TestLocValidate(unittest.TestCase):
    def test_identity_passes(self):
        strings = _english()
        loc_mod.validate(strings, _english())  # no raise

    def test_legit_translation_passes(self):
        # Story record 63: same \n count, no $N, ASCII-only.
        orig = _english()
        new = list(orig)
        new[63] = (
            "Sejak gerhana matahari tahun 2035, hidupku tidak pernah sama.\n"
            "Saat itulah aku sadar bahwa aku, Soma Cruz, adalah reinkarnasi Dracula...\n"
            "Di kastilnyalah aku mempelajari kebenaran...\n"
            "Bahwa aku memiliki kekuatan untuk merebut jiwa musuh-musuhku.\n"
            "Kekuatan jahat itu hampir menelanku, tapi teman-temanku menarikku kembali\n"
            "dari jurang.\n"
            "Sudah sekitar setahun berlalu sejak itu, dan kukira masalahku sudah berakhir..."
        )
        self.assertEqual(new[63].count("\n"), orig[63].count("\n"))
        loc_mod.validate(new, orig)

    def test_rejects_wrong_count(self):
        with self.assertRaisesRegex(ValueError, "399"):
            loc_mod.validate(["x"] * 399, _english())

    def test_rejects_g0_change(self):
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(_mutated(lambda s: s.__setitem__(0, "1")), _english())

    def test_rejects_title_change(self):
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(
                _mutated(lambda s: s.__setitem__(64, "Castlevania\nFajar Kesedihan")),
                _english(),
            )

    def test_rejects_language_name_change(self):
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(
                _mutated(lambda s: s.__setitem__(1, "Bahasa Inggris")), _english()
            )

    def test_rejects_empty_filled(self):
        # rec70 is both empty AND do-not-translate, so filling it
        # reports the DNT violation; test the pure empty rule on a
        # synthetic original with an empty non-DNT slot (index 100).
        orig = _english()
        new = list(orig)
        new[70] = "oops"
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(new, orig)
        orig2 = list(orig)
        orig2[100] = ""
        new2 = list(orig2)
        new2[100] = "oops"
        with self.assertRaisesRegex(ValueError, "empty"):
            loc_mod.validate(new2, orig2)

    def test_rejects_dollar_code_drop(self):
        # Find a record that actually carries $N codes (help screens).
        orig = _english()
        idx = next(
            i for i, s in enumerate(orig) if "$" in s and i not in loc_mod.DO_NOT_TRANSLATE
        )
        new = list(orig)
        new[idx] = new[idx].replace("$0", "").replace("$1", "").replace("$2", "")
        with self.assertRaisesRegex(ValueError, r"\$N"):
            loc_mod.validate(new, orig)

    def test_rejects_dollar_code_added(self):
        orig = _english()
        new = list(orig)
        new[63] = "$5 " + new[63]
        with self.assertRaisesRegex(ValueError, r"\$N"):
            loc_mod.validate(new, orig)

    def test_rejects_newline_change(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63].replace("\n", " ", 1)
        with self.assertRaisesRegex(ValueError, "newline"):
            loc_mod.validate(new, orig)

    def test_rejects_non_ascii(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + " café"
        with self.assertRaisesRegex(ValueError, "non-ASCII"):
            loc_mod.validate(new, orig)

    def test_rejects_unwrappable_token(self):
        # The renderer word-wraps into a char[256] buffer, so a single
        # whitespace-free token > 256 chars cannot be wrapped. (A
        # per-\n-line cap is WRONG: shipped PT story line is 399 chars.)
        orig = _english()
        new = list(orig)
        lines = ["pendek"] * 7  # same \n count (6) as the story record
        lines[0] = "x" * 257  # one unwrappable token
        new[63] = "\n".join(lines)
        with self.assertRaisesRegex(ValueError, "unwrappable token"):
            loc_mod.validate(new, orig)

    def test_accepts_long_wrappable_line(self):
        # Shipped PT story line is 399 chars and the game wraps it fine:
        # a long line of SHORT tokens must be accepted.
        orig = _english()
        new = list(orig)
        lines = ["pendek"] * 7  # same \n count (6)
        lines[0] = ("kata " * 60).strip()  # ~300-char line, short tokens
        new[63] = "\n".join(lines)
        loc_mod.validate(new, orig)  # must not raise

    def test_accepts_preserved_original_non_ascii(self):
        # rec68 credits keep person name "Aurélien Seguin" (non-ASCII,
        # ORIGINAL). Translating the role label while keeping the name
        # must be accepted — only NEW non-ASCII is rejected.
        orig = _english()
        new = list(orig)
        self.assertIn("Aur\u00e9lien", new[68])
        new[68] = new[68].replace("Producer", "Produser")
        loc_mod.validate(new, orig)  # must not raise

    def test_rejects_moved_dollar_code(self):
        # Same $N sequence, different position (off line-start) —
        # the renderer consumes codes while laying out lines.
        import re as _re
        orig = _english()
        idx = next(
            i for i, s in enumerate(orig)
            if len(set(loc_mod._dollar_codes(s))) >= 2
            and i not in loc_mod.DO_NOT_TRANSLATE)
        new = list(orig)
        new[idx] = _re.sub(r"\$\d", lambda m: "X" + m.group(0),
                           new[idx], count=1)
        with self.assertRaisesRegex(ValueError, "positions"):
            loc_mod.validate(new, orig)

    def test_rejects_nul_byte(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + "\x00"
        with self.assertRaisesRegex(ValueError, "NUL"):
            loc_mod.validate(new, orig)

    def test_rejects_oversize_record(self):
        orig = _english()
        new = list(orig)
        # 7 chunks of 12000 chars joined by 6 newlines: same \n count
        # as the original story record, but 84006 bytes > 65535 cap.
        # (Line-length violations also fire; the 65535 message must be present.)
        new[63] = "\n".join(["x" * 12000] * 7)
        with self.assertRaisesRegex(ValueError, "65535"):
            loc_mod.validate(new, orig)


class TestLocValidateControlChars(unittest.TestCase):
    """Luna audit LOW #10: validate() rejected NUL but not \\r / other
    C0 control chars -- a provider emitting \\r\\n passed validation
    (\\n count matches) and the renderer may draw \\r as garbage.
    Fabricated 400-record tables (no Castlevania jar needed)."""

    def _orig(self):
        return ["0"] + ["Record %d here" % i for i in range(1, 400)]

    def test_rejects_cr(self):
        orig = self._orig()
        new = list(orig)
        new[2] = "Hello\rworld"
        with self.assertRaisesRegex(ValueError, "control"):
            loc_mod.validate(new, orig)

    def test_rejects_bel(self):
        orig = self._orig()
        new = list(orig)
        new[2] = "Hello\x07world"
        with self.assertRaisesRegex(ValueError, "control"):
            loc_mod.validate(new, orig)

    def test_accepts_tab(self):
        orig = self._orig()
        new = list(orig)
        new[2] = "Hello\tworld"
        loc_mod.validate(new, orig)  # must not raise

    def test_accepts_matching_newlines(self):
        orig = self._orig()
        orig[2] = "Line1\nLine2"
        new = list(orig)
        new[2] = "Baris1\nBaris2"
        loc_mod.validate(new, orig)  # must not raise


if __name__ == "__main__":
    unittest.main()
