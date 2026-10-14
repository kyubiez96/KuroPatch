"""Auto-translate engine -- orchestrates guard, glossary, cache,
provider and validation for a batch of game strings.
"""
try:
    from . import guard as guard_mod
    from .cache import TranslationCache
    from .glossary import Glossary, lookup_fixed
    from .providers import PassthroughProvider
    from .validator import validate
except ImportError:  # standalone script usage
    import guard as guard_mod
    from cache import TranslationCache
    from glossary import Glossary, lookup_fixed
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

    CRITICAL: a real Chaquopy Java ``Set`` proxy is NOT Python-iterable
    -- ``for e in entry_set`` raises
    ``TypeError: 'LinkedHashMap$LinkedEntrySet' object is not iterable``.
    Traverse it ONLY via the explicit Java ``Iterator`` protocol:
    ``iterator()`` / ``hasNext()`` / ``next()`` -- plain method calls,
    no Python iteration protocol.
    """
    if isinstance(mapping, dict):
        return mapping
    try:
        entry_set = mapping.entrySet()
    except AttributeError:
        # Not a Java Map either -- let dict() raise a clear TypeError.
        return dict(mapping)
    result = {}
    try:
        it = entry_set.iterator()
    except AttributeError:
        # entrySet already Python-iterable (non-Chaquopy callers).
        for e in entry_set:
            result[str(e.getKey())] = str(e.getValue())
        return result
    while it.hasNext():
        e = it.next()
        result[str(e.getKey())] = str(e.getValue())
    return result


def translate_strings(strings, target_lang="id", source_lang="en",
                      provider=None, cache=None, glossary=None,
                      max_retries=1):
    """Translate {key: source_text} -> list of TranslateResult.

    Pipeline per string:
      1. guard.should_skip      -> status 'skipped' (kept verbatim)
      2. glossary.is_only_terms -> status 'skipped'
      3. glossary.lookup_language (target id only) -> fixed mapping,
         provider NEVER called (language names are never LLM-guessed)
      4. cache hit              -> status 'cached'
      5. provider.translate     -> validate placeholders
         - on validation failure: retry once with a stricter prompt,
           then mark 'failed' (source kept, never ship broken output)
    """
    provider = provider or PassthroughProvider()
    glossary = glossary or Glossary()
    if strings is None:
        return []
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
        # Language names: fixed mapping, never LLM-guessed (and never
        # shadowed by a stale/wrong cache entry). Indonesian target only.
        if target_lang == "id":
            lang_name = glossary.lookup_language(text)
            if lang_name is not None:
                results.append(TranslateResult(
                    key, text, lang_name, "translated", "glossary-language"))
                continue
        # Other hallucination-prone bare strings (day/month names, bare UI
        # labels): same treatment -- fixed mapping wins over cache/provider.
        fixed = lookup_fixed(text, target_lang)
        if fixed is not None:
            results.append(TranslateResult(
                key, text, fixed, "translated", "fixed-map"))
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
