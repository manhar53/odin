# OSIRIS — Egyptian — god of resurrection and restoration
# Backup: memory backup, config backup, recovery, redundancy

import shutil
import os
import json
from datetime import datetime
from core.marduk import OdinModule


class Osiris(OdinModule):
    MODULE_NAME = "OSIRIS"
    LAYER = "PROTECTION"

    def __init__(self, config: dict):
        super().__init__(config)
        self.backup_path = config.get("osiris", {}).get("backup_path", "data/backups")
        os.makedirs(self.backup_path, exist_ok=True)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "backup_all",
                "description": "Create a full backup of all ODIN data and config",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "backup_memory",
                "description": "Back up conversation history and knowledge",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "list_backups",
                "description": "List all available backups",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "restore_backup",
                "description": "Restore from a specific backup",
                "parameters": {
                    "backup_name": {"type": "string", "description": "Name of the backup to restore"}
                },
                "required": ["backup_name"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "backup_all": self._backup_all,
            "backup_memory": self._backup_memory,
            "list_backups": self._list_backups,
            "restore_backup": self._restore,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[OSIRIS] Error: {e}"
        return f"[OSIRIS] Unknown skill: {skill_name}"

    def _backup_all(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(self.backup_path, f"full_{ts}")
        os.makedirs(backup_dir)
        copied = []
        for src in ["data/knowledge", "data/memory", "config.yaml"]:
            if os.path.exists(src):
                dst = os.path.join(backup_dir, os.path.basename(src))
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                copied.append(src)
        manifest = {"created": ts, "sources": copied}
        with open(os.path.join(backup_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f, indent=2)
        return f"Full backup created: full_{ts}. {len(copied)} source(s) backed up."

    def _backup_memory(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(self.backup_path, f"memory_{ts}")
        os.makedirs(backup_dir)
        for src in ["data/knowledge", "data/memory"]:
            if os.path.exists(src):
                dst = os.path.join(backup_dir, os.path.basename(src))
                shutil.copytree(src, dst)
        return f"Memory backup created: memory_{ts}."

    def _list_backups(self) -> str:
        backups = sorted(os.listdir(self.backup_path)) if os.path.exists(self.backup_path) else []
        if not backups:
            return "No backups found."
        return "Available backups: " + ", ".join(backups[-10:])

    def _restore(self, backup_name: str = "") -> str:
        backup_dir = os.path.join(self.backup_path, backup_name)
        if not os.path.exists(backup_dir):
            return f"Backup '{backup_name}' not found."
        manifest_path = os.path.join(backup_dir, "manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path) as f:
                manifest = json.load(f)
            sources = manifest.get("sources", [])
            for src_name in sources:
                src = os.path.join(backup_dir, os.path.basename(src_name))
                if os.path.exists(src):
                    if os.path.isdir(src_name):
                        shutil.rmtree(src_name, ignore_errors=True)
                        shutil.copytree(src, src_name)
                    else:
                        shutil.copy2(src, src_name)
            return f"Restored from backup: {backup_name}."
        return f"Backup found but no manifest. Manual restore required."
