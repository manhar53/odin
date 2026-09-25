# THOTH — Egyptian — god of knowledge and writing
# Memory store: conversations, preferences, user facts

import json
import os
from datetime import datetime
from core.marduk import OdinModule


class Thoth(OdinModule):
    MODULE_NAME = "THOTH"
    LAYER = "MEMORY"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("thoth", {})
        self.max_turns = cfg.get("max_turns", 20)
        self.data_path = cfg.get("data_path", "data/memory")
        self.knowledge_path = cfg.get("knowledge_path", "data/knowledge")
        # On boot, load the last N turns from the most recent session so
        # ODIN remembers context across restarts. Configurable; default 6
        # turns (3 user + 3 assistant) — enough for "what were we just
        # talking about" without bloating GIL's prompt.
        self.restore_turns = cfg.get("restore_turns", 6)
        # Auto-save the live conversation every N stored messages. Cheap —
        # the file is tiny — and means a hard kill never loses the session.
        self.autosave_every = cfg.get("autosave_every", 4)
        self._autosave_counter = 0
        # Compression of old turns. When the live conversation exceeds
        # `max_turns * 2` messages, the oldest turns are summarised by
        # SARASWATI (cloud) into a single "what we discussed earlier" line,
        # which is then prepended to GIL's context on every subsequent turn.
        # Effect: GIL knows roughly what happened 50 turns ago without those
        # turns living in the active prompt — works around 8GB context.
        # Inspired by OpenJarvis's monitor_operative memory pattern.
        self.compress_after = cfg.get("compress_after_turns", 14)
        self.summary_path = os.path.join(self.data_path, "summary.txt")
        self._live_session_path = os.path.join(self.data_path, "session_current.json")
        os.makedirs(self.data_path, exist_ok=True)
        os.makedirs(self.knowledge_path, exist_ok=True)
        self._conversation: list[dict] = self._load_recent_session()
        self._facts: dict = self._load_facts()
        self._summary: str = self._load_summary()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "store_message",
                "description": "Store a conversation message in memory",
                "parameters": {
                    "role": {"type": "string", "description": "user or assistant"},
                    "content": {"type": "string", "description": "message content"}
                },
                "required": ["role", "content"],
                "internal_only": True
            },
            {
                "name": "get_messages",
                "description": "Retrieve recent conversation history",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "remember",
                "description": "Store a persistent fact or user preference",
                "parameters": {
                    "key": {"type": "string", "description": "fact label"},
                    "value": {"type": "string", "description": "fact content"}
                },
                "required": ["key", "value"]
            },
            {
                "name": "recall",
                "description": "Recall a stored fact by key",
                "parameters": {
                    "key": {"type": "string", "description": "fact label to look up"}
                },
                "required": ["key"]
            },
            {
                "name": "list_memories",
                "description": "List all stored facts and preferences",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "clear_conversation",
                "description": "Clear the current conversation from memory",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str | list:
        _map = {
            "store_message": self._store_message,
            "get_messages": self._get_messages,
            "remember": self._remember,
            "recall": self._recall,
            "list_memories": self._list_memories,
            "clear_conversation": self._clear_conversation,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[THOTH] Error: {e}"
        return f"[THOTH] Unknown skill: {skill_name}"

    def _store_message(self, role: str, content: str) -> str:
        self._conversation.append({"role": role, "content": content})
        if len(self._conversation) > self.max_turns * 2:
            # Before truncating, compress the rolling-off section into
            # the long-term summary. Keep the LAST max_turns*2 verbatim.
            overflow = self._conversation[: -(self.max_turns * 2)]
            self._conversation = self._conversation[-(self.max_turns * 2):]
            if overflow:
                self._compress_into_summary(overflow)
        # Auto-save: a crash, force-quit, or laptop sleep mid-conversation no
        # longer wipes context. The live file is overwritten — only one copy.
        self._autosave_counter += 1
        if self._autosave_counter >= self.autosave_every:
            self._autosave_counter = 0
            self._save_live_session()
        return "stored"

    def _compress_into_summary(self, overflow: list[dict]):
        """Take messages that are rolling out of the active conversation
        and fold them into the long-term summary via SARASWATI (cloud).
        Best-effort: if cloud isn't configured or fails, fall back to a
        plain concatenation of user-message excerpts so we at least keep
        some breadcrumb of what happened."""
        if not overflow or not self.marduk:
            return
        sara = self.marduk.get_module("SARASWATI")
        existing = self._summary
        transcript = "\n".join(
            f"{m.get('role', '?')}: {m.get('content', '')[:300]}"
            for m in overflow
        )
        prompt = (
            "You are maintaining a rolling summary of an ongoing conversation "
            "between a user and ODIN, a local voice assistant. "
            "Update the previous summary with the new section below. "
            "Keep the result under 200 words, third-person, bullet-style — "
            "focus on user goals, decisions, ongoing tasks, and persistent facts. "
            "Drop trivial chat.\n\n"
            f"Previous summary:\n{existing or '(none yet)'}\n\n"
            f"New conversation section to fold in:\n{transcript}\n\n"
            "Return ONLY the updated summary."
        )
        new_summary = ""
        if sara and getattr(sara, "providers", None):
            try:
                resp = sara.execute("ask_ai", {"question": prompt})
                if resp and isinstance(resp, str) and not resp.startswith(("No cloud", "[", "Need ")):
                    new_summary = resp.strip()
            except Exception as e:
                self._log.warning(f"compression via cloud failed: {e}")
        if not new_summary:
            # Plain fallback — just collect user messages
            user_msgs = [m["content"][:120] for m in overflow if m.get("role") == "user"]
            if existing:
                new_summary = existing + "\n- (earlier) " + "; ".join(user_msgs[:5])
            else:
                new_summary = "- (earlier) " + "; ".join(user_msgs[:5])
            new_summary = new_summary[:1500]  # cap
        self._summary = new_summary
        self._save_summary()
        print(f"[THOTH] Compressed {len(overflow)} old messages into summary ({len(new_summary)} chars).")

    def get_summary(self) -> str:
        """Public accessor: GIL can inject this as background context."""
        return self._summary

    def _load_summary(self) -> str:
        if not os.path.exists(self.summary_path):
            return ""
        try:
            with open(self.summary_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_summary(self):
        try:
            with open(self.summary_path, "w", encoding="utf-8") as f:
                f.write(self._summary)
        except OSError as e:
            self._log.warning(f"summary save failed: {e}")

    def _save_live_session(self):
        try:
            with open(self._live_session_path, "w", encoding="utf-8") as f:
                json.dump(self._conversation, f, indent=2)
        except OSError as e:
            self._log.warning(f"live session save failed: {e}")

    def _load_recent_session(self) -> list[dict]:
        """On boot, pull the tail of the last live session so context survives
        across restarts. Returns the last `restore_turns` messages, or [] if
        nothing to restore."""
        if not os.path.exists(self._live_session_path):
            return []
        try:
            with open(self._live_session_path, "r", encoding="utf-8") as f:
                msgs = json.load(f)
            if not isinstance(msgs, list) or not msgs:
                return []
            tail = msgs[-self.restore_turns:]
            print(f"[THOTH] Restored {len(tail)} message(s) from previous session.")
            return tail
        except (OSError, json.JSONDecodeError) as e:
            self._log.warning(f"session restore failed: {e}")
            return []

    def _get_messages(self) -> list[dict]:
        return self._conversation.copy()

    def _remember(self, key: str, value: str) -> str:
        self._facts[key] = {"value": value, "stored": datetime.now().isoformat()}
        self._save_facts()
        return f"Remembered: {key} = {value}"

    def _recall(self, key: str) -> str:
        entry = self._facts.get(key)
        if entry:
            return f"{key}: {entry['value']}"
        return f"Nothing stored for '{key}'."

    def _list_memories(self) -> str:
        # Pull both explicit facts (THOTH.remember) AND auto-learned
        # preferences (PROMETHEUS). Otherwise auto-learn outputs are
        # invisible to "list my memories" — and the user can't tell the
        # auto-learn loop is working.
        lines = []
        if self._facts:
            for k, v in self._facts.items():
                lines.append(f"{k}: {v.get('value', '')}")
        if self.marduk:
            prom = self.marduk.get_module("PROMETHEUS")
            if prom and hasattr(prom, "_prefs"):
                for k, v in prom._prefs.items():
                    mark = " (learned)" if v.get("auto") else ""
                    lines.append(f"{k}: {v.get('value', '')}{mark}")
        if not lines:
            return "No memories stored yet."
        return "; ".join(lines)

    def _clear_conversation(self) -> str:
        self._conversation = []
        return "Conversation cleared."

    def save_session(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.data_path, f"session_{ts}.json")
        with open(path, "w") as f:
            json.dump(self._conversation, f, indent=2)
        # Archive done — clear the live tail so the next boot starts fresh.
        try:
            if os.path.exists(self._live_session_path):
                os.remove(self._live_session_path)
        except OSError:
            pass

    def _load_facts(self) -> dict:
        path = os.path.join(self.knowledge_path, "facts.json")
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
        return {}

    def _save_facts(self):
        path = os.path.join(self.knowledge_path, "facts.json")
        with open(path, "w") as f:
            json.dump(self._facts, f, indent=2)
