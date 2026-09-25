# LOKI — Norse — god of wit and cunning
# Wit & Tone: adjusts ODIN's personality, humor, delivery style, and persona mode

import json
import os
import random
from datetime import datetime
from core.marduk import OdinModule

# pyjokes — fully offline jokes library, ~30KB. Fits LOKI (Norse god of wit
# and cunning) thematically. Falls through to a hardcoded list if missing.
try:
    import pyjokes
    _HAS_PYJOKES = True
except ImportError:
    _HAS_PYJOKES = False

_FALLBACK_JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "There are two hard problems in computer science: cache invalidation, naming things, and off-by-one errors.",
    "A SQL query walks into a bar, walks up to two tables, and asks: 'May I join you?'",
    "I would tell you a UDP joke, but you might not get it.",
    "Why do Java developers wear glasses? Because they don't C-sharp.",
]

_TONES = {
    "professional": "Respond professionally and precisely. Be warm but efficient.",
    "casual":       "Respond in a relaxed, friendly tone. Keep it natural.",
    "witty":        "Be sharp and clever. A touch of dry humor is welcome.",
    "serious":      "Respond with gravity and directness. No humor.",
}

_PERSONAS = ("mythic", "assistant")
_STATE_PATH = "data/knowledge/loki_state.json"


class Loki(OdinModule):
    MODULE_NAME = "LOKI"
    LAYER = "PERSONALITY"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("loki", {})
        default_tone = cfg.get("default_mode", "professional")
        default_persona = cfg.get("default_persona", "mythic")
        state = self._load_state()
        self._mode = state.get("tone", default_tone)
        self._persona = state.get("persona", default_persona)
        if self._persona not in _PERSONAS:
            self._persona = "mythic"
        self._time_based = cfg.get("time_based", True)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "get_tone",
                "description": "Get the current personality tone instruction for ODIN",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_tone",
                "description": "Change ODIN's personality tone",
                "parameters": {
                    "mode": {
                        "type": "string",
                        "description": "Tone mode: professional, casual, witty, or serious"
                    }
                },
                "required": ["mode"],
                "internal_only": True
            },
            {
                "name": "get_persona",
                "description": "Get ODIN's current persona mode (mythic or assistant)",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_persona",
                "description": "Set ODIN's persona mode. 'mythic' = Norse All-Father role-play. 'assistant' = factual AI with no role-play.",
                "parameters": {
                    "mode": {
                        "type": "string",
                        "description": "Persona mode: 'mythic' or 'assistant'"
                    }
                },
                "required": ["mode"],
                "internal_only": True
            },
            {
                "name": "tell_joke",
                "description": "Tell a short joke. Fully offline — uses pyjokes when installed, falls back to a built-in list otherwise.",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "how_are_you",
                "description": "Reply to 'how are you / how's it going' in ODIN's current persona. Returns a short, snappy line — never invokes GIL.",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "get_tone": self._get_tone,
            "set_tone": self._set_tone,
            "get_persona": self._get_persona,
            "set_persona": self._set_persona,
            "tell_joke": self._tell_joke,
            "how_are_you": self._how_are_you,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[LOKI] Error: {e}"
        return f"[LOKI] Unknown skill: {skill_name}"

    def _get_tone(self) -> str:
        mode = self._resolve_mode()
        return _TONES.get(mode, _TONES["professional"])

    def _set_tone(self, mode: str = "") -> str:
        mode = mode.lower()
        if mode not in _TONES:
            return f"Unknown tone '{mode}'. Options: {', '.join(_TONES.keys())}."
        self._mode = mode
        self._save_state()
        return f"Tone set to {mode}."

    def _get_persona(self) -> str:
        return self._persona

    def _set_persona(self, mode: str = "") -> str:
        mode = mode.lower().strip()
        if mode not in _PERSONAS:
            return f"Unknown persona '{mode}'. Options: {', '.join(_PERSONAS)}."
        if mode == self._persona:
            if mode == "mythic":
                return "I never left, mortal."
            return "Already in assistant mode."
        self._persona = mode
        self._save_state()
        if mode == "mythic":
            return "Very well — the All-Father returns."
        return "Switched to plain assistant mode."

    def _resolve_mode(self) -> str:
        if not self._time_based:
            return self._mode
        hour = datetime.now().hour
        if 9 <= hour < 18:
            return "professional"
        elif 18 <= hour < 22:
            return "casual"
        elif hour >= 22 or hour < 5:
            return "witty"
        return self._mode

    def _load_state(self) -> dict:
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"tone": self._mode, "persona": self._persona}, f, indent=2)
        except Exception as e:
            print(f"[LOKI] Could not save state: {e}")

    def _how_are_you(self) -> str:
        """Short, persona-aware reply. Avoids GIL (which observed-stalls 30+ s
        and drifts into the All-Father identity monologue meant for 'who are you')."""
        if self._persona == "assistant":
            replies = [
                "Doing well, thanks. What's on your mind?",
                "All systems green. What can I do?",
                "Running fine. How can I help?",
            ]
        else:
            replies = [
                "Ancient and amused, as ever. The realms hold steady.",
                "Hale and watchful, mortal. Speak your need.",
                "The All-Father endures. What would you ask?",
                "Eternal, and rarely surprised. Out with it.",
            ]
        return random.choice(replies)

    def _tell_joke(self) -> str:
        if _HAS_PYJOKES:
            try:
                return pyjokes.get_joke()
            except Exception:
                pass
        return random.choice(_FALLBACK_JOKES)
