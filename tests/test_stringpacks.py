"""Tests for Gameloft IGP string-pack support (RES_STRINGS* files).

Uses synthetic fixtures only — no copyrighted game data.
"""

import os
import sys
import tempfile
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "python"))

from stringpacks import (
    StringPack,
    build_resource,
    flatten,
    is_string_pack_entry,
    parse_resource,
)
from translator import protect_segments, restore_segments


def _sample_packs():
    return [
        StringPack(es=2, strings=[
            "QUIT",
            "\\mSir\\m\\fMa'am\\f, you alright?]Miami P.D.",
            "Find him at \\p2South Beach\\p1.]Talk \\v1now\\v1\\v600later\\v600.",
        ]),
        StringPack(es=3, strings=[
            "Hello \\x, welcome!",
            "Score: \\d points",
        ]),
    ]


def test_pack_name_matcher():
    assert is_string_pack_entry("RES_STRINGS")
    assert is_string_pack_entry("RES_STRINGS1")
    assert is_string_pack_entry("RES_STRINGS2")
    assert is_string_pack_entry("assets/RES_STRINGS12")
    assert not is_string_pack_entry("RES_STRINGS1.png")
    assert not is_string_pack_entry("RES_STRINGS.bak")
    assert not is_string_pack_entry("a.class")
    assert not is_string_pack_entry("MY_RES_STRINGS")


def test_round_trip_synthetic():
    packs = _sample_packs()
    raw = build_resource(packs)
    parsed = parse_resource(raw)
    assert [p.es for p in parsed] == [2, 3]
    assert flatten(parsed) == flatten(packs)


def test_round_trip_preserves_gameloft_codes():
    packs = _sample_packs()
    parsed = parse_resource(build_resource(packs))
    text = flatten(parsed)[1]
    assert "\\mSir\\m" in text
    assert "\\fMa'am\\f" in text
    assert "]" in text


def test_rebuild_with_longer_translations():
    packs = _sample_packs()
    packs[0].strings[0] = "KELUAR DARI PERMAINAN SEKARANG"  # much longer than "QUIT"
    packs[1].strings[0] = "Halo \\x, selamat datang di kota Miami yang cerah!"
    parsed = parse_resource(build_resource(packs))
    assert flatten(parsed)[0] == "KELUAR DARI PERMAINAN SEKARANG"
    assert "\\x" in flatten(parsed)[3]


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_resource(b"definitely not lzma")
    with pytest.raises(ValueError):
        parse_resource(b"\x00" * 100)


def test_gameloft_codes_protected():
    text = "\\mSir\\m\\fMa'am\\f, you alright?]Go to \\p2Downtown\\p1 \\v600now\\v600."
    masked, tokens = protect_segments(text)
    for code in ("\\m", "\\f", "\\p2", "\\p1", "\\v600", "]"):
        assert code in tokens, (code, tokens)
    # The words themselves stay translatable.
    assert "Sir" in masked
    assert restore_segments(masked, tokens) == text


def test_player_name_and_misc_codes_protected():
    text = "Hello \\x! You have \\d points.]Bye."
    masked, tokens = protect_segments(text)
    assert "\\x" in tokens and "\\d" in tokens and "]" in tokens
    assert restore_segments(masked, tokens) == text


def test_collect_and_patch_integration(tmp_path):
    """End-to-end: fake JAR with a RES_STRINGS file -> collect -> patch -> rebuild."""
    from main import collect_from_packs, patch_pack_files
    from patcher import JarPatcher

    jar_path = str(tmp_path / "game.jar")
    raw_pack = build_resource(_sample_packs())
    with zipfile.ZipFile(jar_path, "w") as zf:
        zf.writestr("RES_STRINGS1", raw_pack)
        zf.writestr("a.class", b"\xca\xfe\xba\xbe" + b"\x00" * 32)

    patcher = JarPatcher(jar_path, workspace=str(tmp_path / "ws"))
    try:
        patcher.extract()
        pack_files, collected = collect_from_packs(patcher, 3)
        assert len(pack_files) == 1
        assert len(collected.origins) == 5
        # origin labels point at the pack resource
        assert any("RES_STRINGS1::pack0#" in o
                   for origins in collected.origins.values() for o in origins)

        translations = {
            "QUIT": "KELUAR",
            "\\mSir\\m\\fMa'am\\f, you alright?]Miami P.D.": "\\mPak\\m\\fBu\\f, kamu baik-baik saja?]Miami P.D.",
        }
        replaced = patch_pack_files(pack_files, translations)
        assert replaced == 2

        # Re-read from the workspace file and verify the translations landed.
        with open(pack_files[0][0], "rb") as handle:
            reparsed = parse_resource(handle.read())
        flat = flatten(reparsed)
        assert "KELUAR" in flat
        assert any("baik-baik saja" in s for s in flat)
        # Untouched strings survive byte-identical in meaning.
        assert "Hello \\x, welcome!" in flat
    finally:
        patcher.close()


def test_build_resource_offset_overflow():
    pack = StringPack(es=1, strings=["x" * 300])  # 1-byte offsets max at 255
    with pytest.raises(ValueError):
        build_resource([pack])
