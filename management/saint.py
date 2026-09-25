# SAINT — Universal — the healer, protector of wellbeing
# Health: vitals tracking, fitness reminders, hydration, sleep

import json
import os
from datetime import datetime
from core.marduk import OdinModule


class Saint(OdinModule):
    MODULE_NAME = "SAINT"
    LAYER = "MANAGEMENT"

    def __init__(self, config: dict):
        super().__init__(config)
        self._health_path = "data/knowledge/health.json"
        self._data: dict = self._load()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "log_water",
                "description": "Log a water intake entry",
                "parameters": {
                    "glasses": {"type": "integer", "description": "Number of glasses of water"}
                },
                "required": ["glasses"]
            },
            {
                "name": "get_health_summary",
                "description": "Get today's health and wellness summary",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "log_sleep",
                "description": "Log last night's sleep duration",
                "parameters": {
                    "hours": {"type": "number", "description": "Hours of sleep"}
                },
                "required": ["hours"],
                "internal_only": True
            },
            {
                "name": "log_workout",
                "description": "Log a workout or physical activity",
                "parameters": {
                    "activity": {"type": "string", "description": "Activity name"},
                    "duration_minutes": {"type": "integer", "description": "Duration in minutes"}
                },
                "required": ["activity", "duration_minutes"],
                "internal_only": True
            },
            {
                "name": "set_health_goal",
                "description": "Set a daily health goal (water, steps, sleep)",
                "parameters": {
                    "goal_type": {"type": "string", "description": "Goal type: water, sleep, workout"},
                    "target": {"type": "string", "description": "Target value"}
                },
                "required": ["goal_type", "target"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "log_water": self._log_water,
            "get_health_summary": self._summary,
            "log_sleep": self._log_sleep,
            "log_workout": self._log_workout,
            "set_health_goal": self._set_goal,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[SAINT] Error: {e}"
        return f"[SAINT] Unknown skill: {skill_name}"

    def _today_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _today(self) -> dict:
        key = self._today_key()
        if key not in self._data.get("days", {}):
            self._data.setdefault("days", {})[key] = {"water": 0, "sleep": None, "workouts": []}
        return self._data["days"][key]

    def _log_water(self, glasses: int = 1) -> str:
        today = self._today()
        today["water"] = today.get("water", 0) + glasses
        self._save()
        goal = self._data.get("goals", {}).get("water", 8)
        return f"Logged {glasses} glass(es). Total today: {today['water']} of {goal}."

    def _log_sleep(self, hours: float = 0) -> str:
        today = self._today()
        today["sleep"] = hours
        self._save()
        return f"Sleep logged: {hours} hours."

    def _log_workout(self, activity: str = "", duration_minutes: int = 0) -> str:
        today = self._today()
        today.setdefault("workouts", []).append({
            "activity": activity, "minutes": duration_minutes
        })
        self._save()
        return f"Workout logged: {activity} for {duration_minutes} minutes."

    def _summary(self) -> str:
        today = self._today()
        water = today.get("water", 0)
        sleep = today.get("sleep", "not logged")
        workouts = today.get("workouts", [])
        wk_str = ", ".join(f"{w['activity']} {w['minutes']}min" for w in workouts) or "none"
        return f"Today: water {water} glasses, sleep {sleep} hours, workouts: {wk_str}."

    def _set_goal(self, goal_type: str = "", target: str = "") -> str:
        self._data.setdefault("goals", {})[goal_type] = target
        self._save()
        return f"Health goal set — {goal_type}: {target}."

    def _load(self) -> dict:
        if os.path.exists(self._health_path):
            with open(self._health_path) as f:
                return json.load(f)
        return {"days": {}, "goals": {"water": 8, "sleep": 8}}

    def _save(self):
        os.makedirs(os.path.dirname(self._health_path), exist_ok=True)
        with open(self._health_path, "w") as f:
            json.dump(self._data, f, indent=2)
