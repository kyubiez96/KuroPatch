"""KuroPatch v2 -- patching engine core.

Submodules:
  apk      APK extraction, binary AndroidManifest.xml parsing, resource discovery.
  strings  String resource parsers (.properties, strings.xml, stringpack).
  patcher  Apply translations, repack, zipalign and sign APKs.

All stdlib only. Translation providers live in the sibling
``translate/`` package; the patcher consumes a plain ``dict[str, str]``.
"""

from . import apk, patcher, strings

__all__ = ["apk", "patcher", "strings"]
