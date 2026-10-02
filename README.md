# KuroPatch

Automated localisation patcher for Java games. Scan, extract, translate and
rebuild — for `.jar` game resources and for full Android APKs.

## Install

```bash
pip install -r requirements.txt
```

## Usage

### JAR / APK resources

```bash
python main.py game.jar -o game_ID.jar --target id
```

This unpacks the archive, collects every `.properties` value and every smali
`const-string` literal, translates each distinct string once, writes the
translations back and repacks the archive.

Useful flags:

| Flag | Effect |
| --- | --- |
| `-s, --source` / `-t, --target` | language pair (default `en` → `id`) |
| `--provider` | `google-web` (default, no key), `google-v2`, `libretranslate`, `none` |
| `--api-key` | key for `google-v2` / `libretranslate` (or set `TRANSLATE_API_KEY`) |
| `--dry-run` | translate and report, write no archive |
| `--translate-all` | disable the skip heuristics (keys, paths and tokens too) |
| `--no-smali` | only patch `.properties` resources |
| `--workers`, `--delay` | parallelism and request spacing |
| `--cache`, `--no-cache` | translation cache control |

Exit codes: `0` success, `1` error, `2` nothing translatable found, `3` nothing
could be translated.

### Full APK pipeline

```bash
python apk.py game.apk -t id -o out/
```

`python apk.py` is the front end for real `.apk` files
(preflight → apktool decompile → extract → translate → patch → rebuild →
sign with v1+v2+v3 → verify). It needs a JDK 17 on `PATH`; `apktool` and the
standalone `uber-apk-signer` are downloaded into `--tools` automatically, so no
Android SDK is required.

The same flow is available as a library:

`core.GameTranslator.run_full_pipeline()` does the whole flow:
apktool decompile → extract → translate → patch → rebuild → zipalign → sign,
then verifies the result is a valid, v2-signed APK.

```python
from core import GameTranslator

engine = GameTranslator(output_dir="./out")
engine.target_lang = "id"
engine.setup_tools("./tools")          # or engine.download_tools("./tools")
result = engine.run_full_pipeline("game.apk")
print(result["success"], result["output_apk"])
```

It needs a JDK on `PATH` plus `apktool`. `apksigner` and `zipalign` are picked up
from `ANDROID_SDK_ROOT`/`ANDROID_HOME` `build-tools/*`, and
`download_tools()` fetches apktool and jadx if they are missing.

## Reports

Both entry points write reports you can inspect:

* `found_strings.txt` — every string found, with the file and key it came from
* `translated_strings.txt` — original → translation, with per-string status
* `translation_cache.json` — reusable cache; a second run translates nothing

## What is preserved

The patcher is written to be non-destructive:

* `.properties` comments, `=` vs `:` separators, whitespace, `#`/`!` trailing
  comments and `\` line continuations survive a round trip
* output is always safe for every Java version: pure-ASCII resources stay
  ASCII with `\uXXXX` escapes, ISO-8859-1 resources stay ISO-8859-1
* `%s`, `%1$s`, `{0}`, `$1`, HTML/XML tags, XML entities and `\n`/`\uXXXX`
  escapes are masked before translation and spliced back in place
* archive entry order, per-entry compression, timestamps and the position of
  `META-INF/MANIFEST.MF` are preserved; unmodified entries are copied
  byte-for-byte
* Android builds rewrite only the default `res/values/strings.xml` and add
  `res/values-<lang>/strings.xml`; existing translations in other
  `values-<other>/` folders are never overwritten
* inline markup (`<b>`, `<xliff:g>`) stays a tag, while text is XML-escaped
  exactly once

## Android app

The `app/` module is a thin Android shell. It is built and published by
GitHub Actions — nothing needs to be built locally:

* every push and pull request runs the Python test suite, then
  `assembleDebug assembleRelease`
* artifacts land in the workflow run
* pushing a `v*` tag publishes a GitHub Release with the signed APK

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests/ -q
```

The suite is fully offline: the translation backend is replaced by a
deterministic fake, so no test touches the network and none of them need a JDK
or the Android SDK.

## Layout

| File | Purpose |
| --- | --- |
| `main.py` | CLI for JAR/APK resource patching |
| `patcher.py` | archive handling, `.properties` codec, smali literals |
| `translator.py` | providers, placeholder masking, retries, cache |
| `core.py` | full APK pipeline (apktool, rebuild, sign) |
| `ui.py` | headless UI state machine for the Android shell |
| `config.json` | tool URLs, providers, keystore and signing settings |
