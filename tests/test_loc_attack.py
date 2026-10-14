"""Adversarial tests for core/loc.py (Haiku, verifier role).

Every test here is an ATTACK: a mutated translation that MUST be
rejected by loc.validate(), or a legitimate translation that MUST be
accepted (false positives are bugs too). Each attack was first proven
against a failing implementation (or a vacuous variant of itself) —
a test that cannot fail is not a test.

Run: python3 -m unittest test_loc_attack -v
"""

import os
import re
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import loc as loc_mod  # noqa: E402

JAR = os.path.expanduser(
    "~/workspace/user/files/"
    "castlevania_dawn_of_sorrow_320x240-276636_2_5uug.jar"
)


def _english():
    with zipfile.ZipFile(JAR) as z:
        return loc_mod.parse(z.read("TRAD_english.loc"))


def _dollar_idx(orig):
    return next(
        i for i, s in enumerate(orig)
        if len(set(loc_mod._dollar_codes(s))) >= 2
        and i not in loc_mod.DO_NOT_TRANSLATE)


@unittest.skipUnless(os.path.exists(JAR), "Kovak's Castlevania jar not present")
class TestLocAttacks(unittest.TestCase):
    def test_rejects_dropped_dollar_code(self):
        orig = _english()
        new = list(orig)
        i = _dollar_idx(orig)
        new[i] = re.sub(r"\$\d", "", new[i], count=1)
        with self.assertRaisesRegex(ValueError, r"\$N"):
            loc_mod.validate(new, orig)

    def test_rejects_added_dollar_code(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + " $9"
        with self.assertRaisesRegex(ValueError, r"\$N"):
            loc_mod.validate(new, orig)

    def test_rejects_reordered_dollar_codes(self):
        orig = _english()
        new = list(orig)
        i = _dollar_idx(orig)
        codes = list(re.finditer(r"\$\d", new[i]))
        a, b = codes[0], codes[1]
        s = new[i]
        new[i] = (s[:a.start()] + b.group(0) + s[a.end():b.start()]
                  + a.group(0) + s[b.end():])
        with self.assertRaisesRegex(ValueError, r"\$N"):
            loc_mod.validate(new, orig)

    def test_rejects_moved_dollar_code(self):
        # Same sequence, different position (off line-start).
        orig = _english()
        new = list(orig)
        i = _dollar_idx(orig)
        new[i] = re.sub(r"\$\d", lambda m: "X" + m.group(0),
                        new[i], count=1)
        with self.assertRaisesRegex(ValueError, "positions"):
            loc_mod.validate(new, orig)

    def test_rejects_newline_added(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + "\n"
        with self.assertRaisesRegex(ValueError, "newline"):
            loc_mod.validate(new, orig)

    def test_rejects_newline_removed(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63].replace("\n", " ", 1)
        with self.assertRaisesRegex(ValueError, "newline"):
            loc_mod.validate(new, orig)

    def test_rejects_new_non_ascii(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + "\u201c"  # curly quote, not in original
        with self.assertRaisesRegex(ValueError, "non-ASCII"):
            loc_mod.validate(new, orig)

    def test_rejects_nul_byte(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + "\x00"
        with self.assertRaisesRegex(ValueError, "NUL"):
            loc_mod.validate(new, orig)

    def test_rejects_lone_surrogate(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + "\ud800"
        with self.assertRaisesRegex(ValueError, "modified UTF-8"):
            loc_mod.validate(new, orig)

    def test_rejects_oversize_record(self):
        orig = _english()
        new = list(orig)
        # 7 lines (6 \n preserved), short tokens, ~70000 bytes total.
        new[63] = "\n".join("kata " * 2000 for _ in range(7))
        with self.assertRaisesRegex(ValueError, "65535"):
            loc_mod.validate(new, orig)

    def test_rejects_over_sanity_cap(self):
        orig = _english()
        new = list(orig)
        # 7 lines (6 \n preserved), short tokens, ~4200 bytes total.
        new[63] = "\n".join("kata " * 120 for _ in range(7))
        with self.assertRaisesRegex(ValueError, "sanity cap"):
            loc_mod.validate(new, orig)

    def test_rejects_unwrappable_token(self):
        orig = _english()
        new = list(orig)
        new[63] = new[63] + " " + "X" * 300
        with self.assertRaisesRegex(ValueError, "unwrappable token"):
            loc_mod.validate(new, orig)

    def test_rejects_changed_dnt_title(self):
        orig = _english()
        new = list(orig)
        new[64] = "Castlevania\nFajar Kesedihan"
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(new, orig)

    def test_rejects_changed_dnt_name(self):
        orig = _english()
        new = list(orig)
        new[159] = "Soma Kurusu"
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(new, orig)

    def test_rejects_filled_empty_slot(self):
        orig = _english()
        new = list(orig)
        new[70] = "something"
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(new, orig)

    def test_rejects_changed_g0(self):
        orig = _english()
        new = list(orig)
        new[0] = "1"
        with self.assertRaisesRegex(ValueError, "do-not-translate"):
            loc_mod.validate(new, orig)

    def test_rejects_count_401(self):
        orig = _english()
        new = list(orig) + ["extra"]
        with self.assertRaisesRegex(ValueError, "400"):
            loc_mod.validate(new, orig)

    def test_rejects_count_399(self):
        orig = _english()
        new = list(orig)
        del new[10]
        with self.assertRaisesRegex(ValueError, "400"):
            loc_mod.validate(new, orig)

    def test_accepts_long_wrappable_line(self):
        # Shipped PT story line is 399 chars; the game wraps it fine.
        orig = _english()
        new = list(orig)
        lines = ["pendek"] * 7  # same \n count (6)
        lines[0] = ("kata " * 60).strip()
        new[63] = "\n".join(lines)
        loc_mod.validate(new, orig)  # must not raise

    def test_accepts_preserved_original_non_ascii(self):
        # rec68 credits keep "Aurélien Seguin" (non-ASCII, ORIGINAL).
        orig = _english()
        new = list(orig)
        self.assertIn("Aur\u00e9lien", new[68])
        new[68] = new[68].replace("Producer", "Produser")
        loc_mod.validate(new, orig)  # must not raise

    def test_accepts_valid_story_translation(self):
        orig = _english()
        new = list(orig)
        new[63] = (
            "Sejak gerhana matahari tahun 2035, hidupku tidak pernah sama lagi.\n"
            "Saat itulah aku sadar bahwa aku, Soma Cruz, adalah reinkarnasi Dracula...\n"
            "Di kastilnyalah aku mengetahui kebenaran itu...\n"
            "Bahwa aku memiliki kekuatan untuk merebut jiwa musuh-musuhku.\n"
            "Kekuatan jahat itu hampir menelanku, tapi teman-temanku menarikku kembali dari jurang.\n"
            "Sekitar setahun telah berlalu sejak itu, dan aku kira masalahku sudah berakhir...\n"
            "Tapi ternyata belum."
        )
        loc_mod.validate(new, orig)  # must not raise


if __name__ == "__main__":
    unittest.main()
