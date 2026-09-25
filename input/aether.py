# AETHER — Greek — upper atmosphere, medium of all signals
# Connectivity: WiFi, network status, internet checks

import subprocess
import socket
import requests
from core.marduk import OdinModule


class Aether(OdinModule):
    MODULE_NAME = "AETHER"
    LAYER = "INPUT"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "check_internet",
                "description": "Check if the internet connection is active",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "get_network_info",
                "description": "Get current WiFi and network connection information",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "ping_host",
                "description": "Ping a host to check reachability",
                "parameters": {
                    "host": {"type": "string", "description": "Hostname or IP to ping"}
                },
                "required": ["host"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "check_internet": self._check_internet,
            "get_network_info": self._network_info,
            "ping_host": self._ping,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[AETHER] Error: {e}"
        return f"[AETHER] Unknown skill: {skill_name}"

    def _check_internet(self) -> str:
        try:
            requests.get("https://1.1.1.1", timeout=3)
            return "Internet connection is active."
        except Exception:
            return "No internet connection detected."

    def _network_info(self) -> str:
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            result = subprocess.run(
                ["netsh", "wlan", "show", "interfaces"],
                capture_output=True, text=True, timeout=5
            )
            ssid_line = next(
                (l for l in result.stdout.splitlines() if "SSID" in l and "BSSID" not in l), ""
            )
            ssid = ssid_line.split(":")[-1].strip() if ssid_line else "Unknown"
            return f"Hostname: {hostname}. Local IP: {local_ip}. WiFi: {ssid}."
        except Exception as e:
            return f"Could not retrieve network info: {e}"

    def _ping(self, host: str = "") -> str:
        try:
            result = subprocess.run(
                ["ping", "-n", "2", host],
                capture_output=True, text=True, timeout=8
            )
            if "TTL=" in result.stdout:
                return f"{host} is reachable."
            return f"{host} is not responding."
        except Exception as e:
            return f"Ping failed: {e}"
