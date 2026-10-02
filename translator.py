import json
import time
import os
from deep_translator import GoogleTranslator

class TranslatorEngine:
    def __init__(self, cache_file="translation_cache.json"):
        self.cache_file = cache_file
        self.cache = self._load_cache()
        self.translator = GoogleTranslator(source='en', target='id')

    def _load_cache(self):
        if os.path.exists(self.cache_file):
            with open(self.cache_file, 'r') as f:
                return json.load(f)
        return {}

    def _save_cache(self):
        with open(self.cache_file, 'w') as f:
            json.dump(self.cache, f, indent=4)

    def translate(self, text):
        clean_text = text.strip()
        if not clean_text or "=" in clean_text: return text
        if clean_text in self.cache: return self.cache[clean_text]

        try:
            translated = self.translator.translate(clean_text)
            self.cache[clean_text] = translated
            self._save_cache()
            time.sleep(0.6) # Anti-ban delay
            return translated
        except:
            return text
