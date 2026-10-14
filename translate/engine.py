"""Auto-translate engine -- orchestrates guard, glossary, cache,
provider and validation for a batch of game strings.
"""
try:
    from . import guard as guard_mod
    from .cache import TranslationCache
    from .glossary import Glossary
    from .providers import PassthroughProvider
    from .validator import validate
except ImportError:  # standalone script usage
    import guard as guard_mod
    from cache import TranslationCache
    from glossary import Glossary
    from providers import PassthroughProvider
    from validator import validate


class TranslateResult:
    """Outcome for one input string."""

    def __init__(self, key, source, translation, status, detail=""):
        self.key = key
        self.source = source
        self.translation = translation
        self.status = status  # translated|skipped|cached|failed
        self.detail = detail

    def __repr__(self):
        return (f"TranslateResult(key={self.key!r}, status={self.status}, "
                f"detail={self.detail!r})")


def _coerce_str_dict(mapping):
    """Coerce a mapping to a real ``{str: str}`` dict.

    Chaquopy passes Kotlin/Java maps to Python as Java ``Map`` proxies
    (e.g. ``LinkedHashMap``), which have no ``.items()`` -- iterating
    them like a Python dict raises
    ``AttributeError: 'LinkedHashMap' object has no attribute 'items'``.
    Convert via ``entrySet()`` when needed. Plain dicts pass through.
    """
    if isinstance(mapping, dict):
        return mapping
    try:
        entry_set = mapping.entrySet()
    except AttributeError:
        # Not a Java Map either -- let dict() raise a clear TypeError.
        return dict(mapping)
    return {str(e.getKey()): str(e.getValue()) for e in entry_set}


def translate_strings(strings, target_lang="id", source_lang="en",
                      provider=None, cache=None, glossary=None,
                      max_retries=1):
    """Translate {key: source_text} -> list of TranslateResult.

    Pipeline per string:
      1. guard.should_skip      -> status 'skipped' (kept verbatim)
      2. glossary.is_only_terms -> status 'skipped'
      3. cache hit              -> status 'cached'
      4. provider.translate     -> validate placeholders
         - on validation failure: retry once with a stricter prompt,
           then mark 'failed' (source kept, never ship broken output)
    """
    provider = provider or PassthroughProvider()
    glossary = glossary or Glossary()
    strings = _coerce_str_dict(strings)

    results = []
    for key, text in strings.items():
        skip, reason = guard_mod.should_skip(text)
        if skip:
            results.append(TranslateResult(key, text, text, "skipped", reason))
            continue
        if glossary.is_only_terms(text):
            results.append(TranslateResult(key, text, text, "skipped", "glossary"))
            continue
        if cache is not None:
            hit = cache.get(text, source_lang, target_lang, provider.name)
            if hit is not None:
                results.append(TranslateResult(key, text, hit, "cached", ""))
                continue

        prompt_text = glossary.protect_hint(text)
        attempt = 0
        translated = None
        last_error = ""
        while attempt <= max_retries:
            try:
                translated = provider.translate(
                    prompt_text, source_lang, target_lang)
            except Exception as exc:  # provider blew up: fail safe
                last_error = str(exc)
                break
            check = validate(text, translated)
            if check:
                break
            last_error = f"placeholder mismatch: missing={check.missing}"
            translated = None
            attempt += 1
            # retry once with an even stricter instruction
            prompt_text = (
                "Translate ONLY. Do not explain. Copy every placeholder "
                f"character-for-character.\n{text}"
            )

        if translated is None:
            results.append(TranslateResult(
                key, text, text, "failed", last_error or "validation failed"))
            continue
        if cache is not None:
            cache.put(text, source_lang, target_lang, provider.name, translated)
        results.append(TranslateResult(key, text, translated, "translated", ""))

    if cache is not None:
        cache.save()
    return results


def results_to_dict(results):
    """Collapse results to {key: translation} (failed/skipped keep source)."""
    return {r.key: r.translation for r in results}
