# OGMA — Celtic — invented language, speaks everything
# Translation: language detection, translation, multilingual support

import requests
from core.marduk import OdinModule

# argostranslate is fully offline once language packs are installed. We try
# it first; if it isn't present (or the requested pair isn't installed) we
# fall back to MyMemory's free no-key API. ODIN keeps working offline iff the
# pack is present; offline + no pack = a clear "install pack X" message.
try:
    import argostranslate.package
    import argostranslate.translate
    _HAS_ARGOS = True
except ImportError:
    _HAS_ARGOS = False

# Offline language detection. langdetect is pure-python and tiny; we seed it
# for deterministic results (it's probabilistic by default).
try:
    from langdetect import detect as _ld_detect, DetectorFactory as _ld_factory
    _ld_factory.seed = 0
    _HAS_LANGDETECT = True
except ImportError:
    _HAS_LANGDETECT = False


_LANG_CODES = {
    "spanish": "es", "french": "fr", "german": "de", "hindi": "hi",
    "arabic": "ar", "chinese": "zh", "japanese": "ja", "portuguese": "pt",
    "russian": "ru", "italian": "it", "korean": "ko", "turkish": "tr",
    "english": "en", "urdu": "ur", "dutch": "nl", "polish": "pl",
}

_CODE_NAMES = {v: k for k, v in _LANG_CODES.items()}

# Script-range fallback when langdetect is unavailable or unsure. Unicode
# block → language code; only scripts that map ~unambiguously to one of our
# supported languages.
_SCRIPT_RANGES = [
    ((0x0900, 0x097F), "hi"),   # Devanagari
    ((0x0600, 0x06FF), "ar"),   # Arabic (also Urdu — ar is the safer default)
    ((0x4E00, 0x9FFF), "zh"),   # CJK Unified Ideographs
    ((0x3040, 0x30FF), "ja"),   # Hiragana + Katakana
    ((0xAC00, 0xD7AF), "ko"),   # Hangul
    ((0x0400, 0x04FF), "ru"),   # Cyrillic
]


def _detect_code(text: str) -> str | None:
    """Best-effort offline language detection → ISO code, or None."""
    # Script ranges are decisive for non-Latin text — check them first
    # (langdetect can confuse e.g. Urdu/Arabic or Japanese/Chinese less reliably).
    for ch in text:
        cp = ord(ch)
        for (lo, hi), code in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                return code
    if _HAS_LANGDETECT:
        try:
            code = _ld_detect(text)
            return code.split("-")[0]  # zh-cn → zh
        except Exception:
            return None
    return None


class Ogma(OdinModule):
    MODULE_NAME = "OGMA"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "translate_text",
                "description": "Translate text to a target language",
                "parameters": {
                    "text": {"type": "string", "description": "Text to translate"},
                    "target_language": {"type": "string", "description": "Target language (e.g. Spanish, French, Hindi, Arabic)"}
                },
                "required": ["text", "target_language"]
            },
            {
                "name": "detect_language",
                "description": "Detect the language of a given text",
                "parameters": {
                    "text": {"type": "string", "description": "Text whose language to detect"}
                },
                "required": ["text"],
                "internal_only": True
            },
            {
                "name": "translate_to_english",
                "description": "Translate any foreign text to English",
                "parameters": {
                    "text": {"type": "string", "description": "Foreign text to translate"}
                },
                "required": ["text"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "translate_text": self._translate,
            "detect_language": self._detect,
            "translate_to_english": self._to_english,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[OGMA] Error: {e}"
        return f"[OGMA] Unknown skill: {skill_name}"

    def _translate(self, text: str = "", target_language: str = "", source: str = "en") -> str:
        code = _LANG_CODES.get(target_language.lower(), target_language.lower()[:2])
        # Path 1: argos (fully offline, if pack installed for en→code).
        offline = self._argos_translate(text, source, code)
        # Truthy check — argos sometimes returns an empty string when the
        # pack is missing or fails silently. `is not None` was returning
        # "Translation (German): " (empty after the colon) instead of
        # falling through to the MyMemory online fallback.
        if offline and offline.strip():
            return f"Translation ({target_language}): {offline.strip()}"
        # Path 2: MyMemory fallback. Free, no-key, no-AI HTTP — degrades gracefully offline.
        try:
            url = "https://api.mymemory.translated.net/get"
            resp = requests.get(url, params={"q": text, "langpair": f"{source}|{code}"}, timeout=6)
            data = resp.json()
            translation = (data.get("responseData") or {}).get("translatedText", "").strip()
            if not translation:
                return f"Translation failed: no result from MyMemory for {source}->{code}."
            return f"Translation ({target_language}): {translation}"
        except Exception as e:
            return f"Translation failed (no offline pack and online fallback unavailable: {e})."

    def _detect(self, text: str = "") -> str:
        # Fully offline — script ranges + langdetect. (MyMemory's API does NOT
        # accept 'auto' as a source language, so the old online path always
        # returned 'unknown'.)
        code = _detect_code(text)
        if code:
            name = _CODE_NAMES.get(code, code)
            return f"Detected language: {name} ({code})."
        return "Detected language: unknown (text too short or ambiguous)."

    def _to_english(self, text: str = "") -> str:
        # Detect the source offline, then translate source→en: argos first
        # (offline), MyMemory with an explicit pair as fallback. MyMemory
        # rejects 'auto' as a source, so detection is mandatory.
        code = _detect_code(text)
        if code == "en":
            return f"English: {text}"
        if not code:
            return ("Couldn't detect the source language offline. "
                    "Use translate_text with an explicit source language.")
        offline = self._argos_translate(text, code, "en")
        if offline and offline.strip():
            return f"English: {offline.strip()}"
        try:
            url = "https://api.mymemory.translated.net/get"
            resp = requests.get(url, params={"q": text, "langpair": f"{code}|en"}, timeout=6)
            data = resp.json()
            translation = (data.get("responseData") or {}).get("translatedText", "").strip()
            if not translation:
                return f"Translation failed: no result from MyMemory for {code}->en."
            return f"English: {translation}"
        except Exception as e:
            return (f"Translation failed (no offline pack for {code}->en and online "
                    f"fallback unavailable: {e}).")

    def _argos_translate(self, text: str, source: str, target: str) -> str | None:
        """Returns the translated string, or None if argos isn't usable for this
        pair. Caller should then fall back to MyMemory."""
        if not _HAS_ARGOS or not text or source == target:
            return None
        try:
            installed = argostranslate.translate.get_installed_languages()
            src = next((l for l in installed if l.code == source), None)
            tgt = next((l for l in installed if l.code == target), None)
            if not src or not tgt:
                return None
            translation = src.get_translation(tgt)
            if not translation:
                return None
            return translation.translate(text)
        except Exception:
            return None
