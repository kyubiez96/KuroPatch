"""Tests for the CLI (cli/main.py) -- translations-file handling and
clean error reporting. No network, no API keys.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cli import main as cli_main


class TestLoadTranslationsJson(unittest.TestCase):
    def _write(self, tmp, obj):
        path = os.path.join(tmp, "t.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def test_list_values_rejected(self):
        # A `collect --out` file maps {value: [origins]}. Feeding it
        # back as --translations must FAIL, not coerce the origin
        # lists to "['...']" and patch game strings with garbage.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"Hello": ["a.jar::k"], "Bye": "Dadah"})
            with self.assertRaises(ValueError):
                cli_main._load_translations_json(path)

    def test_string_values_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"Hello": "Halo", "Bye": "Dadah"})
            self.assertEqual(
                cli_main._load_translations_json(path),
                {"Hello": "Halo", "Bye": "Dadah"})


class TestCliErrors(unittest.TestCase):
    def test_corrupt_jar_clean_error(self):
        # BadZipFile must surface as a clean "kpatch: error:" line
        # with exit code 1, not an uncaught traceback.
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "bad.jar")
            with open(bad, "wb") as fh:
                fh.write(b"not a zip at all")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli_main.main(["collect", bad])
            self.assertEqual(code, 1)
            self.assertIn("kpatch: error:", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_empty_jar_no_output(self):
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            jar = os.path.join(tmp, "empty.jar")
            with zipfile.ZipFile(jar, "w"):
                pass
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = cli_main.main(["patch", jar, "--provider", "passthrough"])
            self.assertEqual(code, 0)
            self.assertIn("nothing translatable", out.getvalue())


if __name__ == "__main__":
    unittest.main()
