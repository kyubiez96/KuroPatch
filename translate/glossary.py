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

# Language names -> correct Indonesian name.
# Language names are NEVER left to the LLM: it mistranslated
# 'Español' -> 'Jerman' and 'English' -> 'Ingris' (live test, 2026-10-14).
# Keys are lowercase; lookup matches the WHOLE stripped string only,
# so "English version" is NOT rewritten. Only applied when the
# target language is Indonesian (target_lang == "id").
LANGUAGE_NAMES_ID = {
    # English names
    "english": "Inggris",
    "spanish": "Spanyol",
    "german": "Jerman",
    "french": "Prancis",
    "italian": "Italia",
    "portuguese": "Portugis",
    "dutch": "Belanda",
    "russian": "Rusia",
    "japanese": "Jepang",
    "korean": "Korea",
    "chinese": "Mandarin",
    "mandarin": "Mandarin",
    "polish": "Polandia",
    "czech": "Ceko",
    "hungarian": "Hungaria",
    "turkish": "Turki",
    "arabic": "Arab",
    "hindi": "Hindi",
    "thai": "Thailand",
    "vietnamese": "Vietnam",
    "indonesian": "Indonesia",
    "malay": "Melayu",
    "swedish": "Swedia",
    "finnish": "Finlandia",
    "norwegian": "Norwegia",
    "danish": "Denmark",
    "greek": "Yunani",
    "hebrew": "Ibrani",
    "ukrainian": "Ukraina",
    "bulgarian": "Bulgaria",
    "romanian": "Rumania",
    "catalan": "Katalan",
    "galician": "Galisia",
    "basque": "Basque",
    "esperanto": "Esperanto",
    "latin": "Latin",
    "tagalog": "Filipina",
    "filipino": "Filipina",
    "punjabi": "Punjab",
    "bengali": "Bengali",
    "tamil": "Tamil",
    "telugu": "Telugu",
    "marathi": "Marathi",
    "gujarati": "Gujarat",
    "kannada": "Kannada",
    "malayalam": "Malayalam",
    "nepali": "Nepal",
    "sinhala": "Sinhala",
    "burmese": "Myanmar",
    "khmer": "Khmer",
    "lao": "Laos",
    "mongolian": "Mongolia",
    "georgian": "Georgia",
    "armenian": "Armenia",
    "azerbaijani": "Azerbaijan",
    "kazakh": "Kazakhstan",
    "uzbek": "Uzbekistan",
    "tatar": "Tatar",
    "slovak": "Slowakia",
    "slovenian": "Slovenia",
    "croatian": "Kroasia",
    "serbian": "Serbia",
    "bosnian": "Bosnia",
    "macedonian": "Makedonia",
    "albanian": "Albania",
    "latvian": "Latvia",
    "lithuanian": "Lituania",
    "estonian": "Estonia",
    "icelandic": "Islandia",
    "irish": "Irlandia",
    "welsh": "Wales",
    "maltese": "Malta",
    "swahili": "Swahili",
    "afrikaans": "Afrikaans",
    "yoruba": "Yoruba",
    "igbo": "Igbo",
    "hausa": "Hausa",
    "amharic": "Amharik",
    "somali": "Somalia",
    "zulu": "Zulu",
    "xhosa": "Xhosa",
    "shona": "Shona",
    "yiddish": "Yiddish",
    "persian": "Persia",
    "pashto": "Pashtun",
    "kurdish": "Kurdi",
    "urdu": "Urdu",
    "sindhi": "Sindhi",
    "assamese": "Assam",
    "odia": "Odia",
    "sanskrit": "Sanskerta",
    "pali": "Pali",
    # Native / autonym names
    "español": "Spanyol",
    "espanol": "Spanyol",
    "deutsch": "Jerman",
    "français": "Prancis",
    "francais": "Prancis",
    "italiano": "Italia",
    "português": "Portugis",
    "portugues": "Portugis",
    "nederlands": "Belanda",
    "русский": "Rusia",
    "日本語": "Jepang",
    "한국어": "Korea",
    "中文": "Mandarin",
    "polski": "Polandia",
    "čeština": "Ceko",
    "cestina": "Ceko",
    "magyar": "Hungaria",
    "türkçe": "Turki",
    "turkce": "Turki",
    "العربية": "Arab",
    "हिन्दी": "Hindi",
    "ไทย": "Thailand",
    "tiếng việt": "Vietnam",
    "bahasa indonesia": "Indonesia",
    "bahasa melayu": "Melayu",
    "bahasa inggeris": "Inggris",
    "melayu": "Melayu",
    "svenska": "Swedia",
    "suomi": "Finlandia",
    "norsk": "Norwegia",
    "dansk": "Denmark",
    "ελληνικά": "Yunani",
    "עברית": "Ibrani",
    "українська": "Ukraina",
    "български": "Bulgaria",
    "română": "Rumania",
    "català": "Katalan",
    "galego": "Galisia",
    "euskara": "Basque",
    "ਪੰਜਾਬੀ": "Punjab",
    "বাংলা": "Bengali",
    "தமிழ்": "Tamil",
    "తెలుగు": "Telugu",
    "मराठी": "Marathi",
    "ગુજરાતી": "Gujarat",
    "ಕನ್ನಡ": "Kannada",
    "മലയാളം": "Malayalam",
    "नेपाली": "Nepal",
    "සිංහල": "Sinhala",
    "မြန်မာ": "Myanmar",
    "ខ្មែរ": "Khmer",
    "ລາວ": "Laos",
    "монгол": "Mongolia",
    "ქართული": "Georgia",
    "հայերեն": "Armenia",
    "azərbaycan": "Azerbaijan",
    "қазақша": "Kazakhstan",
    "ўзбек": "Uzbekistan",
    "татарча": "Tatar",
    "slovenský": "Slowakia",
    "slovenčina": "Slowakia",
    "slovenščina": "Slovenia",
    "hrvatski": "Kroasia",
    "српски": "Serbia",
    "srpski": "Serbia",
    "bosanski": "Bosnia",
    "македонски": "Makedonia",
    "shqip": "Albania",
    "latviešu": "Latvia",
    "lietuvių": "Lituania",
    "eesti": "Estonia",
    "íslenska": "Islandia",
    "gaeilge": "Irlandia",
    "cymraeg": "Wales",
    "malti": "Malta",
    "kiswahili": "Swahili",
    "yorùbá": "Yoruba",
    "hausa": "Hausa",
    "አማርኛ": "Amharik",
    "soomaali": "Somalia",
    "isizulu": "Zulu",
    "isixhosa": "Xhosa",
    "chishona": "Shona",
    "ייִדיש": "Yiddish",
    "فارسی": "Persia",
    "پښتو": "Pashtun",
    "کوردی": "Kurdi",
    "kurdî": "Kurdi",
    "kurdi": "Kurdi",
    "اردو": "Urdu",
    "سنڌي": "Sindhi",
    "অসমীয়া": "Assam",
    "ଓଡ଼ିଆ": "Odia",
    "संस्कृतम्": "Sanskerta",
    "पालि": "Pali",
}


# "Name (Region)" -- e.g. "English (US)", "Español (México)".
# Locale pickers qualify a language with a region; the base name is
# translated via the fixed map, the region is kept verbatim.
_REGION_SUFFIX_RE = re.compile(r"^(.+?)\s*\(([^()]*)\)\s*$")


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

    def lookup_language(self, text):
        """Return the fixed Indonesian name if text is a language name.

        Whole-string match only (case-insensitive, whitespace-trimmed).
        Also handles "Name (Region)" -- the base name is translated via
        the fixed map and the region is kept verbatim, e.g.
        "English (US)" -> "Inggris (US)".
        Returns None for anything else -- sentences, fragments, and
        unknown strings fall through to the provider. The caller must
        only apply this when the target language is Indonesian.
        """
        key = text.strip().lower()
        hit = LANGUAGE_NAMES_ID.get(key)
        if hit is not None:
            return hit
        m = _REGION_SUFFIX_RE.match(text.strip())
        if m:
            base, suffix = m.group(1).strip(), m.group(2).strip()
            if base and suffix:
                hit = LANGUAGE_NAMES_ID.get(base.lower())
                if hit is not None:
                    return f"{hit} ({suffix})"
        return None

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


# ---------------------------------------------------------------------------
# Fixed translations -- exact full-string mappings applied BEFORE the
# provider call. Language names live in LANGUAGE_NAMES_ID (single source
# of truth); this table holds the other hallucination-prone categories:
# day/month names and bare UI labels. LLMs hallucinate on short bare strings (observed live:
# 'Español' -> 'Jerman', 'English' -> 'Ingris'), so these never reach
# the provider. Only exact matches (case-insensitive, whitespace-trimmed)
# apply -- never substring replacement, so sentences are untouched.
# ---------------------------------------------------------------------------

# Keys are lowercased source strings; values are the target translation.
FIXED_TRANSLATIONS = {
    "id": {
        # Day names.
        "monday": "Senin",
        "tuesday": "Selasa",
        "wednesday": "Rabu",
        "thursday": "Kamis",
        "friday": "Jumat",
        "saturday": "Sabtu",
        "sunday": "Minggu",
        # Month names.
        "january": "Januari",
        "february": "Februari",
        "march": "Maret",
        "april": "April",
        "may": "Mei",
        "june": "Juni",
        "july": "Juli",
        "august": "Agustus",
        "september": "September",
        "october": "Oktober",
        "november": "November",
        "december": "Desember",
        # Bare single-word UI terms (unambiguous as full-string labels).
        "yes": "Ya",
        "no": "Tidak",
        "cancel": "Batal",
        "back": "Kembali",
        "next": "Berikutnya",
        "previous": "Sebelumnya",
        "continue": "Lanjutkan",
        "retry": "Coba Lagi",
        "save": "Simpan",
        "load": "Muat",
        "delete": "Hapus",
        "settings": "Pengaturan",
        "options": "Opsi",
        "help": "Bantuan",
        "about": "Tentang",
        "exit": "Keluar",
        "quit": "Keluar",
        "start": "Mulai",
        "pause": "Jeda",
        "resume": "Lanjutkan",
        "restart": "Mulai Ulang",
        "play": "Main",
        "stop": "Berhenti",
        "new": "Baru",
        "open": "Buka",
        "close": "Tutup",
        "confirm": "Konfirmasi",
        "accept": "Terima",
        "decline": "Tolak",
        "skip": "Lewati",
        "done": "Selesai",
        "loading": "Memuat",
        "error": "Kesalahan",
        "warning": "Peringatan",
        "success": "Berhasil",
        "failed": "Gagal",
        "on": "Aktif",
        "off": "Nonaktif",
        "sound": "Suara",
        "music": "Musik",
        "language": "Bahasa",
        "player": "Pemain",
        "score": "Skor",
        "credits": "Kredit",
        "high score": "Skor Tertinggi",
        "game over": "Game Over",
        "press start": "Tekan Mulai",
        # "Select language" in the 6 languages J2ME games ship.
        # Castlevania v2.0.10: the provider turned FR "CHOIX DE LA
        # LANGUE" into "PELIHARAN BAHASA" ("peliharaan" = pet).
        # Fixed map wins over the provider, whole-string only.
        "select language": "Pilih Bahasa",
        "choix de la langue": "Pilih Bahasa",
        "choisir la langue": "Pilih Bahasa",
        "selecciona el idioma": "Pilih Bahasa",
        "selección de idioma": "Pilih Bahasa",
        "seleziona lingua": "Pilih Bahasa",
        "selezione lingua": "Pilih Bahasa",
        "sprachauswahl": "Pilih Bahasa",
        "sprache wählen": "Pilih Bahasa",
        "selecione o idioma": "Pilih Bahasa",
        "seleção de idioma": "Pilih Bahasa",
    },
}


def lookup_fixed(text, target_lang="id"):
    """Return the fixed translation for an exact full-string match.

    Case-insensitive, whitespace-trimmed. Returns None when there is no
    fixed mapping (caller falls through to cache/provider).
    """
    table = FIXED_TRANSLATIONS.get(target_lang)
    if not table:
        return None
    return table.get(text.strip().lower())
