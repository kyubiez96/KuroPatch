"""Shared pytest fixtures and fake translation providers.

No test in this suite touches the network or needs a JDK/Android SDK.
"""

from __future__ import annotations

import os
import sys
import zipfile
from typing import Dict, List

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A deterministic dictionary: provider input -> Indonesian output.
DICTIONARY: Dict[str, str] = {
    "start game": "Mulai Game",
    "quit": "Keluar",
    "pause": "Jeda",
    "load game": "Muat Game",
    "my game": "Permainanku",
    "score: %d": "Skor: %d",
    "no items": "tidak ada item",
    "welcome": "Selamat datang",
    "settings": "Pengaturan",
    "cafe": "Kafe",
    "story mode": "Mode Cerita",
}


class FakeProvider:
    """Deterministic stand-in for a real backend.

    ``fail_times`` makes the first N calls raise, which is how retry and
    error-handling behaviour is tested.
    """

    def __init__(self, source: str = "en", target: str = "id", fail_times: int = 0) -> None:
        self.source = source
        self.target = target
        self.fail_times = fail_times
        self.calls: List[str] = []
        self.closed = False

    def translate(self, text: str) -> str:
        self.calls.append(text)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("simulated provider failure")
        return DICTIONARY.get(text.strip().lower(), f"[{self.target}]{text}")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def engine(fake_provider, tmp_path):
    """A TranslatorEngine wired to the fake provider, with a temp cache."""
    from translator import TranslatorEngine

    instance = TranslatorEngine(
        cache_file=str(tmp_path / "cache.json"),
        provider=fake_provider,
        delay=0,
        max_retries=3,
    )
    yield instance
    instance.close()


@pytest.fixture
def jar_factory(tmp_path):
    """Build a JAR/APK-like archive from a dict of entries."""
    def _build(name: str, entries: Dict[str, bytes], manifest_first: bool = True) -> str:
        path = str(tmp_path / name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            keys = list(entries)
            if manifest_first and "META-INF/MANIFEST.MF" in keys:
                keys.remove("META-INF/MANIFEST.MF")
                keys.insert(0, "META-INF/MANIFEST.MF")
            for key in keys:
                archive.writestr(key, entries[key])
        return path

    return _build


LATIN1_PROPERTIES = (
    "# Game strings\n"
    "! another comment\n"
    "\n"
    "menu.start=Start Game          # trailing comment\n"
    "menu.quit=Quit\n"
    "menu.pause:\tPause\n"
    "hud.score=Score: %d\n"
    "hud.slot=Slot %1$s of %2$s\n"
    "cafe=Caf\\u00e9\n"
    "path=/data/user/0/com.game/files\n"
    "weird=100%\n"
    "long=This is a very long sentence that spans \\\n"
    "  two physical lines in the properties file.\n"
    "empty=\n"
)

SMALI_SOURCE = '''.class public Lcom/game/Main;
.method public static greet()V
    const-string v0, "Start Game"
    const-string v1, "Welcome to the arena\\n"
    const-string v2, "Level %d complete"
    const-string/jumbo v3, "Quit"
    const-string v4, "com.game.internal.Class"
    return-void
.end method
'''

MANIFEST = "Manifest-Version: 1.0\r\nMain-Class: com.game.Main\r\n\r\n"

ANDROID_STRINGS_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<resources xmlns:xliff="urn:oasis:names:tc:xliff:document:1.2">\n'
    "    <!-- a comment that must survive -->\n"
    '    <string name="app_name">My Game</string>\n'
    '    <string name="btn_start">Start Game</string>\n'
    '    <string name="welcome">Welcome, <b>%s</b>!</string>\n'
    '    <string name="amp">Tom &amp; Jerry say &lt;hi&gt;</string>\n'
    '    <string name="xliff">Hi <xliff:g id="name" example="Bob">%s</xliff:g>!</string>\n'
    '    <string name="empty"></string>\n'
    "</resources>\n"
)


@pytest.fixture
def sample_jar(jar_factory) -> str:
    """A small archive with a properties file, a smali file and binary payloads."""
    return jar_factory(
        "game.jar",
        {
            "META-INF/MANIFEST.MF": MANIFEST.encode("utf-8"),
            "res/strings.properties": LATIN1_PROPERTIES.encode("iso-8859-1"),
            "com/game/Main.smali": SMALI_SOURCE.encode("utf-8"),
            "classes.dex": bytes(range(256)),
            "assets/data.bin": b"\x00\x01\x02payload" * 100,
        },
    )


@pytest.fixture
def decompiled_tree(tmp_path):
    """A fake apktool output tree. Returns the GameTranslator holding it."""
    import core

    translator = core.GameTranslator(keep_workspace=True)
    root = translator.decompiled_dir

    for relative in ("res/values", "res/values-ru", "smali/com/game"):
        os.makedirs(os.path.join(root, relative), exist_ok=True)

    with open(os.path.join(root, "res/values/strings.xml"), "w", encoding="utf-8") as handle:
        handle.write(ANDROID_STRINGS_XML)

    with open(os.path.join(root, "res/values-ru/strings.xml"), "w", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="utf-8"?>\n<resources>\n'
            '    <string name="btn_start">\u041d\u0430\u0447\u0430\u0442\u044c</string>\n'
            "</resources>\n"
        )

    with open(os.path.join(root, "smali/com/game/Main.smali"), "w", encoding="utf-8") as handle:
        handle.write(SMALI_SOURCE.replace("Load Game", "Start Game"))

    yield translator
    translator.clean_workspace()
