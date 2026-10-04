"""End-to-end tests for the ``main.py`` CLI against a real archive."""

from __future__ import annotations

import os
import zipfile

import pytest

from conftest import LATIN1_PROPERTIES, MANIFEST, SMALI_SOURCE, FakeProvider


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Run main.main() with the network backend swapped for the fake one."""
    import main as cli_module
    import translator

    monkeypatch.setattr(translator, "build_provider", lambda *a, **k: FakeProvider(*a[:2]))
    monkeypatch.chdir(tmp_path)

    def run(*args):
        return cli_module.main([
            *args,
            "--provider", "none",
            "--cache", str(tmp_path / "cache.json"),
            "--delay", "0",
            "--workspace", str(tmp_path / "ws"),
        ])

    return run


def test_end_to_end_patches_properties_and_smali(cli, sample_jar, tmp_path):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out) == 0

    with zipfile.ZipFile(out) as archive:
        props = archive.read("res/strings.properties").decode("iso-8859-1")
        smali = archive.read("com/game/Main.smali").decode("utf-8")
        assert archive.read("classes.dex") == bytes(range(256))
        # Stale signature files are stripped on rebuild (output is unsigned),
        # but MANIFEST.MF is kept so J2ME loaders recognise the game.
        assert "META-INF/MANIFEST.MF" in archive.namelist()
        assert "Main-Class" in archive.read("META-INF/MANIFEST.MF").decode()
        assert archive.testzip() is None

    assert "menu.start=Mulai Game" in props
    assert "menu.quit=Keluar" in props
    assert "# trailing comment" in props
    assert props.count("menu.start=") == 1
    assert "/data/user/0/com.game/files" in props   # path left alone
    assert "weird=100%" in props                    # non-word left alone

    assert 'const-string v0, "Mulai Game"' in smali
    assert 'const-string/jumbo v3, "Keluar"' in smali
    assert 'const-string v4, "com.game.internal.Class"' in smali


def test_reports_are_written(cli, sample_jar, tmp_path):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out) == 0

    found = (tmp_path / "found_strings.txt").read_text(encoding="utf-8")
    done = (tmp_path / "translated_strings.txt").read_text(encoding="utf-8")

    assert "Start Game" in found
    assert "res/strings.properties::menu.start" in found   # provenance
    assert "com/game/Main.smali::const-string" in found
    assert "Start Game => Mulai Game" in done
    assert "unchanged" not in found


def test_dry_run_writes_no_archive(cli, sample_jar, tmp_path):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out, "--dry-run") == 0
    assert not os.path.exists(out)


def test_refuses_to_overwrite_the_input(cli, sample_jar):
    assert cli(sample_jar, "-o", sample_jar) == 1


def test_missing_file_is_a_clean_error(cli, tmp_path):
    assert cli(str(tmp_path / "nope.jar")) == 1


def test_non_archive_input_is_a_clean_error(cli, tmp_path):
    bogus = tmp_path / "notazip.jar"
    bogus.write_bytes(b"just some bytes")
    assert cli(str(bogus)) == 1


def test_archive_without_strings_exits_two(cli, jar_factory, tmp_path):
    empty = jar_factory("empty.jar", {"a.txt": "nothing here\n"})
    assert cli(empty, "-o", str(tmp_path / "out.jar")) == 2


def test_total_provider_failure_keeps_the_archive_usable(cli, sample_jar, tmp_path, monkeypatch):
    import translator

    class Broken(FakeProvider):
        def translate(self, text):
            raise RuntimeError("down")

    monkeypatch.setattr(translator, "build_provider", lambda *a, **k: Broken())
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out) == 3

    # originals preserved, archive still valid
    with zipfile.ZipFile(out) as archive:
        assert archive.read("res/strings.properties").decode("iso-8859-1") == LATIN1_PROPERTIES
        assert archive.read("com/game/Main.smali").decode("utf-8") == SMALI_SOURCE


def test_second_run_is_fully_cached(cli, sample_jar, tmp_path, capsys):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out) == 0
    capsys.readouterr()
    assert cli(sample_jar, "-o", out) == 0
    assert "cached" in capsys.readouterr().out


def test_translate_all_includes_keys_and_paths(cli, sample_jar, tmp_path):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out, "--dry-run", "--translate-all") == 0
    found = (tmp_path / "found_strings.txt").read_text(encoding="utf-8")
    assert "/data/user/0/com.game/files" in found


def test_smali_can_be_ignored(cli, sample_jar, tmp_path):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out, "--no-smali") == 0
    with zipfile.ZipFile(out) as archive:
        assert archive.read("com/game/Main.smali").decode("utf-8") == SMALI_SOURCE
        assert "menu.start=Mulai Game" in archive.read("res/strings.properties").decode("iso-8859-1")


def test_workspace_is_cleaned_up(cli, sample_jar, tmp_path):
    out = str(tmp_path / "game_ID.jar")
    assert cli(sample_jar, "-o", out) == 0
    assert not os.path.exists(tmp_path / "ws")


def test_help_does_not_crash():
    import main as cli_module

    with pytest.raises(SystemExit) as exit_info:
        cli_module.main(["--help"])
    assert exit_info.value.code == 0
