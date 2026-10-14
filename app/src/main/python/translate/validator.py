"""Post-translation validation.

Every placeholder present in the source MUST survive in the
translation, in the same multiplicity. A single dropped %d is a
production crash -- failed strings are never shipped.
"""
from collections import Counter

try:
    from .guard import extract_placeholders
except ImportError:  # standalone script usage
    from guard import extract_placeholders


class ValidationResult:
    def __init__(self, ok, missing=(), extra=()):
        self.ok = ok
        self.missing = tuple(missing)
        self.extra = tuple(extra)

    def __bool__(self):
        return self.ok

    def __repr__(self):
        return (
            f"ValidationResult(ok={self.ok}, "
            f"missing={self.missing}, extra={self.extra})"
        )


def _edge_ws(s):
    """Return (leading_whitespace, trailing_whitespace) of ``s``."""
    return (
        s[:len(s) - len(s.lstrip())],
        s[len(s.rstrip()):],
    )


def validate(source, translation):
    """Check placeholder integrity of a translation.

    Placeholders must survive in the same ORDER as well as the same
    multiplicity: "%1$s killed %2$d" translated as "%2$d membunuh %1$s"
    has the right multiset but the wrong meaning -- the killer and the
    victim are swapped. Ordered comparison catches that.

    An empty / whitespace-only / non-string translation of a non-empty
    source is NEVER valid -- it used to pass (no placeholders in, none
    out) and ship as a blanked-out game string, and a None translation
    crashed here with TypeError. Reject it so the engine retries, then
    marks the string failed and keeps the source.

    Line breaks must match exactly (MED #4): line breaks are manual
    layout in game dialogue, and a translation that merges/splits lines
    used to ship unchecked.

    Leading/trailing whitespace must match exactly (HIGH #1): fragments
    are completed at runtime by concatenation ("5" + " detik tersisa"),
    so a stripped edge corrupts the rendered sentence. The engine
    re-applies the source's edge whitespace before calling here, so
    this is a safety net for direct callers (cache hits, patch-time
    checks) -- it must still fail closed.

    A real newline in the translation satisfies a literal ``\\n``
    escape in the source (LOW #9): providers emit real newlines for
    literal escapes (e.g. .properties "Line1\\nLine2" -> "Line1\nLine2").
    The translation is normalized (real newlines converted back to
    literal escapes, up to the number of literal escapes in the
    source) BEFORE the placeholder / line-break / whitespace checks,
    so a correct conversion passes instead of failing forever.
    """
    if not isinstance(source, str):
        source = ""
    if not isinstance(translation, str) or (
        not translation.strip() and source.strip()
    ):
        return ValidationResult(ok=False, missing=["<empty>"])
    eff = translation
    literal_n = source.count("\\n")
    if literal_n and "\n" in translation:
        eff = translation.replace("\n", "\\n", literal_n)
    if source.count("\n") != eff.count("\n"):
        return ValidationResult(ok=False, missing=["<line-breaks>"])
    if _edge_ws(source) != _edge_ws(eff):
        return ValidationResult(ok=False, missing=["<whitespace>"])
    src_list = extract_placeholders(source)
    dst_list = extract_placeholders(eff)
    if src_list == dst_list:
        return ValidationResult(ok=True)
    src = Counter(src_list)
    dst = Counter(dst_list)
    missing = []
    extra = []
    for ph, count in src.items():
        if dst.get(ph, 0) < count:
            missing.extend([ph] * (count - dst.get(ph, 0)))
    for ph, count in dst.items():
        if count > src.get(ph, 0):
            extra.extend([ph] * (count - src.get(ph, 0)))
    if not missing and not extra:
        # Same multiset, wrong order -- report as missing/extra pair
        # so the caller still gets a non-empty reason.
        missing = ["<order>"]
    return ValidationResult(ok=False, missing=missing, extra=extra)
