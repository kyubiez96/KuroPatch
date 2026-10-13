"""Do-not-translate glossary -- brand/game terms that stay verbatim.

Terms are matched case-insensitively as whole words. A string whose
*entire* content is glossary terms is skipped; otherwise the terms are
protected so the provider prompt keeps them intact.
"""
import re

DEFAULT_TERMS = [
    "KuroPatch",
    "APK",
    "Android",
    "Google Play",
]


class Glossary:
    def __init__(self, terms=None):
        self.terms = list(terms) if terms else list(DEFAULT_TERMS)
        self._rebuild()

    def _rebuild(self):
        escaped = [re.escape(t) for t in self.terms if t.strip()]
        self._word_re = (
            re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)
            if escaped else None
        )

    def add(self, term):
        term = term.strip()
        if term and term not in self.terms:
            self.terms.append(term)
            self._rebuild()

    def remove(self, term):
        if term in self.terms:
            self.terms.remove(term)
            self._rebuild()

    def is_only_terms(self, text):
        """True if text contains nothing but glossary terms/whitespace."""
        if not self._word_re:
            return False
        stripped = self._word_re.sub("", text).strip()
        return not stripped

    def protect_hint(self, text):
        """Return text with a keep-verbatim note for the provider prompt."""
        found = sorted({m.group(0) for m in self._word_re.finditer(text)}) \
            if self._word_re else []
        if not found:
            return text
        keep = ", ".join(f'"{t}"' for t in found)
        return (
            f"{text}\n"
            f"[Keep these terms EXACTLY as written, do not translate: {keep}]"
        )
