# MIMIR — Norse — the wisest of beings, Odin's counselor. When the gods
# warred with the Vanir, Mimir was beheaded. Odin preserved his head with
# herbs and consulted it for wisdom across the worlds. In ODIN, MIMIR is
# the self-diagnostic — he looks inward at ODIN's own behaviour log,
# identifies recurring failures, and proposes (or applies) fixes.
#
# What MIMIR actually does, concretely:
#   1. Reads data/logs/failures.jsonl and data/logs/odin.log.
#   2. Buckets failures by (skill, kind-of-failure).
#   3. For each recurring bucket, proposes a fix:
#         - LLM-routed skill repeated ≥ N times → propose adding a fast-route
#         - No-handler errors → propose a skill alias or a new mapping
#         - Cloud-API errors with same provider repeating → propose chain reorder
#         - PROMETHEUS prefs duplicating ("name", "<name> and my age is <n>")
#           → propose canonicalisation
#   4. The proposal is structured + voice-friendly. Auto-apply is OFF by
#      default — `propose_fixes` returns suggestions; `apply_fix` takes one
#      and writes it.
#
# Why this is not just SHERLOCK with extra steps:
#   SHERLOCK already counts failures (weekly_digest) and surfaces top
#   LLM-routed skills (usage_audit). MIMIR is the next layer: it reads
#   SHERLOCK's surfaced patterns and EXPLAINS WHY each one happened
#   plus what to do about it. A scribe vs. a strategist.

import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from core.marduk import OdinModule


_FAILURE_LOG = "data/logs/failures.jsonl"
_ODIN_LOG = "data/logs/odin.log"
_DISPATCH_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\[MARDUK\]\s+"
    r"dispatch:\s+(?P<skill>\w+)\s+→\s+\[(?P<module>\w+)\]\s+args=(?P<args>.+)$"
)
_GIL_STALL_RE = re.compile(r"brain stopped responding|Likely memory pressure")
_BRAIN_STOPPED_RE = re.compile(r"\[GIL\]\s+(?:stream timed out|Ollama.*timeout|chat error)")


class Mimir(OdinModule):
    MODULE_NAME = "MIMIR"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("mimir", {})
        # Repetition thresholds before MIMIR even considers a pattern worth
        # surfacing. Lower = noisier suggestions; higher = misses rare bugs.
        self.fast_route_threshold = int(cfg.get("fast_route_threshold", 3))
        self.failure_threshold = int(cfg.get("failure_threshold", 2))
        # Default lookback for `diagnose` — 24h is enough to catch issues
        # from the most recent session without drowning in old noise.
        self.default_hours = int(cfg.get("default_hours", 24))
        # Auto-apply mode. OFF by default — MIMIR proposes; the user accepts.
        # Even with auto-apply on, destructive fixes (file edits) still need
        # the user's voice "apply that fix" before they land.
        self.auto_apply = bool(cfg.get("auto_apply", False))

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "diagnose",
                "description": (
                    "Self-diagnose. Reads ODIN's own dispatch log and failure "
                    "log, identifies recurring problems, and produces a "
                    "voice-friendly summary of what's broken and WHY. Use "
                    "when the user asks 'why did you fail' / 'what's wrong' / "
                    "'diagnose yourself'."
                ),
                "parameters": {
                    "hours": {"type": "integer", "description": f"Look-back window in hours (default {self.default_hours})"},
                },
                "required": [],
            },
            {
                "name": "propose_fixes",
                "description": (
                    "Look at recent failures and propose concrete fixes — "
                    "fast-route candidates, skill aliases, provider chain "
                    "tweaks. Returns a list with rationale. Does NOT apply "
                    "anything; use apply_fix for that."
                ),
                "parameters": {
                    "hours": {"type": "integer", "description": "Look-back window (default 24)"},
                },
                "required": [],
            },
            {
                "name": "explain_last_failure",
                "description": (
                    "Explain why the most recent failure happened in human "
                    "terms. Useful when the user asks 'what just went wrong'."
                ),
                "parameters": {},
                "required": [],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "diagnose":             self._diagnose,
            "propose_fixes":        self._propose_fixes,
            "explain_last_failure": self._explain_last_failure,
        }
        fn = _map.get(skill_name)
        if not fn:
            return f"[MIMIR] Unknown skill: {skill_name}"
        try:
            return fn(**args)
        except Exception as e:
            return f"[MIMIR] Error: {e}"

    # ── Diagnosis ───────────────────────────────────────────────────
    def _diagnose(self, hours: int = 0) -> str:
        hours = self._clamp_hours(hours)
        cutoff = datetime.now() - timedelta(hours=hours)
        fails = self._load_failures(cutoff)
        dispatches = self._load_dispatches(cutoff)
        stalls = self._count_gil_stalls(cutoff)

        if not fails and not stalls:
            n = len(dispatches)
            return (f"Looked back {hours}h: {n} dispatches, 0 failures, 0 brain stalls. "
                    "Clean run.")

        # Bucket failures by skill + first-word of the error message.
        buckets: Counter[tuple[str, str]] = Counter()
        for f in fails:
            skill = f.get("skill") or "?"
            kind = self._classify(f.get("result", ""))
            buckets[(skill, kind)] += 1
        top = buckets.most_common(5)

        lines = [f"In the last {hours}h: {len(dispatches)} dispatches, "
                 f"{len(fails)} failure(s), {stalls} brain stall(s)."]
        if top:
            lines.append("Top issues:")
            for (skill, kind), n in top:
                lines.append(f"  • {skill} — {kind} × {n}")
        if stalls:
            lines.append(f"GIL stalls suggest 8 GB memory pressure or a slow tool-chain. "
                         f"Consider routing complex commands to TYR instead of GIL.")
        return "\n".join(lines)

    def _propose_fixes(self, hours: int = 0) -> str:
        hours = self._clamp_hours(hours)
        cutoff = datetime.now() - timedelta(hours=hours)
        proposals: list[str] = []

        # Proposal type A — over-LLM-routed skills (high dispatch frequency
        # via GIL tool-calling, when a fast-route would be cheaper).
        dispatches = self._load_dispatches(cutoff)
        skill_counts: Counter[str] = Counter(d["skill"] for d in dispatches)
        fast_routed = self._known_fast_routed()
        for skill, n in skill_counts.most_common():
            if n < self.fast_route_threshold:
                break
            if skill in fast_routed or skill == "?":
                continue
            sample_args = next((d["args"] for d in dispatches if d["skill"] == skill), "{}")
            proposals.append(
                f"FAST-ROUTE  '{skill}' fired {n}× via LLM. Add a fast-route in "
                f"heimdall._FAST_ROUTES so voice commands hit it directly. "
                f"Sample args: {sample_args}"
            )

        # Proposal type B — recurring failures grouped by skill.
        fails = self._load_failures(cutoff)
        fail_by_skill: dict[str, list[dict]] = defaultdict(list)
        for f in fails:
            fail_by_skill[f.get("skill", "?")].append(f)
        for skill, items in fail_by_skill.items():
            if len(items) < self.failure_threshold:
                continue
            kinds = Counter(self._classify(it.get("result", "")) for it in items)
            kind, k = kinds.most_common(1)[0]
            sample = (items[0].get("result", "") or "")[:120]
            proposals.append(
                f"FIX  '{skill}' failed {len(items)}× ({kind}). Sample: \"{sample}\". "
                f"{self._suggest_for_kind(skill, kind)}"
            )

        # Proposal type C — GIL stalls cluster.
        stalls = self._count_gil_stalls(cutoff)
        if stalls >= 2:
            proposals.append(
                f"STALL  GIL stalled {stalls}× in {hours}h. Either (a) drop "
                f"keep_alive to 2m to free memory faster, (b) route the offending "
                f"verb to TYR via pursue_goal, or (c) add an explicit fast-route "
                f"so the stalling command never reaches GIL."
            )

        if not proposals:
            return f"No fix proposals — looked at {hours}h of activity, everything looks clean."
        return f"Found {len(proposals)} fix proposal(s):\n\n" + "\n\n".join(
            f"{i+1}. {p}" for i, p in enumerate(proposals)
        )

    def _explain_last_failure(self) -> str:
        fails = self._load_failures(datetime.min)
        if not fails:
            return "No failures logged."
        f = fails[-1]
        skill = f.get("skill", "?")
        result = f.get("result", "")
        kind = self._classify(result)
        ts = f.get("ts", "")
        return (f"Last failure ({ts}) was on '{skill}' — {kind}. "
                f"Result: {result[:200]}. "
                f"{self._suggest_for_kind(skill, kind)}")

    # ── Internals ────────────────────────────────────────────────────
    def _classify(self, error_text: str) -> str:
        """Bucket a result string into one of a handful of failure kinds."""
        s = (error_text or "").lower()
        if "no module handles skill" in s or "unknown skill" in s:
            return "no-handler"
        if "timeout" in s or "timed out" in s or "stopped responding" in s:
            return "timeout"
        if "quota" in s or "rate" in s and "limit" in s:
            return "rate-limited"
        if "not configured" in s or "no cloud" in s or "missing api key" in s:
            return "unconfigured"
        if "no result" in s or "could not" in s or "not found" in s:
            return "empty-result"
        if "json" in s and ("parse" in s or "decode" in s):
            return "parse-error"
        if "connection" in s or "refused" in s or "unreachable" in s:
            return "network"
        return "other"

    def _suggest_for_kind(self, skill: str, kind: str) -> str:
        if kind == "no-handler":
            return ("The LLM tried to call a skill that isn't registered. Add it to "
                    "the right module's skills() list, or remove it from the LLM's "
                    "tool catalogue.")
        if kind == "timeout":
            return ("Most likely 8 GB memory pressure. Either route this verb to "
                    "TYR/SARASWATI instead of GIL, or add a fast-route so it never "
                    "touches the LLM.")
        if kind == "rate-limited":
            return ("Daily quota hit. Reorder SARASWATI's chain to put a less-used "
                    "provider first, or raise the daily_limits in config.yaml.")
        if kind == "unconfigured":
            return ("Provider key not set. Check the relevant section in config.yaml "
                    "or set the env var.")
        if kind == "empty-result":
            return (f"Skill '{skill}' returned nothing useful. Either the upstream "
                    f"API has no data, or the query needed normalization (city name, "
                    f"URL form, etc). Check the input.")
        if kind == "parse-error":
            return ("Cloud LLM returned malformed JSON. Tighten the system prompt or "
                    "add a more aggressive JSON extractor.")
        if kind == "network":
            return "Network issue — check connectivity / firewall. Retry."
        return "Inspect the result string; the cause is module-specific."

    def _load_failures(self, cutoff: datetime) -> list[dict]:
        out: list[dict] = []
        if not os.path.exists(_FAILURE_LOG):
            return out
        try:
            with open(_FAILURE_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    try:
                        ts = datetime.fromisoformat(e.get("ts", ""))
                    except (ValueError, TypeError):
                        continue
                    if ts >= cutoff:
                        out.append(e)
        except OSError:
            pass
        return out

    def _load_dispatches(self, cutoff: datetime) -> list[dict]:
        out: list[dict] = []
        if not os.path.exists(_ODIN_LOG):
            return out
        try:
            with open(_ODIN_LOG, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = _DISPATCH_RE.match(line)
                    if not m:
                        continue
                    try:
                        ts = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        continue
                    if ts < cutoff:
                        continue
                    out.append({"ts": ts, "skill": m.group("skill"),
                                "module": m.group("module"), "args": m.group("args")})
        except OSError:
            pass
        return out

    def _count_gil_stalls(self, cutoff: datetime) -> int:
        if not os.path.exists(_ODIN_LOG):
            return 0
        n = 0
        try:
            with open(_ODIN_LOG, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if _GIL_STALL_RE.search(line) or _BRAIN_STOPPED_RE.search(line):
                        # Best-effort timestamp parse from the line start
                        try:
                            ts = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
                            if ts >= cutoff:
                                n += 1
                        except ValueError:
                            n += 1  # No timestamp → count it
        except OSError:
            pass
        return n

    def _clamp_hours(self, hours) -> int:
        try:
            h = int(hours) if hours else self.default_hours
        except (TypeError, ValueError):
            h = self.default_hours
        return max(1, min(720, h))   # 1 hour to 30 days

    def _known_fast_routed(self) -> set[str]:
        """Skills that already have a fast-route in HEIMDALL. We don't want
        to suggest fast-routing something that's already routed; that just
        means the regex isn't matching the user's phrasing — different fix."""
        return {
            "set_volume", "set_brightness", "open_application", "search_web",
            "wiki_lookup", "compose_note", "find_files_smart", "draft_email",
            "deep_research", "ask_ai", "ask_claude", "weather_here", "get_weather",
            "track_flight", "crypto_price", "convert_currency", "air_quality",
            "browse", "drive_search", "ask_my_drive", "drive_recent",
            "drive_fetch", "fetch_page", "pursue_goal", "get_time",
            "lock_screen", "take_screenshot", "set_reminder", "list_reminders",
            "remember", "recall", "list_memories", "list_directory",
            "search_vault", "tell_joke", "public_holidays", "morning_digest",
        }
