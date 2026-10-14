"""STRING GUARD -- decides which strings must NOT be translated.

A mistranslated dimension or format specifier crashes the game at
runtime, so anything matching these patterns is skipped (kept verbatim).
"""
import re

# Screen dimensions: 1080x1920, 1920 x 1080, 720p, 1080P, 4K, 8k
_DIMENSION_RE = re.compile(
    r"\b\d{2,5}\s*[xX×]\s*\d{2,5}\b"      # 1080x1920
    r"|\b\d{3,4}\s*[pP]\b"                 # 720p / 1080P
    r"|\b[48][kK]\b"                       # 4K / 8K
)

# Format specifiers: %d %s %f %1$s %2$d, and {name} {0} {count}
_FORMAT_RE = re.compile(
    r"%(?:\d+\$)?[diuoxXfFeEgGaAcspn%]"    # printf-style
    r"|\{[a-zA-Z_][a-zA-Z0-9_]*\}"         # {name}
    r"|\{\d+\}"                            # {0}
)

# Pure numbers (int, float, with separators): 123, 1.5, 1,000
_NUMBER_RE = re.compile(r"^[\d\s.,_+\-xXa-fA-F]+$")

# URLs
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

# File paths: /sdcard/..., C:\..., something.apk, res/drawable
_PATH_RE = re.compile(
    r"^(?:/|\./|\.\./|[A-Za-z]:\\)"                   # absolute / dotted start
    r"|^(?:[\w.\-]+/)+[\w.\-]+$"                       # multi-segment relative path
    r"|\.(apk|dex|so|png|jpg|xml|json|db|ttf|ogg|mp3)$",  # known extensions
    re.IGNORECASE,
)

# Tag-only strings: nothing but markup tags, e.g. "<b></b>", "<xliff:g/>".
_TAGS_ONLY_RE = re.compile(r"^(?:\s*<[^>]+>\s*)+$")

# Version strings: v1.2.3, 1.0.0, 2.4.1-beta
_VERSION_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?([-_][\w.]+)?$", re.IGNORECASE)

# Single token with identifier chars (menu_start, com.game.Main, level-1)
# is a key / class name / format token, not UI text. A single
# *capitalised* word is usually a UI label ("Quit", "Pause") and passes.
_IDENT_CHARS_RE = re.compile(r"[_.$/\-:]")

# CJK exemption: a single CJK char is already meaningful ("剑").
_CJK_RE = re.compile(r"[\u2e80-\u9fff]")


def should_skip(text):
    """Return True if `text` must NOT be translated (keep verbatim).

    Returns a (skip: bool, reason: str) tuple.
    """
    if text is None:
        return True, "null"
    s = text.strip()
    if not s:
        return True, "empty"
    if _DIMENSION_RE.search(s):
        return True, "screen-dimension"
    if _URL_RE.search(s):
        return True, "url"
    if _VERSION_RE.match(s):
        return True, "version"
    if _PATH_RE.search(s):
        return True, "file-path"
    if _TAGS_ONLY_RE.match(s):
        return True, "tags-only"
    if _NUMBER_RE.match(s):
        return True, "number"
    # v1 heuristics (restored for the .jar flow: .properties values are
    # full of identifiers and bare tokens that must never reach a provider).
    if " " not in s:
        if _IDENT_CHARS_RE.search(s):
            return True, "identifier"
        if s.islower() and s.isalpha():
            return True, "lower-token"
    # No real word: only digits/punctuation, e.g. "100%" or "--".
    # Two or more letters in a row count as a word in any script.
    if not re.search(r"[^\W\d_]{2,}", s) and not _CJK_RE.search(s):
        return True, "no-word"
    return False, ""


def extract_placeholders(text):
    """Return the ordered list of format placeholders found in text."""
    return _FORMAT_RE.findall(text)
