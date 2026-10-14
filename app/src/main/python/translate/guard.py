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

# Pure numbers (int, float, with separators): 123, 1.5, 1,000.
# The (?=.*\d) lookahead requires at least one digit: without it,
# real words made only of hex letters (DEAD, CAFE, FACE, BEEF, FEED)
# were misclassified as "numbers" and never translated.
_NUMBER_RE = re.compile(r"^(?=.*\d)[\d\s.,_+\-xXa-fA-F]+$")

# URLs
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)

# File paths: /sdcard/..., C:\..., something.apk, res/drawable.
# The relative-path alternative requires TWO or more slashes: a single
# "Word/Word" segment is far more often a UI label ("Save/Load",
# "Yes/No", "On/Off") than a path. Single-slash lowercase paths
# ("res/drawable") still get skipped -- by the identifier rule below.
_PATH_RE = re.compile(
    r"^(?:/|\./|\.\./|[A-Za-z]:\\)"                   # absolute / dotted start
    r"|^(?:[\w.\-]+/){2,}[\w.\-]+$"                    # multi-segment relative path (2+ slashes)
    r"|\.(apk|dex|so|png|jpg|xml|json|db|ttf|ogg|mp3)$",  # known extensions
    re.IGNORECASE,
)

# Tag-only strings: nothing but markup tags, e.g. "<b></b>", "<xliff:g/>".
_TAGS_ONLY_RE = re.compile(r"^(?:\s*<[^>]+>\s*)+$")

# ISO 639-1 (2-letter) + ISO 639-2 (3-letter, for mapped languages) codes.
# A bare code ("EN", "es", "ENG") is a language tag, never UI text.
# "no" is DELIBERATELY excluded: a bare "No" is far more often the
# Yes/No dialog answer than the Norwegian code -- skipping it would
# keep "No" untranslated in dialogs. Same for "hi" (greeting, far more
# common than the Hindi tag) and "yo" (slang greeting vs Yoruba tag).
_ISO_LANG_CODES = frozenset([
    # ISO 639-1 (2-letter), minus "no" (see above)
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
    "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
    "da", "de", "dv", "dz",
    "ee", "el", "en", "eo", "es", "et", "eu",
    "fa", "ff", "fi", "fj", "fo", "fr", "fy",
    "ga", "gd", "gl", "gn", "gu", "gv",
    "ha", "he", "ho", "hr", "ht", "hu", "hy", "hz",
    "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
    "ja", "jv",
    "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks",
    "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
    "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
    "na", "nb", "nd", "ne", "ng", "nl", "nn", "nr", "nv", "ny",
    "oc", "oj", "om", "or", "os",
    "pa", "pi", "pl", "ps", "pt",
    "qu",
    "rm", "rn", "ro", "ru", "rw",
    "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so",
    "sq", "sr", "ss", "st", "su", "sv", "sw",
    "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts",
    "tt", "ty",
    "ug", "uk", "ur", "uz",
    "ve", "vi", "vo",
    "wa", "wo",
    "xh", "yi",
    "za", "zh", "zu",
    # ISO 639-2 (3-letter) for mapped languages (B + T variants)
    "eng", "spa", "deu", "ger", "fra", "fre", "ita", "por", "nld", "dut",
    "rus", "jpn", "kor", "zho", "chi", "ara", "hin", "tha", "vie", "ind",
    "msa", "may", "swe", "fin", "nor", "dan", "ell", "gre", "heb", "ukr",
    "bul", "ron", "rum", "cat", "glg", "eus", "baq", "epo", "lat", "tgl",
    "pan", "ben", "tam", "tel", "mar", "guj", "kan", "mal", "nep", "sin",
    "mya", "bur", "khm", "lao", "mon", "kat", "geo", "hye", "arm", "aze",
    "kaz", "uzb", "tat", "slk", "slo", "slv", "hrv", "scr", "srp", "scc",
    "bos", "mkd", "mac", "sqi", "alb", "lav", "lit", "est", "isl", "ice",
    "gle", "cym", "wel", "mlt", "swa", "afr", "yor", "ibo", "hau", "amh",
    "som", "zul", "xho", "sna", "yid", "fas", "per", "pus", "kur", "urd",
    "snd", "asm", "ori", "san", "pli", "fil", "ces", "cze", "hun", "tur",
    "pol",
])

# Version strings: v1.2.3, 1.0.0, 2.4.1-beta
_VERSION_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?([-_][\w.]+)?$", re.IGNORECASE)

# Single token with identifier chars (menu_start, com.game.Main, level-1)
# is a key / class name / format token, not UI text. A single
# *capitalised* word is usually a UI label ("Quit", "Pause") and passes.
_IDENT_CHARS_RE = re.compile(r"[_.$/\-:]")

# "Capitalized/Capitalized" (Save/Load, Yes/No, On/Off) is a UI label
# pair, not a path or identifier -- it must reach the translator.
# Lowercase "a/b", "res/x" and multi-slash "a/b/c" stay identifiers.
_SLASH_LABEL_RE = re.compile(r"^[A-Z][a-z]+/[A-Z][a-z]+$")

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
    # Bare ISO language code ("EN", "es", "ENG") -- a language tag, never
    # UI text. Checked before the number rule: "DE" would otherwise be
    # misclassified as a hex "number".
    if s.isalpha() and len(s) in (2, 3) and s.lower() in _ISO_LANG_CODES:
        return True, "language-code"
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
        if _IDENT_CHARS_RE.search(s) and not _SLASH_LABEL_RE.match(s):
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
