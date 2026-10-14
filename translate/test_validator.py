"""Tests for post-translation validation."""
import unittest

from validator import validate


class TestValidate(unittest.TestCase):
    def test_ok_no_placeholders(self):
        self.assertTrue(validate("Hello", "Halo"))

    def test_ok_placeholders_preserved(self):
        self.assertTrue(validate("You earned %d gold", "Kamu dapat %d emas"))
        self.assertTrue(
            validate("Hi {name}, you have {0} items",
                     "Hai {name}, kamu punya {0} barang"))

    def test_missing_placeholder(self):
        r = validate("You earned %d gold", "Kamu dapat emas")
        self.assertFalse(r)
        self.assertEqual(r.missing, ("%d",))

    def test_wrong_multiplicity(self):
        r = validate("%d and %d", "hanya %d")
        self.assertFalse(r)
        self.assertEqual(r.missing, ("%d",))

    def test_extra_placeholder(self):
        r = validate("Hello", "Halo %s")
        self.assertFalse(r)
        self.assertEqual(r.extra, ("%s",))

    def test_positional(self):
        self.assertTrue(validate("%1$s killed %2$d", "%1$s membunuh %2$d"))
        r = validate("%1$s killed %2$d", "%1$s membunuh")
        self.assertFalse(r)

    def test_swapped_order_rejected(self):
        # Same multiset, wrong order: killer/victim swapped. Must fail.
        r = validate("%1$s killed %2$d", "%2$d membunuh %1$s")
        self.assertFalse(r)
        r = validate("Give %d to %s", "Beri %s ke %d")
        self.assertFalse(r)


if __name__ == "__main__":
    unittest.main()
