# ARJUN — Hindu hero — greatest archer, absolute focus
# Focus: do not disturb mode, deep work, distraction blocking, Pomodoro

import os
import time
import ctypes
import threading
from datetime import datetime
from core.marduk import OdinModule


_HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
_ARJUN_BEGIN = "# === ARJUN focus block — start ===\n"
_ARJUN_END = "# === ARJUN focus block — end ===\n"


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


class Arjun(OdinModule):
    MODULE_NAME = "ARJUN"
    LAYER = "MANAGEMENT"

    def __init__(self, config: dict):
        super().__init__(config)
        self._focus_active = False
        self._focus_end_time: float | None = None
        self._session_count = 0
        # Read once at boot; live edits to config.yaml don't take effect until restart.
        self._block_sites: list[str] = list(
            (config or {}).get("arjun", {}).get("focus_block_sites") or []
        )

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "start_focus",
                "description": "Activate focus mode — silences notifications and blocks distractions",
                "parameters": {
                    "duration_minutes": {
                        "type": "integer",
                        "description": "How long to focus in minutes (default 25)"
                    }
                },
                "required": [],
                "internal_only": True
            },
            {
                "name": "end_focus",
                "description": "Deactivate focus mode",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "get_focus_status",
                "description": "Check if focus mode is currently active",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "start_pomodoro",
                "description": "Start a Pomodoro timer (25 min work, 5 min break)",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "start_focus": self._start_focus,
            "end_focus": self._end_focus,
            "get_focus_status": self._status,
            "start_pomodoro": self._pomodoro,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[ARJUN] Error: {e}"
        return f"[ARJUN] Unknown skill: {skill_name}"

    def is_focus_active(self) -> bool:
        return self._focus_active

    def _start_focus(self, duration_minutes: int = 25) -> str:
        self._focus_active = True
        self._focus_end_time = time.time() + (duration_minutes * 60)
        # Notify MERLIN about focus mode via MARDUK
        if self.marduk:
            self.send("MERLIN", "set_mode", mode="focus")
        block_msg = self._block_sites_in_hosts()
        threading.Thread(
            target=self._auto_end_focus,
            args=(duration_minutes,),
            daemon=True
        ).start()
        return f"Focus mode activated for {duration_minutes} minutes. Eyes on the target. {block_msg}".strip()

    def _auto_end_focus(self, minutes: int):
        time.sleep(minutes * 60)
        if self._focus_active:
            self._focus_active = False
            self._unblock_sites_in_hosts()
            if self.marduk:
                iris = self.marduk.get_module("IRIS")
                if iris:
                    iris.speak("Focus session complete. Well done.")
            self.send("MERLIN", "clear_mode", mode="focus")

    def _end_focus(self) -> str:
        self._focus_active = False
        self._focus_end_time = None
        self._unblock_sites_in_hosts()
        if self.marduk:
            self.send("MERLIN", "clear_mode", mode="focus")
        return "Focus mode deactivated."

    # === Hosts-file blocking ============================================
    # Editing C:\Windows\System32\drivers\etc\hosts requires admin. We map each
    # blocked domain (and its 'www.' variant) to 127.0.0.1, fenced by ARJUN
    # sentinel comments so we can reverse the change cleanly without touching
    # the user's other hosts entries.
    def _block_sites_in_hosts(self) -> str:
        if not self._block_sites:
            return ""
        if not _is_admin():
            return "(Site blocking skipped — needs admin. Restart ODIN as administrator.)"
        try:
            entries = []
            for site in self._block_sites:
                domain = site.strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
                if not domain:
                    continue
                entries.append(f"127.0.0.1 {domain}\n")
                if not domain.startswith("www."):
                    entries.append(f"127.0.0.1 www.{domain}\n")
            with open(_HOSTS_PATH, "r", encoding="utf-8") as f:
                current = f.read()
            # Remove any stale ARJUN block first (ungraceful prior shutdown).
            current = self._strip_arjun_block(current)
            new_content = current.rstrip() + "\n\n" + _ARJUN_BEGIN + "".join(entries) + _ARJUN_END
            with open(_HOSTS_PATH, "w", encoding="utf-8") as f:
                f.write(new_content)
            return f"Blocked {len(self._block_sites)} site(s)."
        except Exception as e:
            return f"(Site blocking failed: {e})"

    def _unblock_sites_in_hosts(self):
        if not self._block_sites:
            return
        if not _is_admin():
            return
        try:
            with open(_HOSTS_PATH, "r", encoding="utf-8") as f:
                current = f.read()
            new = self._strip_arjun_block(current)
            if new != current:
                with open(_HOSTS_PATH, "w", encoding="utf-8") as f:
                    f.write(new)
        except Exception as e:
            self._log.warning(f"hosts unblock failed: {e}")

    @staticmethod
    def _strip_arjun_block(content: str) -> str:
        if _ARJUN_BEGIN not in content:
            return content
        before, _, rest = content.partition(_ARJUN_BEGIN)
        _, _, after = rest.partition(_ARJUN_END)
        return before.rstrip() + ("\n" + after.lstrip() if after.strip() else "\n")

    def _status(self) -> str:
        if not self._focus_active:
            return "Focus mode is inactive."
        if self._focus_end_time:
            remaining = max(0, int((self._focus_end_time - time.time()) / 60))
            return f"Focus mode active. {remaining} minutes remaining."
        return "Focus mode is active."

    def _pomodoro(self) -> str:
        self._session_count += 1
        self._start_focus(25)
        return f"Pomodoro session {self._session_count} started. 25 minutes of focus. Break follows."
