"""Auto-translate engine -- orchestrates guard, glossary, cache,
provider and validation for a batch of game strings.
"""
import time

try:
    from . import guard as guard_mod
    from .cache import TranslationCache
    from .glossary import Glossary, lookup_fixed
    from .providers import PassthroughProvider, TransientProviderError
    from .validator import validate
except ImportError:  # standalone script usage
    import guard as guard_mod
    from cache import TranslationCache
    from glossary import Glossary, lookup_fixed
    from providers import PassthroughProvider, TransientProviderError
    from validator import validate


# Consecutive provider exceptions (network/API down) that abort a
# batch instead of timing out once per string.
_MAX_CONSECUTIVE_PROVIDER_ERRORS = 3

# Batch recovery: when the abort threshold trips on a TRANSIENT
# error (rate-limit / 5xx), back off and retry the batch before
# mass-failing the rest. Bounded: a truly dead provider (bad key,
# no network -- PERMANENT errors) never triggers recovery and
# still fails fast. Backoffs are 10s then 30s.
_MAX_PROVIDER_RECOVERIES = 2
_RECOVERY_BACKOFFS = (10.0, 30.0)

# Test hook: tests patch translate.engine._sleep to avoid real waits.
_sleep = time.sleep


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
    # Consecutive provider EXCEPTIONS (network down, API down, bad
    # key) abort the batch: without this, a dead network means one
    # timeout per string (a 97-string jar = 97 timeouts). Only
    # exceptions count -- validation failures are per-string issues
    # and never trigger the abort; any successful provider call
    # (even one that then fails validation) resets the counter.
    # The abort only skips PROVIDER CALLS: skipped / glossary /
    # fixed-map / cache hits are still served normally.
    consecutive_provider_errors = 0
    provider_dead = False
    recoveries_used = 0
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
                # A cache hit is NOT automatically trustworthy: a
                # stale / hand-edited / older-provider entry can drop
                # placeholders, and shipping it unchecked would
                # defeat the validator entirely. An invalid hit is a
                # miss -- fall through and re-translate.
                if validate(text, hit):
                    results.append(TranslateResult(key, text, hit, "cached", ""))
                    continue

        prompt_text = glossary.protect_hint(text)
        attempt = 0
        translated = None
        last_error = ""
        if provider_dead:
            # Provider already proven dead this batch: don't call it
            # again, just mark this string failed. (Skipped /
            # glossary / fixed-map / cache hits above already returned.)
            last_error = (
                "provider unavailable -- batch aborted after "
                f"{_MAX_CONSECUTIVE_PROVIDER_ERRORS} consecutive "
                "provider errors")
            results.append(TranslateResult(
                key, text, text, "failed", last_error))
            continue
        while attempt <= max_retries:
            try:
                # Throttle: pace real provider calls so a 400-string
                # .loc batch doesn't trip the free-tier rate limit
                # with rapid-fire requests. Providers without a
                # throttle_seconds attribute (stubs, passthrough)
                # get no delay -- tests stay instant.
                delay = getattr(provider, "throttle_seconds", 0) or 0
                if delay > 0:
                    _sleep(delay)
                translated = provider.translate(
                    prompt_text, source_lang, target_lang)
                # Provider answered (even if validation then rejects
                # it): the infrastructure is up, reset the streak.
                consecutive_provider_errors = 0
            except Exception as exc:  # provider blew up: fail safe
                last_error = str(exc)
                consecutive_provider_errors += 1
                if (consecutive_provider_errors
                        >= _MAX_CONSECUTIVE_PROVIDER_ERRORS):
                    # TRANSIENT only (rate-limit / 5xx): back off and
                    # retry the batch instead of mass-failing the
                    # rest. v2.0.11 Castlevania: 3 consecutive 429s
                    # mass-failed 327 strings. Recovery retries THIS
                    # string (attempt not consumed); if the provider
                    # recovered, the batch continues normally.
                    # PERMANENT errors (bad key, no network, ...) get
                    # NO recovery -- a truly dead provider fails fast.
                    if (isinstance(exc, TransientProviderError)
                            and recoveries_used < _MAX_PROVIDER_RECOVERIES):
                        recoveries_used += 1
                        _sleep(_RECOVERY_BACKOFFS[recoveries_used - 1])
                        consecutive_provider_errors = 0
                        continue
                    provider_dead = True
                break
            check = validate(text, translated)
            if check:
                break
            last_error = f"placeholder mismatch: missing={check.missing}"
            translated = None
            attempt += 1
            # Retry once with an even stricter instruction -- keeping
            # the glossary keep-verbatim hint, which the old retry
            # prompt dropped (a retried string could then mistranslate
            # a brand term the first attempt had protected).
            prompt_text = (
                "Translate ONLY. Do not explain. Copy every placeholder "
                "character-for-character.\n"
                + glossary.protect_hint(text)
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


def results_summary(results):
    """Summarize a TranslateResult list -> plain dict for the UI.

    The UI MUST call this alongside :func:`results_to_dict`: the dict
    alone cannot distinguish "provider returned this verbatim" from
    "provider FAILED and the source was kept". Without this, a dead
    provider (bad key, no network) shows as "0 translated, N
    unchanged" with no error at all -- the silent-failure bug reported
    on-device for the Sims 3 jar (44 strings, 10 failed, 0 shown).
    """
    summary = {
        "total": 0,
        "translated": 0,
        "skipped": 0,
        "cached": 0,
        "failed": 0,
        "changed": 0,
        "first_error": "",
    }
    for r in results:
        summary["total"] += 1
        if r.status in ("translated", "skipped", "cached", "failed"):
            summary[r.status] += 1
        if r.translation != r.source:
            summary["changed"] += 1
        if r.status == "failed" and not summary["first_error"]:
            summary["first_error"] = r.detail or "translation failed"
    return summary
