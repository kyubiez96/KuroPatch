"""Translation providers for KuroPatch v2.

All providers follow the same interface. API keys are read from
environment variables at runtime only -- never hardcoded, never logged.
"""
import json
import os
import urllib.request


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

    Reads API key from the AGNES_API_KEY environment variable at call
    time. The key is never stored, never logged, never accepted as a
    default argument.
    """

    name = "agnes"
    endpoint = "https://apihub.agnes-ai.com/v1/chat/completions"
    model = "agnes-3.0-flash"

    def __init__(self, timeout=30):
        self.timeout = timeout

    def _api_key(self):
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
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"agnes provider request failed: {exc}") from exc
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("agnes provider returned unexpected response") from exc
