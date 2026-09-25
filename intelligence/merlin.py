# MERLIN — Celtic hero — reads situations, understands intent
# Context: situational awareness, current mode, ambient state for GILGAMESH

import json
import os
from datetime import datetime
from core.marduk import OdinModule


_STATE_PATH = "data/knowledge/merlin_state.json"


class Merlin(OdinModule):
    MODULE_NAME = "MERLIN"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        self._active_modes: set[str] = self._load_modes()
        self._last_topic: str = ""

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "get_context",
                "description": "Get current situational context (time, mode, state)",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_mode",
                "description": "Set an active context mode (e.g. focus, travel, work, rest)",
                "parameters": {
                    "mode": {"type": "string", "description": "Mode to activate"}
                },
                "required": ["mode"],
                "internal_only": True
            },
            {
                "name": "clear_mode",
                "description": "Deactivate a context mode",
                "parameters": {
                    "mode": {"type": "string", "description": "Mode to deactivate"}
                },
                "required": ["mode"],
                "internal_only": True
            },
            {
                "name": "get_active_modes",
                "description": "List all currently active modes",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "get_context": self._get_context,
            "set_mode": self._set_mode,
            "clear_mode": self._clear_mode,
            "get_active_modes": self._get_modes,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[MERLIN] Error: {e}"
        return f"[MERLIN] Unknown skill: {skill_name}"

    def _get_context(self) -> str:
        now = datetime.now()
        hour = now.hour
        if 5 <= hour < 12:
            period = "morning"
        elif 12 <= hour < 17:
            period = "afternoon"
        elif 17 <= hour < 21:
            period = "evening"
        else:
            period = "night"

        day = now.strftime("%A")
        date_str = now.strftime("%B %d")
        ctx = f"It is {day} {period}, {date_str}."

        if self._active_modes:
            ctx += f" Active modes: {', '.join(self._active_modes)}."

        # Enrich with user preferences if PROMETHEUS is available
        if self.marduk:
            prom = self.marduk.get_module("PROMETHEUS")
            if prom:
                prefs = prom.get_context_string()
                if prefs:
                    ctx += f" {prefs}."

        # Working memory: tell GIL what ODIN currently holds in mind (the
        # last link/file/screen/found path it comprehended), so free-form
        # turns like "so what do you think of it?" have a referent without
        # a tool call. Kept short — the 3B brain's context is precious.
        wm = self._working_memory_line()
        if wm:
            ctx += f" {wm}"

        return ctx

    def _working_memory_line(self) -> str:
        try:
            path = "data/knowledge/last_learned.json"
            if not os.path.exists(path):
                return ""
            # Stale attention is worse than none: only surface items from
            # the last 2 hours.
            if (datetime.now().timestamp() - os.path.getmtime(path)) > 7200:
                return ""
            with open(path, "r", encoding="utf-8") as f:
                last = json.load(f)
            title = str(last.get("title", "")).strip()
            if not title:
                return ""
            kind = str(last.get("kind", "content")).strip()
            gist = " ".join(str(last.get("note", "")).split())[:160]
            line = f"Currently in working memory ({kind}): '{title}'."
            if gist:
                line += f" Gist: {gist}"
            return line
        except (OSError, ValueError):
            return ""

    def _set_mode(self, mode: str = "") -> str:
        self._active_modes.add(mode.lower())
        self._save_modes()
        return f"Context mode '{mode}' activated."

    def _clear_mode(self, mode: str = "") -> str:
        self._active_modes.discard(mode.lower())
        self._save_modes()
        return f"Context mode '{mode}' deactivated."

    def _get_modes(self) -> str:
        if not self._active_modes:
            return "No active context modes."
        return "Active modes: " + ", ".join(self._active_modes)

    # === Persistence ====================================================
    # Active modes (focus, night, travel, etc.) survive a restart so ODIN
    # doesn't forget that ARJUN still has hosts blocked or SELENE still has
    # quiet hours active. Cleared by the matching mode owner on deactivate.
    def _load_modes(self) -> set[str]:
        if not os.path.exists(_STATE_PATH):
            return set()
        try:
            with open(_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            modes = set(data.get("active_modes", []))
            if modes:
                print(f"[MERLIN] Restored active modes: {', '.join(modes)}")
            return modes
        except (OSError, json.JSONDecodeError):
            return set()

    def _save_modes(self):
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump({"active_modes": sorted(self._active_modes)}, f, indent=2)
        except OSError as e:
            self._log.warning(f"merlin state save failed: {e}")
