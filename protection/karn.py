# KARN — Hindu hero — born armored, invincible defender
# Defense: primary security shield, firewall monitoring, threat detection

import subprocess
import os
import psutil
from datetime import datetime
from core.marduk import OdinModule


class Karn(OdinModule):
    MODULE_NAME = "KARN"
    LAYER = "PROTECTION"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "scan_open_ports",
                "description": "Scan for open network connections and listening ports",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "check_firewall",
                "description": "Check Windows Firewall status",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "list_startup_programs",
                "description": "List programs that run at Windows startup",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "check_suspicious_processes",
                "description": "Check for high CPU or unusual processes",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "lock_screen",
                "description": "Lock the Windows screen immediately",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "scan_open_ports": self._open_ports,
            "check_firewall": self._firewall,
            "list_startup_programs": self._startup,
            "check_suspicious_processes": self._suspicious,
            "lock_screen": self._lock,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[KARN] Error: {e}"
        return f"[KARN] Unknown skill: {skill_name}"

    def _open_ports(self) -> str:
        connections = psutil.net_connections(kind="inet")
        listening = [
            f"Port {c.laddr.port} ({c.status})"
            for c in connections if c.status == "LISTEN"
        ]
        if not listening:
            return "No ports currently listening."
        return f"Listening ports: {', '.join(listening[:10])}."

    def _firewall(self) -> str:
        result = subprocess.run(
            ["netsh", "advfirewall", "show", "allprofiles", "state"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        return " | ".join(lines[:6]) or "Could not retrieve firewall status."

    def _startup(self) -> str:
        result = subprocess.run(
            ["wmic", "startup", "get", "Caption,Command"],
            capture_output=True, text=True, timeout=8
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        if len(lines) <= 1:
            return "No startup programs found."
        return "Startup programs: " + "; ".join(lines[1:6])

    def _suspicious(self) -> str:
        procs = sorted(psutil.process_iter(["name", "cpu_percent"]), key=lambda p: -p.info["cpu_percent"])
        high = [f"{p.info['name']} ({p.info['cpu_percent']:.1f}%)" for p in procs[:5] if p.info["cpu_percent"] > 5]
        if not high:
            return "All processes appear normal. No unusual CPU activity."
        return "High CPU processes: " + ", ".join(high)

    def _lock(self) -> str:
        os.system("rundll32.exe user32.dll,LockWorkStation")
        return "Screen locked."
