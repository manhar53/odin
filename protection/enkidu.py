# ENKIDU — Sumerian hero — sworn protector of Gilgamesh
# Privacy: data privacy, permissions, access guard, sensitive data protection

import os
import json
import base64
import hashlib
from datetime import datetime
from core.marduk import OdinModule

# AES-128-CBC via cryptography.Fernet keeps the vault unreadable at rest. The
# key is derived from the user's Windows machine GUID so the file can't be
# decrypted off-machine, while still requiring no password from the user.
# This is "encryption at rest against casual disk access", not "encryption
# against an attacker who has compromised the running OS" — they could just
# read the GUID too. For stronger guarantees we'd add a passphrase prompt.
try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


def _machine_key() -> bytes | None:
    """Derive a Fernet-shaped key from the Windows MachineGuid registry value
    (HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid). Stable across
    reboots, unique per machine. Returns None on non-Windows or if the
    registry read fails (then ENKIDU falls back to plaintext)."""
    try:
        import winreg  # Windows-only stdlib module
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as k:
            guid, _ = winreg.QueryValueEx(k, "MachineGuid")
        digest = hashlib.sha256(("ODIN-ENKIDU::" + guid).encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest)
    except Exception:
        return None


class Enkidu(OdinModule):
    MODULE_NAME = "ENKIDU"
    LAYER = "PROTECTION"

    def __init__(self, config: dict):
        super().__init__(config)
        self._vault_path = "data/knowledge/.vault.enc"
        self._legacy_path = "data/knowledge/.vault.json"
        self._fernet: Fernet | None = None
        if _HAS_CRYPTO:
            key = _machine_key()
            if key:
                self._fernet = Fernet(key)
        self._vault: dict = self._load_vault()
        # Migrate plaintext legacy vault on first encrypted run, then wipe it.
        if self._fernet and os.path.exists(self._legacy_path):
            try:
                with open(self._legacy_path) as f:
                    legacy = json.load(f)
                if legacy:
                    self._vault.update(legacy)
                    self._save_vault()
                # Best-effort secure wipe — overwrite then delete.
                size = os.path.getsize(self._legacy_path)
                with open(self._legacy_path, "wb") as f:
                    f.write(b"\x00" * size)
                os.remove(self._legacy_path)
            except Exception as e:
                self._log.warning(f"legacy vault migration failed: {e}")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "store_secret",
                "description": "Securely store a private note, credential label, or sensitive key",
                "parameters": {
                    "label": {"type": "string", "description": "Label for this secret"},
                    "value": {"type": "string", "description": "The secret value to store"}
                },
                "required": ["label", "value"],
                "internal_only": True
            },
            {
                "name": "retrieve_secret",
                "description": "Retrieve a stored secret by its label",
                "parameters": {
                    "label": {"type": "string", "description": "Label of the secret"}
                },
                "required": ["label"],
                "internal_only": True
            },
            {
                "name": "delete_secret",
                "description": "Delete a stored secret",
                "parameters": {
                    "label": {"type": "string", "description": "Label to delete"}
                },
                "required": ["label"],
                "internal_only": True
            },
            {
                "name": "list_secret_labels",
                "description": "List the labels of all stored secrets (values not shown)",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "check_data_exposure",
                "description": "Check if sensitive ODIN data files are accessible",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "store_secret": self._store,
            "retrieve_secret": self._retrieve,
            "delete_secret": self._delete,
            "list_secret_labels": self._list_labels,
            "check_data_exposure": self._check_exposure,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[ENKIDU] Error: {e}"
        return f"[ENKIDU] Unknown skill: {skill_name}"

    def _store(self, label: str = "", value: str = "") -> str:
        self._vault[label] = {"value": value, "stored": datetime.now().isoformat()}
        self._save_vault()
        return f"Secret '{label}' secured by ENKIDU."

    def _retrieve(self, label: str = "") -> str:
        entry = self._vault.get(label)
        if entry:
            return f"{label}: {entry['value']}"
        return f"No secret found under label '{label}'."

    def _delete(self, label: str = "") -> str:
        if label in self._vault:
            del self._vault[label]
            self._save_vault()
            return f"Secret '{label}' destroyed."
        return f"No secret found: '{label}'."

    def _list_labels(self) -> str:
        if not self._vault:
            return "No secrets stored."
        return "Stored labels: " + ", ".join(self._vault.keys())

    def _check_exposure(self) -> str:
        sensitive_paths = [
            self._vault_path,
            "data/knowledge/facts.json",
            "data/knowledge/preferences.json",
            "config.yaml"
        ]
        report = []
        for path in sensitive_paths:
            if os.path.exists(path):
                report.append(f"{path}: present")
            else:
                report.append(f"{path}: not found")
        return "Data exposure check: " + " | ".join(report)

    def _load_vault(self) -> dict:
        if not os.path.exists(self._vault_path):
            return {}
        if not self._fernet:
            # Fallback: if cryptography is missing, try legacy plaintext.
            try:
                with open(self._vault_path) as f:
                    return json.load(f)
            except Exception:
                return {}
        try:
            with open(self._vault_path, "rb") as f:
                blob = f.read()
            return json.loads(self._fernet.decrypt(blob).decode("utf-8"))
        except (InvalidToken, ValueError, json.JSONDecodeError) as e:
            self._log.warning(f"vault decrypt failed ({e}); starting empty.")
            return {}

    def _save_vault(self):
        os.makedirs(os.path.dirname(self._vault_path), exist_ok=True)
        payload = json.dumps(self._vault, indent=2).encode("utf-8")
        if self._fernet:
            blob = self._fernet.encrypt(payload)
            with open(self._vault_path, "wb") as f:
                f.write(blob)
        else:
            # Plaintext fallback when cryptography is unavailable. Logged so the
            # user notices and installs the dep.
            self._log.warning("cryptography missing — vault written in plaintext.")
            with open(self._vault_path, "wb") as f:
                f.write(payload)
