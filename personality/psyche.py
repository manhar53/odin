# PSYCHE — Greek — goddess of the soul and feeling
# Emotion: reads user mood, adjusts ODIN's empathy and response behavior

from core.marduk import OdinModule

_EMOTION_KEYWORDS = {
    "happy":    ["happy", "great", "awesome", "excited", "love", "fantastic", "amazing", "wonderful"],
    "sad":      ["sad", "depressed", "down", "miss", "lonely", "unhappy", "crying", "upset"],
    "stressed": ["stressed", "anxious", "worried", "overwhelmed", "panic", "nervous", "pressure"],
    "angry":    ["angry", "frustrated", "annoyed", "furious", "mad", "irritated", "hate"],
    "tired":    ["tired", "exhausted", "sleepy", "drained", "fatigue", "worn out"],
    "sick":     ["sick", "ill", "fever", "headache", "not well", "unwell", "pain"],
    "bored":    ["bored", "nothing to do", "dull", "monotonous"],
}

_EMPATHY_RESPONSES = {
    "sad":      "The user seems emotionally low. Be warm, gentle, and supportive.",
    "stressed": "The user is stressed. Be calm, reassuring, and solution-focused.",
    "angry":    "The user is frustrated. Be patient, understanding, and avoid being dismissive.",
    "tired":    "The user is tired. Keep responses short, clear, and low-effort.",
    "sick":     "The user is unwell. Be compassionate and suggest rest where appropriate.",
    "happy":    "The user is in good spirits. Match their energy — be upbeat.",
    "bored":    "The user is bored. Be engaging and slightly playful.",
}


class Psyche(OdinModule):
    MODULE_NAME = "PSYCHE"
    LAYER = "PERSONALITY"

    def __init__(self, config: dict):
        super().__init__(config)
        self._current_emotion = "neutral"

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "detect_emotion",
                "description": "Detect the user's emotional state from their message",
                "parameters": {
                    "text": {"type": "string", "description": "User's message to analyze"}
                },
                "required": ["text"],
                "internal_only": True
            },
            {
                "name": "get_empathy_context",
                "description": "Get the current empathy instruction based on detected emotion",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "get_current_emotion",
                "description": "Get the most recently detected user emotion",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "detect_emotion": self._detect,
            "get_empathy_context": self._empathy_context,
            "get_current_emotion": self._current,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[PSYCHE] Error: {e}"
        return f"[PSYCHE] Unknown skill: {skill_name}"

    def _detect(self, text: str = "") -> str:
        t = text.lower()
        for emotion, keywords in _EMOTION_KEYWORDS.items():
            if any(kw in t for kw in keywords):
                self._current_emotion = emotion
                return f"Detected emotion: {emotion}."
        self._current_emotion = "neutral"
        return "Emotion: neutral."

    def _empathy_context(self) -> str:
        return _EMPATHY_RESPONSES.get(self._current_emotion, "")

    def _current(self) -> str:
        return self._current_emotion
