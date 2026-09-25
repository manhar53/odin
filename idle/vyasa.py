# VYASA — Vedavyasa, Hindu sage who compiled the scattered Vedas into
# structured knowledge. In ODIN, VYASA is the self-testing loop: he
# replays past commands, generates new ones via cloud, simulates them
# safely, detects failures, asks cloud for fix proposals, logs everything
# for user review, and verifies fixes on re-run.
#
# Three sources of test commands (mixed):
#   1. Recent FAILURES — replay them to verify that a fix has landed.
#   2. Recent SUCCESSES — make sure new code didn't regress them.
#   3. NEW cloud-generated commands — keeps coverage growing organically.
#
# Safety, the most important part:
#   - VYASA dispatches ONLY skills on an allowlist of READ-ONLY operations
#     (lookups, fetches, status). Anything that changes state — sending
#     messages, opening apps, controlling volume, executing browser actions,
#     pursuing goals — is logged as 'would-execute' but NEVER dispatched.
#   - Off by default. `start_self_test` voice command opts in.
#   - Skips when HEIMDALL is busy (user is mid-conversation).
#   - Quota-aware: pauses when SARASWATI's daily limits are near.
#
# The "with your help, passively corrects faults" flow:
#   1. VYASA runs a simulation, detects a failure.
#   2. Cloud (SARASWATI code_chain) writes a fix proposal (regex change,
#      new fast-route, etc.) — saved to data/knowledge/vyasa_proposals.jsonl
#      AND a human-readable report at data/logs/vyasa_session.md.
#   3. User reviews `simulation_report` (voice or file). If they want a
#      fix applied, they bring it to me (Claude) in chat and I patch it.
#   4. Next simulation cycle re-runs the failing command. If it passes,
#      the proposal is marked status="verified". Otherwise status stays "open".
#
# VYASA never patches code itself. Auto-modifying heimdall.py from a
# daemon thread is the kind of footgun that erases a weekend's work.

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from core.marduk import OdinModule

from input.heimdall import (
    _FAST_ROUTES,
    _IDENTITY_QUESTION,
    _CAPABILITY_QUESTION,
    _CAPABILITIES_LINE,
    _SMALL_TALK,
    _pick_identity_response,
    _strip_self_corrections,
    _normalize_email_speech,
    _normalize_url_speech,
)


# Read-only skills VYASA may dispatch directly. Everything else is logged
# as "would-execute" only. KEEP CONSERVATIVE — false positives here mean
# VYASA might send a real WhatsApp message during a self-test loop.
_SAFE_SIMULATION_SKILLS = frozenset({
    # Time / status
    "get_time", "get_odin_uptime",
    "get_system_stats", "check_internet", "run_diagnostics",
    "check_ollama", "check_modules", "check_disk_health",
    "weekly_digest", "usage_audit",
    "get_brightness", "get_volume",
    # Memory / facts (READ-only — recall/list, not remember)
    "recall", "list_memories", "search_memory", "get_last_exchange",
    "summarize_session",
    # Vault / Drive (READ-only)
    "search_vault", "list_directory", "drive_search", "drive_recent",
    "drive_fetch", "fetch_page",
    # Working memory + file locate (find WITHOUT opening) + news digest
    "recall_learned", "locate_file", "world_news",
    # Environment / weather / facts
    "get_weather", "get_weather_here", "get_my_location",
    "air_quality", "track_flight", "public_holidays",
    # Wiki / web search (no side effects)
    "wiki_lookup", "search_web",
    # Currency / crypto
    "crypto_price", "convert_currency",
    # Health / spending read
    "get_health_summary", "get_spending_summary", "get_budget_status",
    "list_reminders",
    # Personality (safe to fetch)
    "get_persona", "get_tone", "tell_joke",
    # MIMIR — diagnostic itself is read-only.
    "diagnose", "propose_fixes", "explain_last_failure",
})


_PROPOSAL_LOG = "data/knowledge/vyasa_proposals.jsonl"
_SESSION_LOG = "data/logs/vyasa_session.md"
_GENERATED_CACHE = "data/knowledge/vyasa_generated_commands.json"


_SYSTEM_PROMPT_GEN = """You generate realistic voice commands a user might say to a personal voice AI named ODIN. The user is a developer; ODIN has skills for weather, reminders, web search, wikipedia, currency, crypto prices, file search, vault notes, drive search, and general questions.

Rules:
- Output 8 commands, one per line. No numbering, no quotes, no preamble.
- Vary phrasing: questions, imperatives, partial sentences.
- Include both common ("what's the weather", "remind me to drink water") and edge cases ("read https voters dot eci dot gov dot in", "weather in Tokyo right now").
- Avoid destructive intents (no "buy", "send", "delete", "shutdown")."""


_SYSTEM_PROMPT_FIX = """You are debugging ODIN, a voice AI. Given a failing command and the result, propose a SPECIFIC fix.

Output rules:
- Reply with VALID JSON only.
- Schema: {"category": "missed_fast_route|wrong_regex|api_down|empty_result|other", "diagnosis": "<1-2 sentences>", "proposed_fix": "<concrete change — regex, code, or config>", "confidence": "high|medium|low"}
- 'proposed_fix' must be actionable: name the file, the regex, or the config value to change.
- If the failure is environmental (API down, network), say so plainly with low confidence."""


class Vyasa(OdinModule):
    MODULE_NAME = "VYASA"
    LAYER = "IDLE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("vyasa", {})
        self.interval_seconds = int(cfg.get("interval_seconds", 300))   # 5 min
        self.max_per_day = int(cfg.get("max_per_day", 50))
        self.auto_propose_fixes = bool(cfg.get("auto_propose_fixes", True))
        self.generate_new_every = int(cfg.get("generate_new_every", 5))  # every N sims, ask cloud
        self.session_log_path = cfg.get("session_log_path", _SESSION_LOG)
        self.proposal_log_path = cfg.get("proposal_log_path", _PROPOSAL_LOG)
        self.generated_cache_path = cfg.get("generated_cache_path", _GENERATED_CACHE)
        os.makedirs(os.path.dirname(self.session_log_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.proposal_log_path), exist_ok=True)

        # Loop state
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sims_today = 0
        self._sims_today_date = datetime.now().date()
        self._sim_count = 0   # total since loop start
        self._heimdall = None  # injected post-boot for busy-check

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "start_self_test",
                "description": (
                    "Start VYASA's self-testing loop. ODIN will replay past "
                    "commands, generate new ones via cloud, and simulate them "
                    "safely. Failures + cloud-proposed fixes log to "
                    f"{self.session_log_path}. READ-ONLY skills only — "
                    "VYASA never sends messages, opens apps, or changes state."
                ),
                "parameters": {
                    "interval_seconds": {"type": "integer", "description": f"Seconds between simulations (default {self.interval_seconds})"},
                    "max_per_day": {"type": "integer", "description": f"Cap simulations per day (default {self.max_per_day})"},
                },
                "required": [],
            },
            {
                "name": "stop_self_test",
                "description": "Stop the self-testing loop.",
                "parameters": {},
                "required": [],
            },
            {
                "name": "simulation_report",
                "description": (
                    "Show VYASA's recent self-test activity: how many sims ran, "
                    "how many passed, how many open fix proposals, how many "
                    "verified. Use to answer 'what has odin been testing'."
                ),
                "parameters": {
                    "hours": {"type": "integer", "description": "Look-back window (default 24)"},
                },
                "required": [],
            },
            {
                "name": "simulate_once",
                "description": (
                    "Run a single self-test simulation now. If `command` is "
                    "omitted, VYASA picks one (failure replay > success replay "
                    "> cloud-generated). Returns the result."
                ),
                "parameters": {
                    "command": {"type": "string", "description": "Specific command to simulate (optional)"},
                },
                "required": [],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "start_self_test":     self._start_loop,
            "stop_self_test":      self._stop_loop,
            "simulation_report":   self._report,
            "simulate_once":       self._simulate_once,
        }
        fn = _map.get(skill_name)
        if not fn:
            return f"[VYASA] Unknown skill: {skill_name}"
        try:
            return fn(**args)
        except Exception as e:
            return f"[VYASA] Error: {e}"

    def attach_heimdall(self, heimdall):
        """Wire HEIMDALL post-construction so VYASA can pause while the user
        is mid-conversation. main.py calls this after HEIMDALL is built."""
        self._heimdall = heimdall

    # ── Loop control ─────────────────────────────────────────────────
    def _start_loop(self, interval_seconds: int = 0, max_per_day: int = 0) -> str:
        if self._running.is_set():
            return f"Self-test loop already running ({self._sim_count} simulations so far)."
        if interval_seconds:
            self.interval_seconds = max(30, int(interval_seconds))
        if max_per_day:
            self.max_per_day = max(1, int(max_per_day))
        self._running.set()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="VYASA-self-test")
        self._thread.start()
        return (f"Self-test started — {self.interval_seconds}s between sims, "
                f"cap {self.max_per_day}/day. Read-only skills only. "
                f"Report at {self.session_log_path}.")

    def _stop_loop(self) -> str:
        if not self._running.is_set():
            return "Self-test loop is not running."
        self._running.clear()
        return f"Self-test stopped. {self._sim_count} simulations run this session."

    def _loop(self):
        """Main simulation loop. Pauses while HEIMDALL is busy. Sleeps on
        SARASWATI quota near limit. Reset day-counter at midnight."""
        self._append_session(f"\n## VYASA loop started — {_now()}\n")
        while self._running.is_set():
            try:
                # Day rollover
                today = datetime.now().date()
                if today != self._sims_today_date:
                    self._sims_today = 0
                    self._sims_today_date = today
                # Cap check
                if self._sims_today >= self.max_per_day:
                    time.sleep(self.interval_seconds)
                    continue
                # Pause while user is mid-conversation
                if self._heimdall and getattr(self._heimdall, "_busy", None) \
                        and self._heimdall._busy.is_set():
                    time.sleep(min(30, self.interval_seconds))
                    continue
                cmd, source = self._next_command()
                if cmd:
                    self._run_simulation(cmd, source)
                    self._sims_today += 1
                    self._sim_count += 1
            except Exception as e:
                self._log.warning(f"VYASA loop iteration failed: {e}")
            # Sleep in small chunks so stop_loop reacts quickly
            for _ in range(self.interval_seconds // 5):
                if not self._running.is_set():
                    break
                time.sleep(5)
        self._append_session(f"\n## VYASA loop stopped — {_now()}, "
                             f"{self._sim_count} sims total.\n")

    # ── Command selection ───────────────────────────────────────────
    def _next_command(self) -> tuple[str, str]:
        """Return (command, source-tag). Mix:
        - 50% failure replay (verify fix landed)
        - 30% success replay (regression catch)
        - 20% cloud-generated fresh
        Falls through if a source has no data."""
        r = random.random()
        if r < 0.5:
            cmd = self._pick_from_failures()
            if cmd:
                return (cmd, "failure-replay")
        if r < 0.8:
            cmd = self._pick_from_successes()
            if cmd:
                return (cmd, "success-replay")
        cmd = self._pick_generated()
        if cmd:
            return (cmd, "cloud-generated")
        # Last resort if everything's empty
        return ("what's the time", "fallback")

    def _pick_from_failures(self) -> str:
        """Pick a random user-command that previously failed, so we can
        verify whether the fix has landed."""
        path = "data/logs/failures.jsonl"
        if not os.path.exists(path):
            return ""
        candidates: list[str] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-200:]
            for line in lines:
                try:
                    e = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                # We log skill+args, not user utterance — but args.get("query")
                # / args.get("question") often captures the original phrasing.
                args = e.get("args") or {}
                if isinstance(args, dict):
                    for k in ("query", "question", "task", "goal", "text", "topic", "url"):
                        v = args.get(k)
                        if isinstance(v, str) and len(v) > 3:
                            candidates.append(v)
                            break
        except OSError:
            return ""
        return random.choice(candidates) if candidates else ""

    def _pick_from_successes(self) -> str:
        """Pull a user message from the most recent session file."""
        sess_dir = "data/memory"
        if not os.path.isdir(sess_dir):
            return ""
        files = sorted(
            [os.path.join(sess_dir, f) for f in os.listdir(sess_dir)
             if f.startswith("session_") and f.endswith(".json")],
            key=os.path.getmtime, reverse=True,
        )[:5]
        # Also include the live session
        live = os.path.join(sess_dir, "session_current.json")
        if os.path.exists(live):
            files.insert(0, live)
        candidates: list[str] = []
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
                for m in msgs:
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content", "").strip()
                        if 3 < len(c) < 200:
                            candidates.append(c)
            except (OSError, json.JSONDecodeError):
                continue
        return random.choice(candidates) if candidates else ""

    def _pick_generated(self) -> str:
        """Return a cloud-generated command. Cached on disk so we don't burn
        tokens every cycle — refreshed every `generate_new_every` calls."""
        cache = self._load_generated()
        if cache and self._sim_count % self.generate_new_every != 0:
            return random.choice(cache) if cache else ""
        # Refresh from cloud (best-effort)
        fresh = self._generate_via_cloud()
        if fresh:
            cache = fresh
            self._save_generated(cache)
        return random.choice(cache) if cache else ""

    def _generate_via_cloud(self) -> list[str]:
        sara = self.marduk.get_module("SARASWATI") if self.marduk else None
        if not sara or not getattr(sara, "providers", None):
            return []
        try:
            text, _ = sara._call_with_fallback(
                sara.general_chain, _SYSTEM_PROMPT_GEN,
                "Generate 8 commands now.", max_tokens=300,
            )
        except Exception:
            return []
        if not text:
            return []
        lines = [ln.strip(" -*•0123456789.").strip().strip('"\'')
                 for ln in text.splitlines()]
        return [ln for ln in lines if 3 < len(ln) < 200][:8]

    def _load_generated(self) -> list[str]:
        if not os.path.exists(self.generated_cache_path):
            return []
        try:
            with open(self.generated_cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_generated(self, items: list[str]) -> None:
        try:
            with open(self.generated_cache_path, "w", encoding="utf-8") as f:
                json.dump(items, f)
        except OSError:
            pass

    # ── Simulation core ─────────────────────────────────────────────
    def _simulate_once(self, command: str = "") -> str:
        cmd = (command or "").strip() or self._next_command()[0]
        if not cmd:
            return "Nothing to simulate."
        result = self._run_simulation(cmd, "manual")
        return f"Simulated: {cmd!r}\nResult: {result}"

    def _run_simulation(self, command: str, source: str) -> str:
        # Match the heimdall preprocessing pipeline. NOT calling _handle directly
        # because that has side effects (THOTH writes, IRIS speaks).
        cmd = _strip_self_corrections(command)
        cmd = _normalize_email_speech(cmd)
        cmd = _normalize_url_speech(cmd)

        # Try the same fast-route ladder.
        skill, args, reply = self._try_fast_route_chain(cmd)

        # Determine outcome.
        outcome, dispatched = self._evaluate(skill, args, reply)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "command": command,
            "normalized": cmd if cmd != command else None,
            "matched_skill": skill,
            "matched_args": args,
            "would_execute": not dispatched,
            "result": reply,
            "outcome": outcome,
        }
        self._append_session_entry(entry)

        # If failed, generate fix proposal (best-effort, cloud-bounded).
        if outcome == "fail" and self.auto_propose_fixes:
            proposal = self._propose_fix(command, reply, skill)
            if proposal:
                self._save_proposal(command, reply, proposal)

        # If this was a failure-replay and it now PASSES, mark prior open
        # proposals for this command as "verified".
        if source == "failure-replay" and outcome == "pass":
            self._verify_open_proposals(command)

        return reply

    def _try_fast_route_chain(self, cmd: str) -> tuple[str, dict, str]:
        """Mirror heimdall._try_fast_route, but only DISPATCH safe skills.
        Returns (matched_skill, args, reply). reply may be a 'would-execute'
        notice when the matched skill is not in the safe allowlist."""
        # Identity / capability / small-talk first.
        if _IDENTITY_QUESTION.search(cmd):
            return ("identity", {}, _pick_identity_response("mythic"))
        if _CAPABILITY_QUESTION.search(cmd):
            return ("capability", {}, _CAPABILITIES_LINE)
        for pattern, reply in _SMALL_TALK:
            if pattern.match(cmd):
                return ("small_talk", {}, reply)

        for pattern, skill, args_fn in _FAST_ROUTES:
            m = pattern.search(cmd)
            if not m:
                continue
            try:
                args = args_fn(m)
            except Exception as e:
                return (skill, {}, f"[VYASA-pre] args build failed: {e}")
            # Safety gate: only dispatch READ-ONLY skills.
            if skill not in _SAFE_SIMULATION_SKILLS:
                return (skill, args, f"[would-execute] {skill}({args}) — skipped in simulation")
            # Dispatch.
            try:
                result = self.marduk.dispatch(skill, args)
                return (skill, args, result if isinstance(result, str) else str(result))
            except Exception as e:
                return (skill, args, f"[dispatch error] {e}")

        # No fast-route matched. Don't waste GIL on simulated commands — too
        # slow. Route to cloud only if the question looks like one cloud can
        # answer cheaply.
        if self._looks_question(cmd):
            sara = self.marduk.get_module("SARASWATI") if self.marduk else None
            if sara and getattr(sara, "providers", None):
                try:
                    out = sara.execute("ask_ai", {"question": cmd})
                    return ("ask_ai", {"question": cmd},
                            out if isinstance(out, str) else str(out))
                except Exception as e:
                    return ("ask_ai", {"question": cmd}, f"[cloud error] {e}")
        return ("", {}, "[no fast-route matched]")

    def _looks_question(self, cmd: str) -> bool:
        cmd_l = cmd.lower().strip()
        return (
            cmd_l.endswith("?")
            or cmd_l.startswith(("what", "who", "when", "where", "why", "how",
                                 "is ", "are ", "do ", "does ", "did "))
        )

    def _evaluate(self, skill: str, args: dict, reply: str) -> tuple[str, bool]:
        """Returns (outcome, dispatched). outcome is 'pass' / 'fail' / 'skipped'."""
        if not reply:
            return ("fail", False)
        if reply.startswith("[would-execute]"):
            return ("skipped", False)
        if reply.startswith(("[VYASA-pre]", "[dispatch error]", "[cloud error]",
                             "[no fast-route matched]")):
            return ("fail", False)
        if reply.startswith("MARDUK:"):
            return ("fail", True)
        low = reply.lower()
        # Heuristic for soft failures.
        if any(s in low for s in (
            "no result", "could not", "not configured", "not found",
            "failed", "missing", "no cloud", "no module handles",
            "brain stopped", "unknown skill",
        )):
            return ("fail", True)
        return ("pass", True)

    # ── Fix proposals via cloud ─────────────────────────────────────
    def _propose_fix(self, command: str, result: str, matched_skill: str) -> dict:
        sara = self.marduk.get_module("SARASWATI") if self.marduk else None
        if not sara or not getattr(sara, "providers", None):
            return {}
        prompt = (
            f"Voice command: {command!r}\n"
            f"Result: {result!r}\n"
            f"Matched skill (or empty): {matched_skill!r}\n\n"
            "Propose a specific fix. JSON only."
        )
        try:
            text, _ = sara._call_with_fallback(
                sara.code_chain, _SYSTEM_PROMPT_FIX, prompt, max_tokens=400,
            )
        except Exception:
            return {}
        if not text:
            return {}
        # Same robust JSON parse as TYR uses.
        try:
            return json.loads(text)
        except Exception:
            pass
        text2 = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
        text2 = re.sub(r"\s*```\s*$", "", text2)
        try:
            return json.loads(text2)
        except Exception:
            m = re.search(r"\{[\s\S]*\}", text2)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    return {}
        return {}

    def _save_proposal(self, command: str, result: str, proposal: dict) -> None:
        record = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "result": result[:500],
            "category": proposal.get("category", "other"),
            "diagnosis": proposal.get("diagnosis", ""),
            "proposed_fix": proposal.get("proposed_fix", ""),
            "confidence": proposal.get("confidence", "low"),
            "status": "open",
        }
        try:
            with open(self.proposal_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def _verify_open_proposals(self, command: str) -> None:
        if not os.path.exists(self.proposal_log_path):
            return
        rows: list[dict] = []
        changed = False
        try:
            with open(self.proposal_log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (r.get("command") == command
                            and r.get("status") == "open"):
                        r["status"] = "verified"
                        r["verified_at"] = datetime.now().isoformat(timespec="seconds")
                        changed = True
                    rows.append(r)
        except OSError:
            return
        if not changed:
            return
        try:
            with open(self.proposal_log_path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        except OSError:
            pass

    # ── Reporting + persistence ─────────────────────────────────────
    def _append_session_entry(self, entry: dict) -> None:
        line = (f"- {entry['ts']} [{entry['source']}] "
                f"`{entry['command']}` → "
                f"{entry['outcome'].upper()}"
                f"{' (would-execute)' if entry.get('would_execute') else ''}"
                f" via `{entry.get('matched_skill') or '(none)'}`"
                f" — {(entry.get('result') or '')[:140]}")
        self._append_session(line + "\n")

    def _append_session(self, text: str) -> None:
        try:
            with open(self.session_log_path, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def _report(self, hours: int = 24) -> str:
        try:
            hours = max(1, min(168, int(hours or 24)))
        except (TypeError, ValueError):
            hours = 24
        cutoff = datetime.now() - timedelta(hours=hours)

        # Count proposals by status
        open_props = verified_props = 0
        recent_open: list[dict] = []
        if os.path.exists(self.proposal_log_path):
            try:
                with open(self.proposal_log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = r.get("ts", "")
                        try:
                            t = datetime.fromisoformat(ts)
                        except ValueError:
                            continue
                        if t < cutoff:
                            continue
                        status = r.get("status", "open")
                        if status == "open":
                            open_props += 1
                            recent_open.append(r)
                        elif status == "verified":
                            verified_props += 1
            except OSError:
                pass

        # Count outcomes from the session log
        passes = fails = skipped = 0
        if os.path.exists(self.session_log_path):
            try:
                with open(self.session_log_path, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-500:]:
                        if " → PASS " in line: passes += 1
                        elif " → FAIL " in line: fails += 1
                        elif " → SKIPPED " in line: skipped += 1
            except OSError:
                pass

        status = "RUNNING" if self._running.is_set() else "STOPPED"
        lines = [
            f"VYASA self-test: {status}. {self._sim_count} sims this session.",
            f"Last ~{hours}h log: {passes} pass, {fails} fail, {skipped} skipped.",
            f"Fix proposals: {open_props} open, {verified_props} verified.",
        ]
        if recent_open:
            lines.append("Top open proposals:")
            for r in recent_open[:3]:
                cmd = r.get("command", "")[:60]
                diag = r.get("diagnosis", "")[:120]
                lines.append(f"  • `{cmd}` — {diag}")
        return "\n".join(lines)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
