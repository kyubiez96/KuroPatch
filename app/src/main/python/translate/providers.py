"""Translation providers for KuroPatch v2.

All providers follow the same interface. API keys are read from
environment variables at runtime only -- never hardcoded, never logged.
"""
import json
import os
import time
import urllib.error
import urllib.request

# HTTP statuses worth retrying: rate-limit + server errors are
# TRANSIENT. 4xx (401 bad key, 400 bad request, 404, ...) are NOT
# retried -- retrying a bad key is just slow failure.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Explicit key handoff from the Android app (Chaquopy).
#
# WHY THIS EXISTS (proven on-device bug, v2.0.9): Kotlin exported the
# key with android.system.Os.setenv("AGNES_API_KEY", key), but
# CPython's os.environ is a SNAPSHOT taken at interpreter startup --
# a libc setenv() from outside Python does NOT update the os.environ
# dict. So AgnesProvider._api_key() (os.environ.get(...)) ALWAYS saw
# "" on-device and the provider NEVER got the key, since v2.0.0. The
# failures were swallowed pre-4fb0954 ("0 translated, N unchanged");
# after 4fb0954 they surfaced as "AGNES_API_KEY environment variable"
# errors even though the user had saved a key. Only glossary
# translations ever landed on-device.
#
# FIX: the app calls set_api_key(key) explicitly via Chaquopy before
# any translate call. The key lives in this module variable, in-memory
# only -- never logged, never written to disk by Python. The env-var
# fallback stays for CLI use (where os.environ is genuinely set by
# the caller's shell).
_API_KEY = None


def set_api_key(key):
    """Set the Agnes API key for this process (Android app path).

    Blank / None clears the override, restoring pure env-var lookup.
    """
    global _API_KEY
    _API_KEY = key.strip() if isinstance(key, str) and key.strip() else None
_MAX_PROVIDER_RETRIES = 3
# Server-sent Retry-After is honoured but capped: a runaway "wait
# 3600s" must not stall the on-device translate job.
_RETRY_AFTER_CAP = 10
# Test hook: tests patch translate.providers._sleep to avoid real waits.
_sleep = time.sleep


def _retry_delay(attempt, headers):
    """Backoff before retry number ``attempt`` (0-based).

    Exponential 1s / 2s / 4s, or the server's Retry-After header when
    present and parseable (capped). Unparseable / negative values fall
    back to exponential.
    """
    retry_after = None
    if headers is not None:
        try:
            retry_after = int(headers.get("Retry-After", ""))
        except (ValueError, TypeError, AttributeError):
            retry_after = None
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, _RETRY_AFTER_CAP)
    return min(2 ** attempt, 8)


class BaseProvider:
    """Provider interface."""

    name = "base"

    def translate(self, text, source_lang="en", target_lang="id"):
        """Translate a single string. Returns translated string."""
        raise NotImplementedError

    def translate_batch(self, texts, source_lang="en", target_lang="id"):
        """Translate multiple strings. Default: one-by-one."""
        return [self.translate(t, source_lang, target_lang) for t in texts]

    def close(self):
        """Release provider resources."""


class PassthroughProvider(BaseProvider):
    """Identity provider -- used by tests and --dry-run."""

    name = "passthrough"

    def translate(self, text, source_lang="en", target_lang="id"):
        return text


class AgnesProvider(BaseProvider):
    """Free OpenAI-compatible backend (agnes-3.0-flash).

    Key resolution: explicit set_api_key() override FIRST (the Android
    app path -- Chaquopy; see module note), then the AGNES_API_KEY
    environment variable (CLI path). The key is never stored, never
    logged, never accepted as a default argument.
    """

    name = "agnes"
    endpoint = "https://apihub.agnes-ai.com/v1/chat/completions"
    model = "agnes-3.0-flash"

    def __init__(self, timeout=30):
        self.timeout = timeout

    def _api_key(self):
        # Explicit override (Android app via set_api_key) wins: on-device
        # os.environ never sees Kotlin's Os.setenv (CPython snapshot).
        if _API_KEY:
            return _API_KEY
        key = os.environ.get("AGNES_API_KEY", "")
        if not key:
            raise RuntimeError(
                "AgnesProvider requires the AGNES_API_KEY environment variable"
            )
        return key

    def _build_prompt(self, text, source_lang, target_lang):
        return (
            f"Translate the following text from {source_lang} to {target_lang}.\n"
            "Return ONLY the translation, no explanations, no quotes.\n"
            "Preserve all placeholders exactly as they appear "
            "(e.g. %d, %s, %1$s, {name}, {0}). Do not translate them, "
            "do not reorder them, do not add or remove any.\n"
            "Preserve any HTML/XML tags exactly.\n"
            f"Text: {text}"
        )

    def _fetch(self, req):
        """POST ``req`` with retry on transient HTTP statuses (429 / 5xx).

        A 429 means "slow down", not "provider dead". Without a retry,
        a burst of strings trips the free-tier rate limit, the engine
        counts 3 consecutive failures and aborts the whole batch --
        a jar with more than a handful of strings could never
        translate. 4xx and network errors fail FAST: retrying a bad
        key is just slow failure, and a dead network must not hang
        (the engine's batch abort handles it).
        """
        for attempt in range(_MAX_PROVIDER_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if (exc.code not in _RETRYABLE_STATUS
                        or attempt >= _MAX_PROVIDER_RETRIES):
                    raise RuntimeError(
                        f"agnes provider request failed: {exc}") from exc
                _sleep(_retry_delay(attempt, exc.headers))
            except Exception as exc:
                raise RuntimeError(
                    f"agnes provider request failed: {exc}") from exc
        # Unreachable: the loop always returns or raises.
        raise RuntimeError("agnes provider request failed: no response")

    def translate(self, text, source_lang="en", target_lang="id"):
        if not text.strip():
            return text
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": self._build_prompt(text, source_lang, target_lang)}],
            "temperature": 0.1,
            "max_tokens": 2000,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key()}",
            },
            method="POST",
        )
        data = self._fetch(req)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("agnes provider returned unexpected response") from exc
        # A null / non-string content (content filter, empty reply)
        # used to die here with a raw AttributeError on .strip() --
        # the engine caught it, but first_error showed a cryptic
        # "NoneType has no attribute 'strip'". Fail with a clear
        # message instead (round-1 final audit).
        if not isinstance(content, str):
            raise RuntimeError("agnes provider returned no content")
        content = content.strip()
        # A truncated response (max_tokens hit) is NOT a translation:
        # the text is cut mid-sentence and the validator cannot detect
        # that (placeholders may survive by luck). Fail instead of
        # shipping a half-sentence -- the engine keeps the source.
        if choice.get("finish_reason") == "length":
            raise RuntimeError(
                "agnes provider response truncated (max_tokens hit)")
        return content
