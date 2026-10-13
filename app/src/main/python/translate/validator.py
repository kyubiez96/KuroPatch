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
    """Check placeholder integrity of a translation."""
    src = Counter(extract_placeholders(source))
    dst = Counter(extract_placeholders(translation))
    missing = []
    extra = []
    for ph, count in src.items():
        if dst.get(ph, 0) < count:
            missing.extend([ph] * (count - dst.get(ph, 0)))
    for ph, count in dst.items():
        if count > src.get(ph, 0):
            extra.extend([ph] * (count - src.get(ph, 0)))
    return ValidationResult(ok=not missing and not extra,
                            missing=missing, extra=extra)
