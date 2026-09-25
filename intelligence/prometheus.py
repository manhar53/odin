# PROMETHEUS — Greek — brought knowledge to mankind, always learning
# Learning: adapts to user preferences, builds a user profile over time

import json
import os
import re
from datetime import datetime
from core.marduk import OdinModule


# Regex-based fact extractor. Tier-1 self-learning — runs on every user
# message, picks out self-disclosed facts, stores them. Cheap (no LLM call),
# misses nuance, but compounds over weeks of normal use. The patterns
# capture the value group; the category is hard-coded per pattern. False
# positives go in too — that's the trade-off; user can `forget` them.
_LEARN_PATTERNS = [
    # "my name is X" / "i'm X" (only when followed by a clear name pattern)
    (re.compile(r"\bmy\s+name\s+is\s+(.+?)(?:[.,]|$)", re.I),                       "name"),
    # "i prefer/like/love/enjoy X"
    (re.compile(r"\bi\s+(?:prefer|like|love|enjoy)\s+(.+?)(?:[.,]|$)", re.I),       "preference"),
    # "i hate/dislike X"
    (re.compile(r"\bi\s+(?:hate|dislike|can't\s+stand)\s+(.+?)(?:[.,]|$)", re.I),   "dislike"),
    # "i work at/on/for X"
    (re.compile(r"\bi\s+work\s+(?:at|on|for|with)\s+(.+?)(?:[.,]|$)", re.I),        "work"),
    # "i live in X"
    (re.compile(r"\bi\s+live\s+in\s+(.+?)(?:[.,]|$)", re.I),                        "location"),
    # "i'm allergic to X"
    (re.compile(r"\bi(?:'m|\s+am)\s+allergic\s+to\s+(.+?)(?:[.,]|$)", re.I),        "allergy"),
    # "my favorite X is Y" — two-group, special-cased below
    (re.compile(r"\bmy\s+favorite\s+(\w+)\s+is\s+(.+?)(?:[.,]|$)", re.I),           "favorite"),
    # "my X is Y" — broad; captures "my dog is Rex", "my car is a Tesla"
    (re.compile(r"\bmy\s+(\w+)\s+is\s+(?:a\s+|an\s+|the\s+)?(.+?)(?:[.,]|$)", re.I), "fact"),
]


class Prometheus(OdinModule):
    MODULE_NAME = "PROMETHEUS"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        self.prefs_path = config.get("prometheus", {}).get(
            "preferences_path", "data/knowledge/preferences.json"
        )
        self._prefs: dict = self._load()
        # Phonetic corrections: STT mishearings the user has reported.
        # Keys are LOWERCASE strings Whisper produces; values are what the
        # user actually said. Used by HEIMDALL to fix up commands before
        # fast-routing. Loaded once at boot; refreshed on every set/remove.
        self.phonetic_path = config.get("prometheus", {}).get(
            "phonetic_path", "data/knowledge/phonetic_corrections.json"
        )
        self._phonetic: dict = self._load_phonetic()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "learn_preference",
                "description": "Learn and store a user preference or habit",
                "parameters": {
                    "category": {"type": "string", "description": "Category (e.g. music, food, work)"},
                    "preference": {"type": "string", "description": "The preference to remember"}
                },
                "required": ["category", "preference"],
                "internal_only": True
            },
            {
                "name": "get_user_profile",
                "description": "Get a summary of known user preferences and habits",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "forget_preference",
                "description": "Remove a learned preference",
                "parameters": {
                    "category": {"type": "string", "description": "Category to forget"}
                },
                "required": ["category"],
                "internal_only": True
            },
            {
                "name": "set_phonetic_correction",
                "description": (
                    "Tell ODIN that when Whisper transcribes a certain word, "
                    "it actually meant something else. Useful for names "
                    "Whisper consistently mishears (e.g. 'Karthik' → 'Kartik'). "
                    "Stored case-insensitively. Applied to every voice command "
                    "before fast-routing."
                ),
                "parameters": {
                    "heard": {"type": "string", "description": "What Whisper transcribed (the wrong word)"},
                    "meant": {"type": "string", "description": "What the user actually said (the correct word)"},
                },
                "required": ["heard", "meant"],
            },
            {
                "name": "remove_phonetic_correction",
                "description": "Remove a previously-set phonetic correction.",
                "parameters": {
                    "heard": {"type": "string", "description": "The 'heard' key to remove"},
                },
                "required": ["heard"],
            },
            {
                "name": "list_phonetic_corrections",
                "description": "Show all phonetic corrections currently active.",
                "parameters": {},
                "required": [],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "learn_preference":            self._learn,
            "get_user_profile":            self._profile,
            "forget_preference":           self._forget,
            "set_phonetic_correction":     self._set_phonetic,
            "remove_phonetic_correction":  self._remove_phonetic,
            "list_phonetic_corrections":   self._list_phonetic,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[PROMETHEUS] Error: {e}"
        return f"[PROMETHEUS] Unknown skill: {skill_name}"

    def get_context_string(self) -> str:
        """Called by GILGAMESH to enrich system context with user profile."""
        if not self._prefs:
            return ""
        items = [f"{k}: {v['value']}" for k, v in self._prefs.items()]
        return "Known preferences: " + "; ".join(items[:5])

    def _learn(self, category: str = "", preference: str = "") -> str:
        self._prefs[category.lower()] = {
            "value": preference,
            "learned": datetime.now().isoformat()
        }
        self._save()
        return f"Got it. I'll remember your preference for {category}."

    def _profile(self) -> str:
        if not self._prefs:
            return "No preferences learned yet."
        lines = [f"{k}: {v['value']}" for k, v in self._prefs.items()]
        return "User profile — " + "; ".join(lines)

    def _forget(self, category: str = "") -> str:
        key = category.lower()
        if key in self._prefs:
            del self._prefs[key]
            self._save()
            return f"Forgotten preference: {category}."
        return f"No preference found for: {category}."

    # === Tier-1 auto-learn ==============================================
    # Called by HEIMDALL on every user message. Scans for self-disclosed
    # facts via regex, stores them with auto=True so we can distinguish
    # explicit from inferred entries later. No LLM round-trip.
    def auto_learn_from_message(self, message: str) -> list[str]:
        if not message or len(message) < 5:
            return []
        learned = []
        for pattern, category in _LEARN_PATTERNS:
            for m in pattern.finditer(message):
                groups = m.groups()
                if category in ("favorite", "fact") and len(groups) >= 2:
                    sub = (groups[0] or "").strip().lower()
                    value = (groups[1] or "").strip().strip(".!?,;:'\"() ")
                    key = f"{category}_{sub}"
                else:
                    value = (groups[0] or "").strip().strip(".!?,;:'\"() ")
                    key = category
                if not value or len(value) > 80:
                    continue
                # Avoid storing trivially-short values or pure stopwords.
                if len(value) < 2 or value.lower() in {"a", "the", "it", "this", "that"}:
                    continue
                prior = self._prefs.get(key)
                if prior and prior.get("value") == value:
                    continue  # already known
                self._prefs[key] = {
                    "value": value,
                    "learned": datetime.now().isoformat(),
                    "auto": True,
                }
                learned.append(f"{key}={value}")
        if learned:
            self._save()
        return learned

    def _load(self) -> dict:
        if os.path.exists(self.prefs_path):
            with open(self.prefs_path) as f:
                return json.load(f)
        return {}

    def _save(self):
        os.makedirs(os.path.dirname(self.prefs_path), exist_ok=True)
        with open(self.prefs_path, "w") as f:
            json.dump(self._prefs, f, indent=2)

    # ── Phonetic corrections ─────────────────────────────────────────
    def _set_phonetic(self, heard: str = "", meant: str = "") -> str:
        heard = (heard or "").strip().lower()
        meant = (meant or "").strip()
        if not heard or not meant:
            return "Need both 'heard' and 'meant'."
        if heard == meant.lower():
            return f"'{heard}' and '{meant}' are the same — nothing to correct."
        self._phonetic[heard] = meant
        self._save_phonetic()
        return f"Got it. When I hear '{heard}', I'll treat it as '{meant}'."

    def _remove_phonetic(self, heard: str = "") -> str:
        heard = (heard or "").strip().lower()
        if heard in self._phonetic:
            del self._phonetic[heard]
            self._save_phonetic()
            return f"Removed phonetic correction for '{heard}'."
        return f"No phonetic correction for '{heard}'."

    def _list_phonetic(self) -> str:
        if not self._phonetic:
            return "No phonetic corrections set."
        pairs = ", ".join(f"'{k}' → '{v}'" for k, v in self._phonetic.items())
        return f"Phonetic corrections: {pairs}"

    def get_phonetic_corrections(self) -> dict:
        """Public accessor — HEIMDALL reads this every command to apply
        STT mishearing fixes before fast-routing."""
        return dict(self._phonetic)

    def _load_phonetic(self) -> dict:
        if os.path.exists(self.phonetic_path):
            try:
                with open(self.phonetic_path) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _save_phonetic(self):
        try:
            os.makedirs(os.path.dirname(self.phonetic_path), exist_ok=True)
            with open(self.phonetic_path, "w") as f:
                json.dump(self._phonetic, f, indent=2)
        except OSError:
            pass
