# CASSANDRA — Greek hero — always warned, always right
# Alerts: warnings, flags, system notifications, anomaly detection

import os
import json
import psutil
from datetime import datetime
from core.marduk import OdinModule

# winotify is pure-Python (no compiled deps) and produces native Windows toasts.
# Falls through to a print if the user hasn't installed it yet.
try:
    from winotify import Notification, audio as _wn_audio
    _HAS_TOAST = True
except ImportError:
    _HAS_TOAST = False


class Cassandra(OdinModule):
    MODULE_NAME = "CASSANDRA"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)
        self._alerts_path = "data/knowledge/alerts.json"
        self._alerts: list = self._load()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "add_alert",
                "description": "Add a persistent alert or warning flag",
                "parameters": {
                    "message": {"type": "string", "description": "Alert message"},
                    "severity": {"type": "string", "description": "Severity: low, medium, high"}
                },
                "required": ["message"],
                "internal_only": True
            },
            {
                "name": "list_alerts",
                "description": "List all active alerts and warnings",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "dismiss_alert",
                "description": "Dismiss an alert by its message text",
                "parameters": {
                    "message": {"type": "string", "description": "Alert text to dismiss"}
                },
                "required": ["message"],
                "internal_only": True
            },
            {
                "name": "check_system_alerts",
                "description": "Auto-detect system issues: low disk, high RAM, high CPU",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "notify",
                "description": "Show a Windows desktop toast notification",
                "parameters": {
                    "message": {"type": "string", "description": "Message body"},
                    "title": {"type": "string", "description": "Optional title (default 'ODIN')"}
                },
                "required": ["message"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "add_alert": self._add,
            "list_alerts": self._list,
            "dismiss_alert": self._dismiss,
            "check_system_alerts": self._system_check,
            "notify": self._notify,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[CASSANDRA] Error: {e}"
        return f"[CASSANDRA] Unknown skill: {skill_name}"

    def _add(self, message: str = "", severity: str = "medium") -> str:
        self._alerts.append({
            "message": message,
            "severity": severity,
            "time": datetime.now().isoformat()
        })
        self._save()
        # High-severity alerts also raise a desktop toast so the user can't miss them.
        if severity.lower() == "high":
            self._notify(message=message, title=f"ODIN alert ({severity})")
        return f"Alert logged: [{severity.upper()}] {message}"

    def _notify(self, message: str = "", title: str = "ODIN") -> str:
        if not _HAS_TOAST:
            print(f"[CASSANDRA toast] {title}: {message}")
            return f"Toast (fallback print): {message}"
        try:
            n = Notification(app_id="ODIN", title=title, msg=message, duration="short")
            n.set_audio(_wn_audio.Default, loop=False)
            n.show()
            return f"Toast shown: {message}"
        except Exception as e:
            return f"Toast failed: {e}"

    def _list(self) -> str:
        if not self._alerts:
            return "No active alerts."
        return "Alerts: " + " | ".join(
            f"[{a['severity'].upper()}] {a['message']}" for a in self._alerts
        )

    def _dismiss(self, message: str = "") -> str:
        original = len(self._alerts)
        self._alerts = [a for a in self._alerts if message.lower() not in a["message"].lower()]
        self._save()
        removed = original - len(self._alerts)
        return f"Dismissed {removed} alert(s)."

    def _system_check(self) -> str:
        warnings = []
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")

        if cpu > 85:
            warnings.append(f"CPU critically high at {cpu:.0f}%")
        if ram.percent > 85:
            warnings.append(f"RAM critically high at {ram.percent:.0f}%")
        if disk.percent > 90:
            free_gb = disk.free // (1024 ** 3)
            warnings.append(f"Disk C: almost full — only {free_gb}GB free")

        if not warnings:
            return f"All systems nominal. CPU {cpu:.0f}%, RAM {ram.percent:.0f}%, Disk {disk.percent:.0f}%."
        return "WARNINGS: " + "; ".join(warnings)

    def _load(self) -> list:
        if os.path.exists(self._alerts_path):
            with open(self._alerts_path) as f:
                return json.load(f)
        return []

    def _save(self):
        os.makedirs(os.path.dirname(self._alerts_path), exist_ok=True)
        with open(self._alerts_path, "w") as f:
            json.dump(self._alerts, f, indent=2)
