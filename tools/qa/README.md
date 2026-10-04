# tools/qa — KuroPatch QA gates

Seven report-only QA gates for the game-localization pipeline, stdlib Python only:

| Gate | Command | Checks |
|---|---|---|
| recon | `qa.py recon app.apk` | APK framework, native archs, string inventory |
| delta | `qa.py delta --old v1.xml --new v2.xml` | NEW/CHANGED/REMOVED strings between versions |
| glossary | `qa.py glossary --glossary g.tsv --strings id.xml` | Terminology consistency (Simpan, not Menyimpan) |
| typo | `qa.py typo --strings id.xml [--formal]` | Indonesian affix heuristics (heuristic, not a dictionary) |
| glyph | `qa.py glyph --font game.ttf --strings id.xml` | Every translated char has a font glyph |
| sample | `qa.py sample --strings id.xml -n 20 --seed 42` | Seeded, reproducible QA sampling |
| keystore | `qa.py keystore --apk app.apk --expect FP` | APK signed with the expected cert (SKIP without apksigner/keytool) |

Exit codes: 0 = pass, 1 = issues found, 2 = gate could not run.
All gates emit JSON on stdout and never modify inputs.

`fixtures/clean/` must pass every gate; `fixtures/dirty/` must trip them
(except keystore, which SKIPs without signing tools). `.github/workflows/qa.yml`
asserts exactly that on every push/PR.

The interactive Hermes skill version lives in `~/workspace/your_files/kuropatch-qa/`
(same `qa.py`). Keep both copies in sync.
