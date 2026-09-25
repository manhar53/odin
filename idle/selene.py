# SELENE — Greek — goddess of the moon, rules the night
# Night Mode: background processes, sleep mode, quiet hours, idle state.
# Also: autonomous learning drain — pops topics from
# data/knowledge/research_queue.json during idle and calls SARASWATI to
# research them in the background. Results land in the vault automatically.

import json
import os
import threading
import time
from datetime import datetime
from core.marduk import OdinModule


_QUEUE_PATH = "data/knowledge/research_queue.json"


class Selene(OdinModule):
    MODULE_NAME = "SELENE"
    LAYER = "IDLE"

    def __init__(self, config: dict):
        super().__init__(config)
        self._night_mode = False
        self._quiet_start = 23   # 11 PM
        self._quiet_end = 7      # 7 AM
        cfg = config.get("selene", {})
        # Autonomous research interval — how often to drain one item from
        # the queue. 30 min keeps quota use moderate; topic queue is only
        # populated when ODIN encounters a real knowledge gap.
        self._auto_research_interval = int(cfg.get("auto_research_interval_seconds", 1800))
        self._auto_research_enabled = bool(cfg.get("auto_research_enabled", True))

        # Memory Tree auto-summary — walks the vault bottom-up, refreshes
        # stale _summary.md files via SARASWATI. Default cadence 1 hr; quiet
        # enough that cloud quota isn't burned, frequent enough that the
        # rollup is current the next morning. Each pass is a no-op if no
        # files changed since last summary.
        self._memory_tree_interval = int(cfg.get("memory_tree_interval_seconds", 3600))
        self._memory_tree_enabled = bool(cfg.get("memory_tree_enabled", True))

        # External-source ingest (Drive, Gmail). Lower cadence than research
        # because each pass costs API calls + cloud summarization.
        self._ingest_interval = int(cfg.get("ingest_interval_seconds", 1200))   # 20 min
        self._ingest_drive_enabled = bool(cfg.get("ingest_drive_enabled", True))
        self._ingest_gmail_enabled = bool(cfg.get("ingest_gmail_enabled", True))

        self._stop = threading.Event()
        if self._auto_research_enabled:
            self._auto_thread = threading.Thread(
                target=self._auto_research_loop, daemon=True, name="SELENE-research"
            )
            self._auto_thread.start()
        if self._memory_tree_enabled:
            threading.Thread(target=self._memory_tree_loop, daemon=True,
                             name="SELENE-memory-tree").start()
        if self._ingest_drive_enabled or self._ingest_gmail_enabled:
            threading.Thread(target=self._ingest_loop, daemon=True,
                             name="SELENE-ingest").start()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "activate_night_mode",
                "description": "Activate night mode — quieter responses, dimmed activity",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "deactivate_night_mode",
                "description": "Deactivate night mode and resume normal operation",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "goodnight",
                "description": "ODIN says goodnight, saves session, enters night mode",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "get_idle_status",
                "description": "Check if ODIN is currently in night or idle mode",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_quiet_hours",
                "description": "Set quiet hours when ODIN reduces activity",
                "parameters": {
                    "start_hour": {"type": "integer", "description": "Hour to start quiet mode (0-23)"},
                    "end_hour": {"type": "integer", "description": "Hour to end quiet mode (0-23)"}
                },
                "required": ["start_hour", "end_hour"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "activate_night_mode": self._activate,
            "deactivate_night_mode": self._deactivate,
            "goodnight": self._goodnight,
            "get_idle_status": self._status,
            "set_quiet_hours": self._set_quiet,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[SELENE] Error: {e}"
        return f"[SELENE] Unknown skill: {skill_name}"

    def is_quiet_time(self) -> bool:
        hour = datetime.now().hour
        if self._quiet_start > self._quiet_end:
            return hour >= self._quiet_start or hour < self._quiet_end
        return self._quiet_start <= hour < self._quiet_end

    def _activate(self) -> str:
        self._night_mode = True
        if self.marduk:
            self.send("MERLIN", "set_mode", mode="night")
        return "Night mode activated. I'll keep things quiet."

    def _deactivate(self) -> str:
        self._night_mode = False
        if self.marduk:
            self.send("MERLIN", "clear_mode", mode="night")
        return "Night mode deactivated. Good morning."

    def _goodnight(self) -> str:
        # Save session via THOTH
        if self.marduk:
            thoth = self.marduk.get_module("THOTH")
            if thoth:
                thoth.save_session()
        self._activate()
        now = datetime.now()
        return (f"Goodnight. Session saved. It's {now.strftime('%I:%M %p')}. "
                f"All systems entering night mode. Rest well.")

    def _status(self) -> str:
        if self._night_mode:
            return "Night mode is active."
        if self.is_quiet_time():
            return f"Quiet hours are active ({self._quiet_start}:00 - {self._quiet_end}:00)."
        return "ODIN is fully active. No idle mode running."

    def _set_quiet(self, start_hour: int = 23, end_hour: int = 7) -> str:
        self._quiet_start = start_hour
        self._quiet_end = end_hour
        return f"Quiet hours set: {start_hour}:00 to {end_hour}:00."

    # === Autonomous research drain =====================================
    # Every `auto_research_interval_seconds`, pop one item off the research
    # queue and hand it to SARASWATI.deep_research. The result writes to
    # the vault automatically. Skip when ODIN is actively conversing
    # (IRIS speaking or HEIMDALL handling).
    def _auto_research_loop(self):
        # Stagger startup so we don't race boot.
        time.sleep(60)
        while not self._stop.wait(self._auto_research_interval):
            try:
                self._drain_one()
            except Exception as e:
                self._log.warning(f"auto-research drain failed: {e}")

    def _drain_one(self):
        if not os.path.exists(_QUEUE_PATH):
            return
        try:
            with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
                queue = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        if not queue:
            return
        # Don't fire while ODIN is mid-conversation.
        if self.marduk:
            iris = self.marduk.get_module("IRIS")
            if iris and hasattr(iris, "is_speaking") and iris.is_speaking():
                return
        # Pop the first item.
        item = queue.pop(0)
        topic = (item or {}).get("topic", "").strip()
        if not topic:
            self._save_queue(queue)
            return
        # Skip if the vault already has a recent note on this topic.
        if self._already_researched(topic):
            print(f"[SELENE] Skipping queued '{topic}' — vault already has it.")
            self._save_queue(queue)
            return
        # Hand off to SARASWATI. The deep_research call writes to the vault
        # itself; we don't need to handle the response here.
        sara = self.marduk.get_module("SARASWATI") if self.marduk else None
        if not sara:
            self._save_queue(queue)
            return
        print(f"[SELENE] Auto-researching '{topic}' in background...")
        try:
            result = sara.execute("deep_research", {"topic": topic})
            print(f"[SELENE] Researched '{topic}': {result[:100]}")
        except Exception as e:
            self._log.warning(f"auto-research of '{topic}' failed: {e}")
        finally:
            self._save_queue(queue)

    def _already_researched(self, topic: str) -> bool:
        """Check if the vault already has a note for this topic."""
        if not self.marduk:
            return False
        nabu = self.marduk.get_module("NABU")
        if not nabu:
            return False
        try:
            hits = nabu.execute("search_vault", {"query": topic, "limit": 1})
            return bool(hits and not hits.startswith(("Empty", "No", "Nothing", "[NABU]")))
        except Exception:
            return False

    @staticmethod
    def _save_queue(queue: list):
        try:
            os.makedirs(os.path.dirname(_QUEUE_PATH), exist_ok=True)
            with open(_QUEUE_PATH, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2)
        except OSError:
            pass

    # ─────────────────────────────────────────────────────────────────
    # Memory Tree loop — periodic NABU.build_memory_tree pass so the
    # hierarchical vault summary stays current.
    # ─────────────────────────────────────────────────────────────────
    def _memory_tree_loop(self):
        # Stagger start so we don't compete with research drain or boot init.
        time.sleep(90)
        while not self._stop.wait(self._memory_tree_interval):
            try:
                if not self.marduk:
                    continue
                # Skip while ODIN is mid-conversation — speech & LLM share quota.
                iris = self.marduk.get_module("IRIS")
                if iris and hasattr(iris, "is_speaking") and iris.is_speaking():
                    continue
                nabu = self.marduk.get_module("NABU")
                if not nabu:
                    continue
                result = nabu.execute("build_memory_tree", {"root": "ODIN"})
                # Only log if something actually refreshed — avoid spamming.
                if "0 folders refreshed" not in result:
                    print(f"[SELENE] {result}")
            except Exception as e:
                self._log.warning(f"memory-tree pass failed: {e}")

    # ─────────────────────────────────────────────────────────────────
    # External-source ingest loop — every N minutes (20 by default),
    # pulls recent Drive files + recent Gmail into vault summaries.
    # Each source delegates to its module's *_ingest_recent skill so
    # logic lives near the data, not here.
    # ─────────────────────────────────────────────────────────────────
    def _ingest_loop(self):
        # Stagger start so the first pass doesn't pile onto boot.
        time.sleep(120)
        while not self._stop.wait(self._ingest_interval):
            if not self.marduk:
                continue
            iris = self.marduk.get_module("IRIS")
            if iris and hasattr(iris, "is_speaking") and iris.is_speaking():
                continue

            if self._ingest_drive_enabled:
                try:
                    chitra = self.marduk.get_module("CHITRA")
                    if chitra and "drive_ingest_recent" in {s["name"] for s in chitra.skills}:
                        r = chitra.execute("drive_ingest_recent", {})
                        if r and "no new" not in r.lower():
                            print(f"[SELENE] {r}")
                except Exception as e:
                    self._log.warning(f"drive ingest pass failed: {e}")

            if self._ingest_gmail_enabled:
                try:
                    mercury = self.marduk.get_module("MERCURY")
                    if mercury and "gmail_ingest_recent" in {s["name"] for s in mercury.skills}:
                        r = mercury.execute("gmail_ingest_recent", {})
                        if r and "no new" not in r.lower():
                            print(f"[SELENE] {r}")
                except Exception as e:
                    self._log.warning(f"gmail ingest pass failed: {e}")
