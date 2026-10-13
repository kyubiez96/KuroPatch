# KuroPatch v2

Game localisation patcher — rebuilt from scratch. Python stdlib only, no
pip dependencies for the core engine.

## Layout

```
kuropatch-v2/
  core/        patching engine (stdlib only)
    apk.py       APK extract, binary AndroidManifest.xml parser, resource discovery
    strings.py   .properties / strings.xml / .stringpack parsers (round-trip safe)
    patcher.py   apply translations, repack, zipalign, sign
  cli/
    main.py      `kpatch` command line (extract/translate/build/patch)
  translate/     auto-translate engine (separate module, Agnes BYOK)
  tests/         unittest suite (31 tests)
```

## Usage

```bash
# unpack an APK, show manifest info + string files
python3 -m cli.main extract game.apk

# apply a translations JSON to a strings file
python3 -m cli.main translate res/values/strings.xml \
    --translations id.json -o res/values-id/strings.xml

# auto-translate with the translate engine (BYOK via AGNES_API_KEY)
AGNES_API_KEY=... python3 -m cli.main translate res/values/strings.xml \
    --auto --provider agnes --lang id

# repack an extracted tree
python3 -m cli.main build game.apk.extracted -o game-id.apk --sign

# full pipeline
python3 -m cli.main patch game.apk --lang id --translations id.json
```

## Design notes

- String values are opaque: `%d`, `%1$s`, `<b>` markup and escapes survive
  round-trips byte-identically (covered by tests).
- `strings.xml` arrays/plurals map to `name[i]` / `name{q}` keys and regroup
  on save. Non-translatable resources (colors, dimens) are never touched.
- Android translations go to `res/values-<lang>/` (proper localization),
  never overwriting the source `res/values/`.
- Binary `AndroidManifest.xml` is parsed without apktool; `resources.arsc`
  is stored uncompressed on repack.
- The patcher consumes `dict[str, str]`; providers/keys live in
  `translate/` and are never handled here.

## Tests

```bash
python3 -m unittest discover -s tests
```
