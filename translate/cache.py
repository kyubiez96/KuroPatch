"""Translation cache -- never translate the same string twice.

JSON-file backed (stdlib only). Keyed by (source_text, source_lang,
target_lang, provider_name) so a provider/model change invalidates
stale entries automatically.
"""
import hashlib
import json
import os


def _key(text, source_lang, target_lang, provider_name):
    h = hashlib.sha256()
    h.update(provider_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(source_lang.encode("utf-8"))
    h.update(b"\x00")
    h.update(target_lang.encode("utf-8"))
    h.update(b"\x00")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


class TranslationCache:
    def __init__(self, path):
        self.path = path
        self._data = {}
        self._dirty = False
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # A cache file containing a JSON list / string / number
                # is corrupt -- .get() on it crashed with AttributeError.
                # Treat it like a missing cache, not a fatal error.
                self._data = data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, text, source_lang, target_lang, provider_name):
        value = self._data.get(
            _key(text, source_lang, target_lang, provider_name))
        # A hand-edited / corrupt cache file may hold a non-string value
        # (list, number, ...) under a valid key -- handing it back would
        # patch a game string with garbage (or crash the patch step).
        # Treat it as a cache miss, not a translation.
        return value if isinstance(value, str) else None

    def put(self, text, source_lang, target_lang, provider_name, translation):
        self._data[_key(text, source_lang, target_lang, provider_name)] = translation
        self._dirty = True

    def save(self):
        if not self._dirty:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._dirty = False

    def __len__(self):
        return len(self._data)
