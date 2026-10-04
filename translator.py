"""Translation engines for KuroPatch.

The original version of this file had three problems worth calling out:

* ``except:`` swallowed ``KeyboardInterrupt`` and every other real error, so
  failures were invisible and always looked like "translation unavailable".
* The cache was rewritten to disk once per string — O(n^2) IO on a real game.
* Translated text was handed to the backend verbatim, so ``%s``, ``{0}``,
  ``%1$s``, ``\\n`` and HTML tags got shuffled around or dropped, producing
  crashes in the patched game instead of text.

Everything below is provider-based, retrying, cache-backed and
placeholder-safe. Providers are plain objects with a ``translate(text)`` method,
which also makes the whole thing testable without network access.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Pattern, Tuple
__all__ = [
    "TranslationResult",
    "TranslatorEngine",
    "BaseProvider",
    "GoogleWebProvider",
    "GoogleV2Provider",
    "LibreTranslateProvider",
    "PassthroughProvider",
    "build_provider",
    "should_translate",
    "protect_segments",
    "restore_segments",
]

DEFAULT_SOURCE = "en"
DEFAULT_TARGET = "id"


# ---------------------------------------------------------------------------
# Placeholder protection
# ---------------------------------------------------------------------------
# Order matters: the most specific patterns come first so that e.g. ``%1$s`` is
# not eaten by the plain ``%s`` rule.
_PROTECT_PATTERNS: List[Pattern[str]] = [
    re.compile(r"[\n\r\t]"),                    # real control characters
    re.compile(r"\\u[0-9a-fA-F]{4}"),            # \uXXXX escapes
    re.compile(r"\\[nrtfb\\]"),                  # \n \t \r \f \b \\
    re.compile(r"%(?:\d+\$)?[-#+ 0,(]*\d*(?:\.\d+)?[a-zA-Z%]"),  # printf / %1$s
    re.compile(r"\$\{[^}]*\}"),                  # ${var}
    re.compile(r"\{[^{}]*\}"),                   # {0} {name}
    re.compile(r"</?[A-Za-z][^<>]*>"),            # html tags
    re.compile(r"&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);"),  # xml entities
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # ip addresses
]

_PLACEHOLDER = "\u0000{}\u0000"
_MASK_RE = re.compile("\u0000(\\d+)\u0000")


def protect_segments(text: str) -> Tuple[str, List[str]]:
    """Split ``text`` into translatable chunks plus a list of protected tokens.

    Returns ``(masked, tokens)`` where ``masked`` contains ``\\x0000N\\x0000``
    placeholders that no translation backend will touch.
    """
    tokens: List[str] = []
    masked = text

    def stash(match: re.Match) -> str:
        tokens.append(match.group(0))
        return _PLACEHOLDER.format(len(tokens) - 1)

    for pattern in _PROTECT_PATTERNS:
        masked = pattern.sub(stash, masked)

    return masked, tokens


def restore_segments(text: str, tokens: List[str]) -> str:
    """Inverse of :func:`protect_segments`."""
    def unstash(match: re.Match) -> str:
        index = int(match.group(1))
        if 0 <= index < len(tokens):
            return tokens[index]
        return match.group(0)

    return _MASK_RE.sub(unstash, text)


# ---------------------------------------------------------------------------
# Should this string be translated at all?
# ---------------------------------------------------------------------------
_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|www\.)", re.IGNORECASE)
_PATH_RE = re.compile(r"^(?:[/~]|\.{1,2}[/\\]|[A-Za-z]:[\\/])")
_IDENT_CHARS_RE = re.compile(r"[_.$/\-:]")
# Any Unicode letter, not just Latin: a Chinese/Japanese/Korean/Arabic game
# string is translatable, and the old [A-Za-z] check silently dropped every
# non-Latin source language ("no-latin").
_HAS_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
# CJK scripts where a single character is already a full morpheme ("剑", "盾").
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"  # Han
    r"\u3040-\u309f\u30a0-\u30ff"  # Hiragana + Katakana
    r"\uac00-\ud7af]",  # Hangul syllables
)
# Standalone tokens that are essentially never in-game UI text — they leak
# into the constant pool as property keys, RMS names or debug labels, and
# translating them can *break* the game (a getAppProperty("width") lookup
# fails when the key becomes "lebar"). Never translated, even with
# translate_all on.
_TECHNICAL_TOKENS = frozenset({
    "width", "height", "depth",
    "true", "false", "null", "nil",
    "utf-8", "utf8", "utf_8", "ascii", "unicode",
})
# Resource names: res/drawable-hdpi/icon.png, assets/levels/one.tmx, ...
_ASSET_RE = re.compile(r"^(?:res|assets|lib|META-INF)/", re.IGNORECASE)


def should_translate(text: str, target_lang: str = DEFAULT_TARGET, translate_all: bool = False) -> Tuple[bool, str]:
    """Cheap heuristics that keep junk out of the translation queue.

    Returns ``(ok, reason)``. ``reason`` is only meaningful when ``ok`` is False
    and is surfaced in the report so the user can see what was skipped.

    ``translate_all`` disables every heuristic except the empty/too-short/
    technical-token ones; it is what ``--translate-all`` on the CLI maps to.
    """
    stripped = text.strip()
    if not stripped:
        return False, "empty"
    # Technical tokens are never UI, in any language — and translating a
    # property key breaks the lookup that uses it.
    if stripped.lower() in _TECHNICAL_TOKENS:
        return False, "technical-token"
    # A single CJK character is already meaningful ("剑"); a single Latin
    # character ("a", "x") is almost always junk.
    if len(stripped) < 2 and not _CJK_RE.search(stripped):
        return False, "too-short"
    if not _HAS_LETTER_RE.search(stripped):
        return False, "no-letters"
    if translate_all:
        return True, ""

    if _URL_RE.match(stripped):
        return False, "url"
    if _PATH_RE.match(stripped) or _ASSET_RE.match(stripped):
        return False, "path"

    # A single token with no spaces is a key, a class name or a format token
    # ("menu_start", "com.game.Main", "level-1"). A single *capitalised* word
    # is usually a UI label ("Quit", "Pause"), so it must still be translated.
    if " " not in stripped:
        if _IDENT_CHARS_RE.search(stripped):
            return False, "identifier"
        if stripped.islower() and stripped.isalpha():
            return False, "lower-token"

    # A value that is only digits/punctuation, e.g. "100%" or "--".
    # Two or more letters in a row count as a word in any script; a single
    # CJK character already passed the too-short check above.
    if not re.search(r"[^\W\d_]{2,}", stripped) and not _CJK_RE.search(stripped):
        return False, "no-word"
    return True, ""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class BaseProvider:
    """Interface every translation backend implements."""

    name = "base"

    def __init__(self, source: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET) -> None:
        self.source = source
        self.target = target

    def translate(self, text: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        """Release provider resources."""


class PassthroughProvider(BaseProvider):
    """Identity provider — used by tests and ``--dry-run``."""

    name = "passthrough"

    def translate(self, text: str) -> str:
        return text


class GoogleWebProvider(BaseProvider):
    """Free, no-key backend built on ``deep_translator``.

    Raises on transport errors so :class:`TranslatorEngine` can retry.
    """

    name = "google-web"

    def __init__(self, source: str = DEFAULT_SOURCE, target: str = DEFAULT_TARGET) -> None:
        super().__init__(source, target)
        try:
            from deep_translator import GoogleTranslator  # noqa: WPS433 (optional dep)
        except ImportError as exc:  # pragma: no cover - depends on env
            raise RuntimeError(
                "deep-translator is not installed; run `pip install -r requirements.txt` "
                "or choose another --provider"
            ) from exc
        self._backend = GoogleTranslator(source=source, target=target)

    def translate(self, text: str) -> str:
        result = self._backend.translate(text)
        if result is None:
            raise RuntimeError("google-web returned no result")
        return str(result)


class GoogleV2Provider(BaseProvider):
    """Official Google Cloud Translation v2 (requires an API key)."""

    name = "google-v2"
    endpoint = "https://translation.googleapis.com/language/translate/v2"

    def __init__(self, source: str, target: str, api_key: str, timeout: int = 20) -> None:
        super().__init__(source, target)
        if not api_key:
            raise RuntimeError("google-v2 provider requires TRANSLATE_API_KEY")
        self.api_key = api_key
        self.timeout = timeout

    def translate(self, text: str) -> str:
        payload = urllib.parse.urlencode(
            {"q": text, "source": self.source, "target": self.target, "format": "text", "key": self.api_key}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        candidates = body.get("data", {}).get("translations")
        if not candidates:
            raise RuntimeError("google-v2 returned no translations")
        return str(candidates[0].get("translatedText", ""))


class LibreTranslateProvider(BaseProvider):
    """LibreTranslate — free when self-hosted, API key optional otherwise."""

    name = "libretranslate"
    default_endpoint = "https://libretranslate.com/translate"

    def __init__(
        self,
        source: str,
        target: str,
        endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: int = 20,
    ) -> None:
        super().__init__(source, target)
        self.endpoint = endpoint or self.default_endpoint
        self.api_key = api_key
        self.timeout = timeout

    def translate(self, text: str) -> str:
        body: Dict[str, str] = {"q": text, "source": self.source, "target": self.target, "format": "text"}
        if self.api_key:
            body["api_key"] = self.api_key
        request = urllib.request.Request(
            self.endpoint,
            data=urllib.parse.urlencode(body).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload.get("translatedText")
        if not result:
            raise RuntimeError("libretranslate returned no translatedText")
        return str(result)


def build_provider(
    name: str = "google-web",
    source: str = DEFAULT_SOURCE,
    target: str = DEFAULT_TARGET,
    api_key: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> BaseProvider:
    """Instantiate a provider by name."""
    key = api_key if api_key is not None else os.getenv("TRANSLATE_API_KEY", "")
    name = (name or "google-web").lower()
    if name in ("google-web", "google", "free"):
        return GoogleWebProvider(source, target)
    if name in ("google-v2", "google-cloud", "paid"):
        return GoogleV2Provider(source, target, key)
    if name in ("libretranslate", "libre"):
        return LibreTranslateProvider(source, target, endpoint=endpoint, api_key=key or None)
    if name in ("none", "passthrough", "offline"):
        return PassthroughProvider(source, target)
    raise ValueError(f"unknown provider: {name!r}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class TranslationResult:
    """Outcome for a single string."""

    __slots__ = ("original", "translated", "status", "error", "skipped_reason")

    def __init__(
        self,
        original: str,
        translated: str,
        status: str = "done",
        error: str = "",
        skipped_reason: str = "",
    ) -> None:
        self.original = original
        self.translated = translated
        self.status = status  # done | error | skipped | cached
        self.error = error
        self.skipped_reason = skipped_reason

    @property
    def ok(self) -> bool:
        return self.status in ("done", "cached", "skipped")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TranslationResult({self.original!r} -> {self.translated!r}, {self.status})"


class _RateLimiter:
    """Spaces out provider calls; safe to share between worker threads."""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed)
            self._next_allowed = slot + self.delay
        sleep_for = slot - now
        if sleep_for > 0:
            time.sleep(sleep_for)


class TranslatorEngine:
    """Cached, retrying, placeholder-safe translation with batching."""

    def __init__(
        self,
        cache_file: str = "translation_cache.json",
        source: str = DEFAULT_SOURCE,
        target: str = DEFAULT_TARGET,
        provider: Optional[BaseProvider] = None,
        provider_name: str = "google-web",
        delay: float = 0.35,
        max_retries: int = 3,
        flush_every: int = 25,
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        translate_all: bool = False,
    ) -> None:
        self.cache_file = cache_file
        self.source = source
        self.target = target
        self.provider = provider or build_provider(provider_name, source, target, api_key, endpoint)
        self.max_retries = max(1, int(max_retries))
        self.flush_every = max(1, int(flush_every))
        self.translate_all = bool(translate_all)

        self._lock = threading.Lock()
        self._dirty = 0
        self._limiter = _RateLimiter(delay)

        self.cache: Dict[str, str] = self._load_cache()
        self.stats: Dict[str, int] = {"done": 0, "cached": 0, "skipped": 0, "error": 0}
        self.last_error = ""

    # -- cache -------------------------------------------------------------
    def _load_cache(self) -> Dict[str, str]:
        if self.cache_file and os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except (OSError, ValueError):
                # A corrupt cache must never break a run.
                return {}
        return {}

    def _save_cache(self) -> None:
        """Write the cache atomically, so an interrupted run cannot truncate it."""
        if not self.cache_file:
            return
        tmp = f"{self.cache_file}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self.cache, handle, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.cache_file)
        except (OSError, TypeError, ValueError) as exc:
            self.last_error = str(exc)
            # Never leave a half-written temporary file lying around.
            try:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            except OSError:
                pass

    def flush(self) -> None:
        with self._lock:
            self._save_cache()
            self._dirty = 0

    def _cache_key(self, text: str) -> str:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        return f"{self.source}->{self.target}:{digest}"

    def cache_size(self) -> int:
        return len(self.cache)

    # -- translation -------------------------------------------------------
    def translate(self, text: str) -> str:
        """Backwards-compatible API: returns a string, never raises."""
        return self.translate_detailed(text).translated

    def translate_detailed(self, text: str) -> TranslationResult:
        original = text.strip()
        if not original:
            return TranslationResult(text, text, "skipped", skipped_reason="empty")

        ok, reason = should_translate(original, self.target, self.translate_all)
        if not ok:
            with self._lock:
                self.stats["skipped"] += 1
            return TranslationResult(text, text, "skipped", skipped_reason=reason)

        cache_key = self._cache_key(original)
        with self._lock:
            cached = self.cache.get(cache_key)
        if cached is not None:
            with self._lock:
                self.stats["cached"] += 1
            return TranslationResult(text, cached, "cached")

        masked, tokens = protect_segments(original)

        translated = ""
        error = ""
        for attempt in range(self.max_retries):
            try:
                translated = self._translate_masked(masked, tokens)
                if not translated.strip():
                    raise RuntimeError("provider returned empty translation")
                break
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                error = f"{type(exc).__name__}: {exc}"
                self.last_error = error
                translated = ""
                if attempt + 1 < self.max_retries:
                    backoff = (1.5 ** attempt) + random.uniform(0, 0.4)
                    time.sleep(min(backoff, 8.0))

        if translated:
            with self._lock:
                self.cache[cache_key] = translated
                self.stats["done"] += 1
                self._dirty += 1
                should_flush = self._dirty >= self.flush_every
            if should_flush:
                self.flush()
            return TranslationResult(text, translated, "done")

        with self._lock:
            self.stats["error"] += 1
        return TranslationResult(text, text, "error", error=error)

    @staticmethod
    def _split_masked(masked: str) -> List[str]:
        """Split a masked string back into its translatable segments."""
        return [part for part in _MASK_RE.split(masked) if part != ""] or [""]

    def _translate_masked(self, masked: str, tokens: List[str]) -> str:
        """Translate only the translatable parts of a masked string.

        The sentinel placeholders never reach the provider, and every protected
        token is spliced back at its exact original position.
        """
        pieces: List[str] = []
        cursor = 0
        for match in _MASK_RE.finditer(masked):
            chunk = masked[cursor : match.start()]
            if chunk:
                pieces.append(self._call_provider(chunk))
            pieces.append(tokens[int(match.group(1))])
            cursor = match.end()

        tail = masked[cursor:]
        if tail:
            pieces.append(self._call_provider(tail))
        return "".join(pieces)

    def _call_provider(self, text: str) -> str:
        self._limiter.wait()
        return self.provider.translate(text)

    # -- bulk --------------------------------------------------------------
    def translate_many(
        self,
        texts: List[str],
        on_result: Optional[Callable[[TranslationResult], None]] = None,
        workers: int = 1,
    ) -> List[TranslationResult]:
        """Translate a list, reusing the cache; results keep input order.

        ``workers`` > 1 fans the cache lookups out across threads. The provider
        calls themselves stay serialised by the rate limiter, so raising this
        does not get the account rate-limited.
        """
        results: List[Optional[TranslationResult]] = [None] * len(texts)

        def run(index: int, text: str) -> None:
            results[index] = self.translate_detailed(text)
            if on_result:
                on_result(results[index])  # type: ignore[arg-type]

        if workers and workers > 1 and len(texts) > 1:
            with ThreadPoolExecutor(max_workers=min(workers, 8)) as pool:
                list(pool.map(lambda pair: run(pair[0], pair[1]), list(enumerate(texts))))
        else:
            for index, text in enumerate(texts):
                run(index, text)

        self.flush()
        return [r for r in results if r is not None]

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self.provider.close()

    def __enter__(self) -> "TranslatorEngine":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
