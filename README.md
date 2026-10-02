# KuroPatch
Automated Java game localization patcher. Scan, extract, translate, and rebuild.

## Usage
1. Place your .jar file in the workspace.
2. Run `main.py` to start the scan-translate-rebuild pipeline.
3. Check `found_strings.txt` for extracted strings.

## Features
- **Auto-Scanner**: Uses `javap` to locate hardcoded strings.
- **Deep Translator**: Free, cache-backed Google Translate integration.
- **Auto-Release**: GitHub Actions automatically compiles and releases your patched APKs.

## Build
The repository is configured for automated APK compilation via GitHub Actions. Patched builds appear in the **Releases** tab.
