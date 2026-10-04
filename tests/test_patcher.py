"""Archive and .properties codec tests."""

from __future__ import annotations

import os
import zipfile

import pytest

from conftest import LATIN1_PROPERTIES, MANIFEST, SMALI_SOURCE
from patcher import (
    JarPatcher,
    PropertiesFile,
    escape_value,
    iter_smali_literals,
    patch_smali_literals,
    smali_escape,
    smali_unescape,
    unescape,
)


# ---------------------------------------------------------------------------
# Module level: importing patcher must not need deep-translator
# ---------------------------------------------------------------------------
def test_patcher_imports_without_optional_dependencies():
    """The original patcher.py imported GoogleTranslator but never used it,
    which made `import patcher` fail outright on a clean install."""
    import importlib
    import sys

    sys.modules.pop("deep_translator", None)
    module = importlib.import_module("patcher")
    assert hasattr(module, "JarPatcher")


# ---------------------------------------------------------------------------
# Properties codec
# ---------------------------------------------------------------------------
def test_parses_every_entry_of_a_realistic_file():
    props = PropertiesFile.loads(LATIN1_PROPERTIES.encode("iso-8859-1"))
    values = dict(props.items())
    assert values["menu.start"] == "Start Game"
    assert values["menu.quit"] == "Quit"
    assert values["menu.pause"] == "Pause"
    assert values["hud.score"] == "Score: %d"
    assert values["hud.slot"] == "Slot %1$s of %2$s"
    assert values["cafe"] == "Caf\u00e9"
    assert values["empty"] == ""


def test_comments_and_separators_survive_a_translation():
    props = PropertiesFile.loads(LATIN1_PROPERTIES.encode("iso-8859-1"))
    props.set("menu.start", "Mulai Game")
    out = props.to_bytes().decode("iso-8859-1")

    assert "menu.start=Mulai Game          # trailing comment" in out
    assert "menu.pause:\tJeda" in out.replace("Pause", "Jeda")
    assert "# Game strings" in out
    assert "! another comment" in out


def test_continuation_lines_are_collapsed_not_duplicated():
    props = PropertiesFile.loads(LATIN1_PROPERTIES.encode("iso-8859-1"))
    props.set("long", "Satu kalimat panjang.")
    out = props.to_bytes().decode("iso-8859-1")
    assert "long=Satu kalimat panjang.\n" in out
    # the old off-by-one wrote the translation onto the *following* key's line
    assert out.count("long=") == 1
    assert "menu.start" in out and "menu.quit" in out


def test_unchanged_file_is_returned_byte_identical():
    raw = LATIN1_PROPERTIES.encode("iso-8859-1")
    assert PropertiesFile.loads(raw).to_bytes() == raw


def test_pure_ascii_source_stays_ascii_so_java8_still_reads_it():
    raw = b"cafe=Caf\\u00e9\n"
    props = PropertiesFile.loads(raw)
    props.set("cafe", "\u041a\u043e\u0444\u0435")  # not representable in ASCII
    out = props.to_bytes()
    assert all(byte < 0x80 for byte in out)
    assert PropertiesFile.loads(out).get("cafe") == "\u041a\u043e\u0444\u0435"


def test_latin1_source_is_written_back_as_latin1():
    raw = "greeting=Hallo W\u00f6rld\n".encode("iso-8859-1")
    props = PropertiesFile.loads(raw)
    assert props.charset == "iso-8859-1"
    props.set("greeting", "Halo D\u00f6nia")
    out = props.to_bytes()
    assert out == "greeting=Halo D\u00f6nia\n".encode("iso-8859-1")


def test_utf8_source_is_written_back_as_utf8():
    raw = "greeting=Hallo W\u00f6rld\n".encode("utf-8")
    props = PropertiesFile.loads(raw)
    assert props.charset == "utf-8"
    props.set("greeting", "\u65e5\u672c\u8a9e")
    assert props.to_bytes() == "greeting=\u65e5\u672c\u8a9e\n".encode("utf-8")


def test_hash_and_equals_in_a_value_are_escaped():
    props = PropertiesFile.loads(b"k=v\n")
    props.set("k", "now # with hash")
    assert props.to_bytes() == b"k=now \\# with hash\n"


def test_leading_whitespace_in_a_value_is_escaped():
    assert escape_value("  padded") == "\\ \\ padded"


def test_unescape_handles_u_escapes_and_known_letters():
    assert unescape(r"Caf\u00e9\n") == "Caf\u00e9\n"
    assert unescape(r"a\\b") == "a\\b"


# ---------------------------------------------------------------------------
# Smali
# ---------------------------------------------------------------------------
def test_smali_literals_are_found_including_jumbo():
    decoded = [text for _s, _e, text in iter_smali_literals(SMALI_SOURCE)]
    assert "Start Game" in decoded
    assert "Welcome to the arena\n" in decoded
    assert "Quit" in decoded  # const-string/jumbo
    assert "com.game.internal.Class" in decoded


def test_smali_unicode_escapes_decode_to_the_same_entry():
    source = '    const-string v0, "Hello \\u0021"\n'
    assert [t for _s, _e, t in iter_smali_literals(source)] == ["Hello !"]


def test_patch_smali_replaces_only_mapped_literals():
    out, replaced, unmatched = patch_smali_literals(
        SMALI_SOURCE, {"Start Game": "Mulai Game", "Quit": "Keluar"}
    )
    assert replaced == 2
    assert unmatched == 0
    assert 'const-string v0, "Mulai Game"' in out
    assert 'const-string/jumbo v3, "Keluar"' in out
    assert 'const-string v2, "Level %d complete"' in out  # untouched
    assert 'const-string v4, "com.game.internal.Class"' in out


def test_smali_escape_round_trip():
    for text in ['say "hi"', "back\\slash", "line\nbreak", "tab\there", "\u00e9"]:
        assert smali_unescape(smali_escape(text)) == text


# ---------------------------------------------------------------------------
# Archive handling
# ---------------------------------------------------------------------------
def test_extract_then_rebuild_preserves_order_and_bytes(sample_jar, tmp_path):
    patcher = JarPatcher(sample_jar, workspace=str(tmp_path / "ws"))
    patcher.extract()
    out = patcher.rebuild(str(tmp_path / "out.jar"))
    patcher.close()

    with zipfile.ZipFile(sample_jar) as before, zipfile.ZipFile(out) as after:
        # Stale signature files are stripped on rebuild (the output is
        # unsigned); MANIFEST.MF is kept (sanitised) so J2ME loaders still
        # recognise the game. Everything else keeps order and bytes.
        stripped = {
            name
            for name in before.namelist()
            if name.upper().startswith("META-INF/")
            and name.upper().endswith((".SF", ".RSA", ".DSA", ".EC"))
        }
        assert [i.filename for i in after.infolist()] == [
            i.filename for i in before.infolist() if i.filename not in stripped
        ]
        assert after.read("classes.dex") == before.read("classes.dex")
        assert after.read("assets/data.bin") == before.read("assets/data.bin")
        assert "META-INF/MANIFEST.MF" in after.namelist()
        # the previous version stored everything uncompressed
        assert all(i.compress_type == zipfile.ZIP_DEFLATED for i in after.infolist())


def test_zip_slip_entry_is_rejected(tmp_path):
    evil = tmp_path / "evil.jar"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr("../escaped.properties", "pwned=1\n")

    patcher = JarPatcher(str(evil), workspace=str(tmp_path / "ws"))
    with pytest.raises(ValueError, match="unsafe"):
        patcher.extract()
    assert not (tmp_path / "escaped.properties").exists()


def test_output_inside_the_workspace_is_not_swallowed(tmp_path):
    jar = tmp_path / "g.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("a.properties", "k=Hello there\n")

    workspace = tmp_path / "ws"
    patcher = JarPatcher(str(jar), workspace=str(workspace))
    patcher.extract()
    props = patcher.load_properties(str(workspace / "a.properties"))
    props.set("k", "Halo")
    patcher.save_properties(props, str(workspace / "a.properties"))

    # writing the archive into the workspace must not include itself
    out = patcher.rebuild(str(workspace / "nested" / "out.jar"))
    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == ["a.properties"]
    patcher.close()


def test_stale_workspace_files_are_cleared_on_extract(tmp_path):
    jar = tmp_path / "g.jar"
    with zipfile.ZipFile(jar, "w") as archive:
        archive.writestr("a.properties", "k=Hello\n")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "leftover.properties").write_text("old=1\n")

    patcher = JarPatcher(str(jar), workspace=str(workspace))
    patcher.extract()
    assert not os.path.exists(workspace / "leftover.properties")
    assert os.path.exists(workspace / "a.properties")
    patcher.close()


def test_missing_archive_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        JarPatcher(str(tmp_path / "nope.jar"))


# ---------------------------------------------------------------------------
# .class constant-pool strings (J2ME games)
# ---------------------------------------------------------------------------
def _make_class(strings, extra_utf8=()):
    """Minimal class file with the given CONSTANT_String entries."""
    from patcher import _mutf8_encode

    pool = b""
    index = {}
    for s in list(strings) + list(extra_utf8):
        raw = _mutf8_encode(s)
        pool += b"\x01" + len(raw).to_bytes(2, "big") + raw
        index.setdefault(s, len(index) + 1)
    for s in strings:
        pool += b"\x08" + index[s].to_bytes(2, "big")
    count = len(index) + len(strings) + 1
    return b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + count.to_bytes(2, "big") + pool + b"\x00" * 16


def test_class_string_constants_are_collected():
    from patcher import iter_class_strings

    data = _make_class(["Start Game", "Quit"])
    assert sorted(v for _, v in iter_class_strings(data)) == ["Quit", "Start Game"]


def test_class_names_and_shared_entries_are_not_candidates():
    """A Utf8 that doubles as a method name must never be translated."""
    from patcher import _mutf8_encode, iter_class_strings

    pool = b""
    for s in ["Hello", "Start"]:
        raw = _mutf8_encode(s)
        pool += b"\x01" + len(raw).to_bytes(2, "big") + raw
    pool += b"\x08" + (1).to_bytes(2, "big")  # String -> "Hello"
    pool += b"\x08" + (2).to_bytes(2, "big")  # String -> "Start"
    pool += b"\x0c" + (2).to_bytes(2, "big") + (2).to_bytes(2, "big")  # NameAndType -> "Start"
    count = 6
    data = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + count.to_bytes(2, "big") + pool + b"\x00" * 16
    assert list(iter_class_strings(data)) == [(1, "Hello")]


def test_patch_class_strings_round_trip():
    from patcher import _parse_constant_pool, iter_class_strings, patch_class_strings

    data = _make_class(["Start Game"])
    new_data, replaced = patch_class_strings(data, {"Start Game": "Mulai Game"})
    assert replaced == 1
    assert list(iter_class_strings(new_data)) == [(1, "Mulai Game")]
    # The pool must still parse after the splice.
    utf8, _cands = _parse_constant_pool(new_data)
    assert utf8[1][1] == "Mulai Game"


def test_mutf8_round_trip():
    from patcher import _mutf8_decode, _mutf8_encode

    for s in ["Hello", "开始游戏", "a\x00b", "🎮", "café"]:
        assert _mutf8_decode(_mutf8_encode(s)) == s


def test_malformed_class_never_raises():
    from patcher import iter_class_strings, patch_class_strings

    assert list(iter_class_strings(b"not a class")) == []
    assert list(iter_class_strings(b"\xca\xfe\xba\xbe\x00")) == []
    assert patch_class_strings(b"\xca\xfe\xba\xbe", {"a": "b"}) == (b"\xca\xfe\xba\xbe", 0)


# ---------------------------------------------------------------------------
# MANIFEST.MF: kept (sanitised) so J2ME loaders accept the patched JAR
# ---------------------------------------------------------------------------
def test_manifest_kept_but_sanitized_on_rebuild(tmp_path):
    """Regression: an earlier build stripped MANIFEST.MF entirely, producing
    JARs that J2ME Loader rejected ("JAR not have META-INF/MANIFEST.MF").
    The manifest must survive with its MIDlet-* attributes, minus stale
    digest sections; .SF/.RSA signature files must still go."""
    from patcher import _sanitize_manifest

    raw = (
        "Manifest-Version: 1.0\r\n"
        "MIDlet-Name: Miami Nights 2\r\n"
        "MIDlet-Vendor: Gameloft\r\n"
        "MicroEdition-Profile: MIDP-2.0\r\n"
        "SHA-256-Digest-Manifest: abcdef\r\n"
        " wrapped-digest\r\n"
        "\r\n"
        "Name: com/game/Main.class\r\n"
        "SHA-256-Digest: deadbeef\r\n"
        "\r\n"
    ).encode()
    out = _sanitize_manifest(raw).decode()
    assert "MIDlet-Name: Miami Nights 2" in out
    assert "MicroEdition-Profile: MIDP-2.0" in out
    assert not any(line.startswith("Name:") for line in out.splitlines())
    assert "Digest" not in out

    src = tmp_path / "in.jar"
    with zipfile.ZipFile(src, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", raw)
        archive.writestr("META-INF/GAME.SF", b"sig")
        archive.writestr("META-INF/GAME.RSA", b"sig")
        archive.writestr("a/b.class", b"\xca\xfe\xba\xbe" + b"\x00" * 32)
    patcher = JarPatcher(str(src), workspace=str(tmp_path / "work"))
    patcher.extract()
    out_jar = str(tmp_path / "out.jar")
    patcher.rebuild(out_jar)
    with zipfile.ZipFile(out_jar) as archive:
        names = archive.namelist()
        assert "META-INF/MANIFEST.MF" in names
        assert not any(n.endswith((".SF", ".RSA")) for n in names)
        manifest = archive.read("META-INF/MANIFEST.MF").decode()
        assert "MIDlet-Name" in manifest
        assert "Digest" not in manifest
