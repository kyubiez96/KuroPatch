# KuroPatch v2

Game localisation patcher for Java games — on-device .jar patching.
Python stdlib only, no pip dependencies for the core engine.

**Target: `.jar` ONLY.** The Android app patches .jar game resources
(.properties + smali const-string + .class constants + Gameloft string
packs) on-device. Full APK patching needs apktool, which requires a real
JVM and cannot run on Android — the app refuses .apk files with an
explanation; use the PC CLI workflow for APKs.

## Layout

```
kuropatch-v2/
  core/        patching engine (stdlib only)
    jar.py       .jar pipeline: collect strings, apply translations, repack
    patcher.py   archive engine: JarPatcher, .properties, smali, .class
    stringpacks.py Gameloft string-pack resources (LZMA string tables)
  cli/
    main.py      `kpatch` command line (collect/translate/patch)
  translate/     auto-translate engine (separate module, Agnes BYOK)
  tests/         unittest suite
  app/           Android app (Material 3, Chaquopy, .jar only)
```

## Usage

```bash
# list translatable strings in a .jar
python3 -m cli.main collect game.jar

# apply a translations JSON ({source: translation}) to a .jar
python3 -m cli.main translate game.jar --translations id.json -o game_id.jar

# auto-translate with the translate engine (BYOK via AGNES_API_KEY)
AGNES_API_KEY=... python3 -m cli.main patch game.jar --provider agnes --target id
# -> game_id.jar
```

## Design notes

- Collection is permissive (cheap safety filters only); the full skip
  heuristics (screen dims, versions, URLs, paths, placeholders) live in
  `translate.guard` and are applied at translate time.
- Translations match by decoded source value across all four sources.
- Repack preserves entry order, compression, timestamps and MANIFEST.MF;
  stale .SF/.RSA signatures are stripped (output is unsigned by design).
- The patcher consumes `dict[str, str]`; providers/keys live in
  `translate/` and are never handled here.

## Tests

```bash
python3 -m unittest discover -s tests
python3 -m unittest discover -s translate -p "test_*.py"
```
