"""KuroPatch v2 -- auto-translate engine."""
from .engine import translate_strings, results_to_dict, results_summary, TranslateResult
from .glossary import Glossary
from .guard import should_skip, extract_placeholders
from .providers import AgnesProvider, BaseProvider, PassthroughProvider
from .validator import validate

__all__ = [
    "translate_strings",
    "results_to_dict",
    "results_summary",
    "TranslateResult",
    "Glossary",
    "should_skip",
    "extract_placeholders",
    "AgnesProvider",
    "BaseProvider",
    "PassthroughProvider",
    "validate",
]
