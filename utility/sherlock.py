# SHERLOCK — Literary hero — greatest detective, finds all errors
# Diagnostics: self-checks, error detection, system health, ODIN integrity

import json
import os
import re
import sys
import importlib
import psutil
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from core.marduk import OdinModule


_FAILURE_LOG_PATH = "data/logs/failures.jsonl"
_ODIN_LOG_PATH = "data/logs/odin.log"

# MARDUK writes dispatch lines like:
#   2026-05-12 10:32:14,891 [MARDUK] dispatch: lock_screen → [KARN] args={}
_DISPATCH_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\[MARDUK\]\s+"
    r"dispatch:\s+(?P<skill>\w+)\s+→\s+\[(?P<module>\w+)\]"
)


class Sherlock(OdinModule):
    MODULE_NAME = "SHERLOCK"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "run_diagnostics",
                "description": "Run a full ODIN system diagnostic and health check",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "check_ollama",
                "description": "Check if Ollama and the LLM model are running correctly",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "check_modules",
                "description": "Verify all ODIN modules are loaded and registered",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "check_disk_health",
                "description": "Check disk space and identify large data directories",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "get_odin_uptime",
                "description": "Get how long ODIN has been running this session",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "weekly_digest",
                "description": "Summarize ODIN's failures from the last 7 days — which skills broke most.",
                "parameters": {
                    "days": {"type": "integer", "description": "Days to include (default 7)"}
                },
                "required": [],
                "internal_only": True
            },
            {
                "name": "usage_audit",
                "description": "Analyze how ODIN has been used recently. Surfaces: top skills by frequency, busiest hours, failure hotspots, and (when patterns appear obvious) suggestions for new fast-routes. Useful for 'what have I been doing with ODIN' style questions.",
                "parameters": {
                    "days": {"type": "integer", "description": "Days to include (default 7)"}
                },
                "required": []
            },
        ]

    _boot_time = datetime.now()

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "run_diagnostics": self._full_diag,
            "check_ollama": self._check_ollama,
            "check_modules": self._check_modules,
            "check_disk_health": self._disk_health,
            "get_odin_uptime": self._uptime,
            "weekly_digest": self._weekly_digest,
            "usage_audit": self._usage_audit,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[SHERLOCK] Error: {e}"
        return f"[SHERLOCK] Unknown skill: {skill_name}"

    def _full_diag(self) -> str:
        results = [
            self._check_ollama(),
            self._check_modules(),
            self._disk_health(),
            self._uptime(),
        ]
        return " | ".join(results)

    def _check_ollama(self) -> str:
        try:
            import requests
            resp = requests.get("http://localhost:11434/api/tags", timeout=3)
            if resp.ok:
                models = [m["name"] for m in resp.json().get("models", [])]
                return f"Ollama online. Models: {', '.join(models) or 'none loaded'}."
            return "Ollama reachable but returned an error."
        except Exception:
            return "Ollama not reachable. Is it running?"

    def _check_modules(self) -> str:
        if not self.marduk:
            return "MARDUK not connected."
        status = self.marduk.status()
        total_modules = len(status)
        total_skills = sum(status.values())
        return f"{total_modules} modules loaded, {total_skills} skills registered."

    def _disk_health(self) -> str:
        disk = psutil.disk_usage("C:\\")
        free_gb = disk.free // (1024 ** 3)
        used_pct = disk.percent
        data_size = self._dir_size("data")
        return (f"Disk C: {used_pct:.0f}% used, {free_gb}GB free. "
                f"ODIN data folder: {data_size:.1f}MB.")

    def _uptime(self) -> str:
        delta = datetime.now() - Sherlock._boot_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"ODIN uptime: {hours}h {minutes}m."
        return f"ODIN uptime: {minutes}m {seconds}s."

    def _dir_size(self, path: str) -> float:
        total = 0
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
        return total / (1024 ** 2)

    def _weekly_digest(self, days: int = 7) -> str:
        """Read MARDUK's failures.jsonl and summarize the last N days."""
        if not os.path.exists(_FAILURE_LOG_PATH):
            return "No failure log yet — ODIN hasn't logged any errors. (Or this is a fresh install.)"
        cutoff = datetime.now() - timedelta(days=int(days))
        skill_counts: Counter[str] = Counter()
        sample_per_skill: dict[str, str] = {}
        total = 0
        try:
            with open(_FAILURE_LOG_PATH, "r", encoding="utf-8") as f:
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
                    if ts < cutoff:
                        continue
                    total += 1
                    skill = e.get("skill", "?")
                    skill_counts[skill] += 1
                    if skill not in sample_per_skill:
                        sample_per_skill[skill] = e.get("result", "")[:120]
        except OSError as err:
            return f"Could not read failure log: {err}"
        if total == 0:
            return f"No failures in the last {days} days. ODIN ran clean."
        top = skill_counts.most_common(5)
        lines = [f"{skill}: {count}× — {sample_per_skill.get(skill,'')}" for skill, count in top]
        return f"Last {days} days: {total} failure(s) across {len(skill_counts)} skill(s). Top: " + "; ".join(lines)

    def _usage_audit(self, days: int = 7) -> str:
        """Read odin.log + failures.jsonl over the last `days` and surface
        usage patterns. Inspired by ECC's continuous-learning idea — the
        assistant analyses its own usage to suggest where it could be
        improved (or just tells you what you've been doing)."""
        try:
            days = max(1, min(60, int(days)))
        except (TypeError, ValueError):
            days = 7
        cutoff = datetime.now() - timedelta(days=days)
        skill_counts: Counter[str] = Counter()
        hour_counts: Counter[int] = Counter()
        total_dispatches = 0
        if os.path.exists(_ODIN_LOG_PATH):
            try:
                with open(_ODIN_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
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
                        total_dispatches += 1
                        skill_counts[m.group("skill")] += 1
                        hour_counts[ts.hour] += 1
            except OSError:
                pass
        # Failure load (same shape as weekly_digest)
        fail_counts: Counter[str] = Counter()
        if os.path.exists(_FAILURE_LOG_PATH):
            try:
                with open(_FAILURE_LOG_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            e = json.loads(line.strip())
                            ts = datetime.fromisoformat(e.get("ts", ""))
                        except (json.JSONDecodeError, ValueError, TypeError):
                            continue
                        if ts < cutoff:
                            continue
                        fail_counts[e.get("skill", "?")] += 1
            except OSError:
                pass
        if total_dispatches == 0:
            return f"No usage recorded in the last {days} days."
        # Build the report
        top_skills = skill_counts.most_common(7)
        skills_line = ", ".join(f"{s} ({c}×)" for s, c in top_skills)
        peak_hour, peak_count = max(hour_counts.items(), key=lambda kv: kv[1])
        # Failure rate
        total_failures = sum(fail_counts.values())
        rate = (total_failures / total_dispatches * 100) if total_dispatches else 0
        # Suggest fast-routes for non-fast-routed skills that recur a lot.
        # Fast-route hits don't show up in MARDUK dispatch logs (they fire
        # directly), so anything showing up here was an LLM-routed call.
        suggestions = []
        for skill, count in top_skills:
            if count >= 5 and skill in {"set_volume", "set_brightness", "open_application",
                                         "search_web", "wiki_lookup", "compose_note",
                                         "find_files_smart", "draft_email", "deep_research",
                                         "ask_ai", "weather_here", "get_weather", "track_flight",
                                         "crypto_price", "convert_currency", "air_quality",
                                         "browse", "drive_search", "ask_my_drive",
                                         "drive_recent", "drive_fetch", "fetch_page",
                                         "reindex_drive"}:
                continue  # already fast-routed
            if count >= 4:
                suggestions.append(skill)
        # Build voice-friendly response
        report = [
            f"In the last {days} days you've made {total_dispatches} commands across {len(skill_counts)} skills.",
            f"Most-used: {skills_line}.",
            f"Peak hour: {peak_hour:02d}:00 ({peak_count} commands).",
        ]
        if total_failures:
            top_fail = fail_counts.most_common(3)
            fail_line = ", ".join(f"{s} ({c}×)" for s, c in top_fail)
            report.append(f"Failures: {total_failures} ({rate:.1f}%) — mainly {fail_line}.")
        else:
            report.append(f"Failures: 0. Clean run.")
        if suggestions:
            report.append(
                f"Heads-up: these LLM-routed skills appeared often and could "
                f"likely become fast-routes: {', '.join(suggestions[:3])}."
            )
        return " ".join(report)
