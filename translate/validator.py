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


def validate(source, translation):
    """Check placeholder integrity of a translation.

    Placeholders must survive in the same ORDER as well as the same
    multiplicity: "%1$s killed %2$d" translated as "%2$d membunuh %1$s"
    has the right multiset but the wrong meaning -- the killer and the
    victim are swapped. Ordered comparison catches that.
    """
    src_list = extract_placeholders(source)
    dst_list = extract_placeholders(translation)
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
