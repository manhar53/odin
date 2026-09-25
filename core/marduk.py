# MARDUK — Babylonian — created order from chaos, supreme orchestrator
# Silent message bus and orchestrator for all ODIN modules

import json
import logging
import os
from datetime import datetime

logging.basicConfig(
    filename="data/logs/odin.log",
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s"
)

# Structured failure log — one JSON object per line. Used by SHERLOCK to
# build a weekly digest of which skills break most. Distinct from the
# free-text odin.log so we can grep / aggregate without parsing prose.
_FAILURE_LOG_PATH = "data/logs/failures.jsonl"


def _looks_like_error(result: str) -> bool:
    """Module error returns follow `[MODULE] Error: ...` by convention.
    Also catch the "Unknown skill" path and the dispatch-miss path."""
    if not isinstance(result, str):
        return False
    s = result.strip()
    if not s:
        return False
    return (
        " Error:" in s[:60]
        or "Unknown skill:" in s
        or s.startswith("MARDUK: No module")
        or s.startswith("MARDUK: Module ")
    )


def _record_failure(skill: str, args: dict, result: str, source: str):
    try:
        os.makedirs(os.path.dirname(_FAILURE_LOG_PATH), exist_ok=True)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": source,    # "dispatch" or "route"
            "skill": skill,
            "args": args,
            "result": result[:500],
        }
        with open(_FAILURE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # never let logging crash the bus


class OdinModule:
    """Base class for all ODIN modules."""
    MODULE_NAME = "BASE"
    LAYER = "BASE"

    def __init__(self, config: dict):
        self.config = config
        self.marduk = None
        self._log = logging.getLogger(self.MODULE_NAME)

    @property
    def skills(self) -> list[dict]:
        return []

    def execute(self, skill_name: str, args: dict) -> str:
        return f"[{self.MODULE_NAME}] No executor defined for: {skill_name}"

    def send(self, to_module: str, action: str, **data) -> str:
        """Route a message to another module through MARDUK."""
        if self.marduk:
            return self.marduk.route(self.MODULE_NAME, to_module, action, data)
        return f"[{self.MODULE_NAME}] MARDUK not connected."


class Marduk:
    """
    MARDUK — The silent orchestrator.
    All inter-module communication flows through here.
    Never exposed to the user.
    """

    def __init__(self, config: dict):
        self.config = config
        self._modules: dict[str, OdinModule] = {}
        self._skill_map: dict[str, str] = {}
        # skill -> set of declared parameter names. Used by dispatch() to drop
        # arguments the local LLM hallucinated (e.g. find_files(location=...)
        # when the schema declares 'directory'), so a slightly-malformed tool
        # call degrades to a working call instead of a TypeError.
        self._skill_params: dict[str, set] = {}
        self._log = logging.getLogger("MARDUK")
        # UI observers: callable(module_name, skill_name) fired on every
        # dispatch/route so ASGARD's orrery can light the active planet.
        # Listener failures must never break the dispatch path.
        self._dispatch_listeners: list = []
        print("[MARDUK] Orchestrator online — order from chaos.")

    def add_dispatch_listener(self, fn):
        if callable(fn):
            self._dispatch_listeners.append(fn)

    def _notify_dispatch(self, module_name: str, skill_name: str):
        for fn in self._dispatch_listeners:
            try:
                fn(module_name, skill_name)
            except Exception:
                pass

    def register(self, module: OdinModule):
        module.marduk = self
        self._modules[module.MODULE_NAME] = module
        for skill in module.skills:
            self._skill_map[skill["name"]] = module.MODULE_NAME
            self._skill_params[skill["name"]] = set((skill.get("parameters") or {}).keys())
        count = len(module.skills)
        print(f"[MARDUK] Registered [{module.MODULE_NAME}] — {module.LAYER} layer — {count} skill(s)")
        self._log.info(f"Registered [{module.MODULE_NAME}] {count} skills")

    def dispatch(self, skill_name: str, args: dict) -> str:
        """GILGAMESH → MARDUK → Module"""
        module_name = self._skill_map.get(skill_name)
        if not module_name:
            result = f"MARDUK: No module handles skill '{skill_name}'"
            _record_failure(skill_name, args, result, "dispatch")
            return result
        # Drop hallucinated/unknown kwargs from the LLM so a near-miss tool
        # call still works instead of raising "unexpected keyword argument".
        # Only when the skill declares params (some take none / are freeform);
        # only on the LLM path (dispatch), never on internal route().
        declared = self._skill_params.get(skill_name)
        if declared and isinstance(args, dict):
            extra = [k for k in args if k not in declared]
            if extra:
                self._log.info(f"dispatch: {skill_name} dropping unknown args {extra} "
                               f"(declared: {sorted(declared)})")
                args = {k: v for k, v in args.items() if k in declared}
        self._log.info(f"dispatch: {skill_name} → [{module_name}] args={args}")
        self._notify_dispatch(module_name, skill_name)
        result = self._modules[module_name].execute(skill_name, args)
        if _looks_like_error(result):
            _record_failure(skill_name, args, result, "dispatch")
        return result

    def route(self, from_module: str, to_module: str, action: str, data: dict) -> str:
        """Module → MARDUK → Module (inter-module messaging)"""
        if to_module not in self._modules:
            result = f"MARDUK: Module [{to_module}] not registered"
            _record_failure(action, data, result, "route")
            return result
        self._log.info(f"route: [{from_module}] → [{to_module}].{action}")
        self._notify_dispatch(to_module, action)
        result = self._modules[to_module].execute(action, data)
        if _looks_like_error(result):
            _record_failure(action, data, result, "route")
        return result

    def get_all_tools(self) -> list[dict]:
        """Return all skill definitions in Ollama tool-calling format. Skips skills marked internal_only."""
        tools = []
        for module in self._modules.values():
            for skill in module.skills:
                if skill.get("internal_only"):
                    continue
                tools.append({
                    "type": "function",
                    "function": {
                        "name": skill["name"],
                        "description": skill["description"],
                        "parameters": {
                            "type": "object",
                            "properties": skill.get("parameters", {}),
                            "required": skill.get("required", [])
                        }
                    }
                })
        return tools

    def get_module(self, name: str) -> OdinModule | None:
        return self._modules.get(name)

    def status(self) -> dict:
        return {name: len(mod.skills) for name, mod in self._modules.items()}
