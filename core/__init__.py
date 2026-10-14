"""KuroPatch v2 -- patching engine core.

Submodules:
  jar         .jar pipeline: collect strings, apply translations, repack.
  patcher     Archive engine: JarPatcher, .properties, smali, .class strings.
  stringpacks Gameloft string-pack resources (LZMA string tables).

On-device target is .jar ONLY -- full APK patching needs apktool (real
JVM), which cannot run on Android. Translation providers live in the
sibling ``translate/`` package; the patcher consumes plain dicts.
"""

from . import jar, patcher, stringpacks

__all__ = ["jar", "patcher", "stringpacks"]
