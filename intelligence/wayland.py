# WAYLAND — Norse legendary master smith who forged the unbreakable
# weapons of the gods, including (per the Beowulf tradition) Beowulf's
# mail-coat. The mythology emphasises he REPAIRS what others have
# broken, and he is famously cautious — every weapon he forges is
# tested before delivery.
#
# In ODIN, WAYLAND is the autonomous fixer. He consumes VYASA's
# proposals (data/knowledge/vyasa_proposals.jsonl) AND ad-hoc problem
# reports, asks SARASWATI for a code patch, applies it under a strict
# safety harness, smoke-tests, and auto-reverts if anything regresses.
#
# Twin pair: VYASA detects + proposes. WAYLAND applies + verifies.
#
# This is genuinely dangerous code (it edits other modules on disk).
# Read the safety section carefully before flipping wayland.auto_apply
# to true. Off by default for good reason.

import ast
import datetime
import hashlib
import importlib
import json
import logging
import os
import shutil
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from core.marduk import OdinModule


# ── Files WAYLAND refuses to touch under any circumstance ────────
# These either own ODIN's boot path, hold the orchestrator state machine,
# or contain secrets. A bad patch to any of them breaks ODIN unrecoverably
# OR exposes credentials. Human-only territory.
_FORBIDDEN_FILES = frozenset({
    "main.py",
    "core/marduk.py",
    "core/gilgamesh.py",
    "intelligence/wayland.py",     # don't let WAYLAND patch itself — too easy to brick
})

# Subtrees WAYLAND refuses to touch (anything under these prefixes).
_FORBIDDEN_PREFIXES = (
    "data/secrets/",
    "data/backups/",
    "data/memory/saved/",
    ".git/",
)

# Patch size guard — anything bigger than this forces human review.
_MAX_PATCH_BYTES = 8000

# Rate limits.
_MAX_APPLIES_PER_DAY    = 5
_MIN_GAP_PER_FILE_SEC   = 3600   # 1 hour cooldown per file

# Where backups + logs land.
_BACKUP_DIR = "data/wayland/backups"
_LOG_PATH   = "data/logs/wayland.log"
_ACTION_LOG = "data/wayland/applied.jsonl"


_log = logging.getLogger("ODIN.WAYLAND")


class Wayland(OdinModule):
    MODULE_NAME = "WAYLAND"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("wayland", {})
        # Off by default. Turn on via config OR voice "start auto-fix".
        self.auto_apply = bool(cfg.get("auto_apply", False))
        # Hard ceiling that even voice-toggling can't override.
        self.absolute_max_per_day = int(cfg.get("max_applies_per_day", _MAX_APPLIES_PER_DAY))
        # Smoke-test command after each patch — must come back without error.
        self.smoke_skills = list(cfg.get("smoke_skills", ["get_time", "get_brightness"]))
        # Cooldown between two patches to the same file.
        self.min_gap_per_file_sec = int(cfg.get("min_gap_per_file_sec", _MIN_GAP_PER_FILE_SEC))
        # Where to source autonomy from. Either reads VYASA proposals OR
        # takes inline problem reports. Drain on a background thread when
        # enabled; otherwise only fires when explicitly invoked.
        self.drain_interval_seconds = int(cfg.get("drain_interval_seconds", 7200))   # 2 h
        self._stop = threading.Event()

        os.makedirs(_BACKUP_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        if self.auto_apply:
            t = threading.Thread(target=self._drain_loop, daemon=True,
                                 name="WAYLAND-drain")
            t.start()

    # ─── Skill surface ───────────────────────────────────────────
    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "propose_fix",
                "description": (
                    "Ask SARASWATI for a code patch addressing a described "
                    "problem. Returns the proposed change as JSON WITHOUT "
                    "applying it. Use this to inspect what WAYLAND would do "
                    "before authorising apply_fix."
                ),
                "parameters": {
                    "problem": {"type": "string", "description": "Plain-English description of what's broken"},
                    "hint_file": {"type": "string", "description": "Optional — narrow the search to this file"},
                },
                "required": ["problem"],
                "internal_only": True,
            },
            {
                "name": "apply_fix",
                "description": (
                    "Apply a patch under WAYLAND's safety harness: backup → "
                    "edit → AST check → import test → smoke test → keep or "
                    "auto-revert. Takes either a JSON patch (the output of "
                    "propose_fix) or a problem description (propose+apply in "
                    "one call). Logs everything to data/logs/wayland.log."
                ),
                "parameters": {
                    "patch":   {"type": "string", "description": "JSON patch OR plain problem description"},
                    "confirmed": {"type": "boolean", "description": "Required true for any apply; defends against accidental dispatch"},
                },
                "required": ["patch", "confirmed"],
                "internal_only": True,
            },
            {
                "name": "drain_proposals",
                "description": (
                    "Walk VYASA's queued proposals (status='open'), propose a "
                    "code patch for each via SARASWATI, and apply under the "
                    "safety harness. Honors the per-file cooldown + per-day "
                    "cap. No-op unless wayland.auto_apply is enabled OR you "
                    "pass force=true."
                ),
                "parameters": {
                    "limit": {"type": "integer", "description": "Max proposals to drain this call (default 3)"},
                    "force": {"type": "boolean", "description": "Drain even when auto_apply is off"},
                },
                "required": [],
                "internal_only": True,
            },
            {
                "name": "wayland_status",
                "description": "Show recent WAYLAND actions: applied, reverted, refused. Counts of each. Last 5 actions.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
            {
                "name": "wayland_revert_last",
                "description": "Revert the most recent applied patch using its backup. Use if you suspect a fix broke something subtle the smoke test missed.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "propose_fix":          self._propose_fix,
            "apply_fix":            self._apply_fix,
            "drain_proposals":      self._drain_proposals,
            "wayland_status":       self._status,
            "wayland_revert_last":  self._revert_last,
        }
        fn = _map.get(skill_name)
        if not fn:
            return f"[WAYLAND] Unknown skill: {skill_name}"
        try:
            return fn(**args)
        except Exception as e:
            self._audit("error", details=f"{skill_name} crashed: {e}\n{traceback.format_exc()}")
            return f"[WAYLAND] Error: {e}"

    # ─── Safety predicates ───────────────────────────────────────
    def _path_is_safe(self, rel_path: str) -> Optional[str]:
        """Return an error string if path is forbidden, None otherwise."""
        norm = rel_path.replace("\\", "/").lstrip("./")
        if norm in _FORBIDDEN_FILES:
            return f"refused: {norm} is on the forbidden-files list"
        for prefix in _FORBIDDEN_PREFIXES:
            if norm.startswith(prefix):
                return f"refused: {norm} sits inside the forbidden tree {prefix}"
        abs_target = os.path.abspath(norm)
        if not abs_target.startswith(os.path.abspath(".")):
            return f"refused: {norm} resolves outside the repo"
        if not os.path.exists(abs_target):
            return f"refused: {norm} does not exist"
        if not abs_target.endswith(".py"):
            return f"refused: {norm} is not a Python file (only .py edits allowed)"
        return None

    def _under_rate_limit(self, rel_path: str) -> Optional[str]:
        """Return error string if rate-limited, None otherwise."""
        actions = self._read_action_log()
        today_str = datetime.date.today().isoformat()
        today_applies = sum(
            1 for a in actions
            if a.get("kind") == "apply" and a.get("status") == "ok"
            and a.get("ts", "").startswith(today_str)
        )
        if today_applies >= self.absolute_max_per_day:
            return f"rate-limited: already {today_applies} applies today (cap {self.absolute_max_per_day})"
        # Per-file cooldown.
        now = time.time()
        for a in reversed(actions):
            if a.get("file") == rel_path and a.get("kind") == "apply" and a.get("status") == "ok":
                last_ts = a.get("ts_epoch", 0)
                gap = now - last_ts
                if gap < self.min_gap_per_file_sec:
                    return f"per-file cooldown: last apply to {rel_path} was {int(gap)}s ago (need {self.min_gap_per_file_sec}s)"
                break
        return None

    # ─── Core: propose ───────────────────────────────────────────
    def _propose_fix(self, problem: str = "", hint_file: str = "") -> str:
        problem = (problem or "").strip()
        if not problem:
            return "Need a problem description."
        if not self.marduk:
            return "[WAYLAND] MARDUK not connected."
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return "[WAYLAND] Needs SARASWATI cloud provider."

        # Show the cloud a tightly-scoped view: file tree + optionally the
        # contents of the hinted file. Sending whole-repo context is wasteful.
        tree_summary = self._build_repo_summary()
        file_excerpt = ""
        if hint_file:
            hint_norm = hint_file.replace("\\", "/").lstrip("./")
            if os.path.exists(hint_norm) and hint_norm.endswith(".py"):
                with open(hint_norm, "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
                file_excerpt = f"\nFILE OF INTEREST ({hint_norm}, first 6000 chars):\n{body[:6000]}\n"

        prompt = (
            "You are WAYLAND, ODIN's fixer. Given a bug description, identify "
            "the single file most likely to need a change, and emit a JSON "
            "patch in exactly one of these shapes:\n\n"
            "1. find_replace (preferred — minimal change):\n"
            '   {"file": "path/to/file.py", "patch_type": "find_replace", '
            '"find": "<unique snippet to replace>", "replace": "<new snippet>", '
            '"reason": "<1-2 sentences>"}\n\n'
            "2. full_replace (last resort — large rewrite):\n"
            '   {"file": "path/to/file.py", "patch_type": "full_replace", '
            '"content": "<entire new file>", "reason": "<1-2 sentences>"}\n\n'
            "Rules:\n"
            " - find_replace.find MUST be unique in the file (no ambiguity).\n"
            " - Keep the patch <= 200 lines of changed code; bigger needs a human.\n"
            " - Don't edit any of these: main.py, core/marduk.py, core/gilgamesh.py.\n"
            " - Output ONLY the JSON object. No surrounding prose or fences.\n\n"
            f"REPO MAP:\n{tree_summary}\n"
            f"{file_excerpt}\n"
            f"PROBLEM:\n{problem}\n"
        )
        try:
            reply = sara.execute("ask_ai", {"question": prompt})
        except Exception as e:
            return f"[WAYLAND] SARASWATI call failed: {e}"
        patch = self._extract_json(reply)
        if not patch:
            return f"[WAYLAND] Could not parse a JSON patch from cloud reply:\n{reply[:400]}"
        # Quick eligibility check so the user sees gates before approving.
        unsafe = self._validate_patch(patch)
        out = {
            "patch": patch,
            "eligible": unsafe is None,
        }
        if unsafe:
            out["refused"] = unsafe
        return json.dumps(out, indent=2)

    # ─── Core: apply (with safety harness) ───────────────────────
    def _apply_fix(self, patch: str = "", confirmed: bool = False) -> str:
        if not confirmed:
            return "[WAYLAND] apply_fix requires confirmed=true. Use propose_fix first to inspect."
        if not patch or not patch.strip():
            return "Need a patch (JSON) or a problem description."

        # If `patch` is a problem description, propose+apply in one shot.
        patch_obj = self._extract_json(patch)
        if not patch_obj:
            # Treat as problem description: propose first, then apply that proposal.
            proposed = self._propose_fix(patch)
            patch_obj = self._extract_json(proposed)
            if not patch_obj or not patch_obj.get("eligible", False):
                return f"[WAYLAND] No usable patch from problem description:\n{proposed[:600]}"
            patch_obj = patch_obj.get("patch", {})

        unsafe = self._validate_patch(patch_obj)
        if unsafe:
            self._audit("apply_refused", file=patch_obj.get("file", "?"), details=unsafe)
            return f"[WAYLAND] {unsafe}"

        rel = patch_obj["file"].replace("\\", "/").lstrip("./")
        rate_err = self._under_rate_limit(rel)
        if rate_err:
            self._audit("apply_refused", file=rel, details=rate_err)
            return f"[WAYLAND] {rate_err}"

        # Backup, apply, verify.
        backup_path = self._backup(rel)
        try:
            self._apply_patch_to_disk(rel, patch_obj)
        except Exception as e:
            self._restore(rel, backup_path)
            self._audit("apply_failed_write", file=rel, details=str(e))
            return f"[WAYLAND] Patch write failed, reverted: {e}"

        # 1. AST syntax check.
        try:
            with open(rel, "r", encoding="utf-8") as f:
                ast.parse(f.read())
        except SyntaxError as e:
            self._restore(rel, backup_path)
            self._audit("apply_reverted_syntax", file=rel, details=str(e))
            return f"[WAYLAND] Reverted — patch introduced a syntax error: {e}"

        # 2. Import test (try to (re-)import the module).
        module_name = self._path_to_module(rel)
        if module_name:
            try:
                if module_name in importlib.sys.modules:
                    importlib.reload(importlib.sys.modules[module_name])
                else:
                    importlib.import_module(module_name)
            except Exception as e:
                self._restore(rel, backup_path)
                self._audit("apply_reverted_import", file=rel, details=str(e))
                return f"[WAYLAND] Reverted — patch broke module import: {e}"

        # 3. Smoke test — dispatch a few safe skills, all should come back
        # without raising (their results don't have to be empty, they just
        # mustn't crash). Any failure → revert.
        smoke_err = self._smoke_test()
        if smoke_err:
            self._restore(rel, backup_path)
            self._audit("apply_reverted_smoke", file=rel, details=smoke_err)
            return f"[WAYLAND] Reverted — smoke test failed after patch: {smoke_err}"

        # All clear — keep the patch.
        self._audit(
            "apply", file=rel,
            details=patch_obj.get("reason", "")[:200],
            backup=backup_path, status="ok",
        )
        return (f"[WAYLAND] Applied patch to {rel}.\n"
                f"  reason: {patch_obj.get('reason', '')[:120]}\n"
                f"  backup: {backup_path}\n"
                f"  Use wayland_revert_last to undo.")

    # ─── Drain VYASA proposals ───────────────────────────────────
    def _drain_proposals(self, limit: int = 3, force: bool = False) -> str:
        if not self.auto_apply and not force:
            return "[WAYLAND] auto_apply is off. Pass force=true OR enable in config to drain."
        if not self.marduk:
            return "[WAYLAND] MARDUK not connected."
        vyasa = self.marduk.get_module("VYASA")
        if not vyasa:
            return "[WAYLAND] VYASA not online — no proposals to drain."
        proposals_path = getattr(vyasa, "proposal_log_path",
                                 "data/knowledge/vyasa_proposals.jsonl")
        if not os.path.exists(proposals_path):
            return "[WAYLAND] No proposals queued."
        try:
            limit = max(1, min(10, int(limit or 3)))
        except (TypeError, ValueError):
            limit = 3

        open_proposals = []
        try:
            with open(proposals_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if r.get("status") == "open":
                        open_proposals.append(r)
        except OSError as e:
            return f"[WAYLAND] Could not read proposals: {e}"
        if not open_proposals:
            return "[WAYLAND] No open proposals."

        applied = 0
        skipped = 0
        results = []
        for p in open_proposals[:limit]:
            problem = f"{p.get('diagnosis', '')}\n\nProposed fix idea: {p.get('proposed_fix', '')}"
            r = self._apply_fix(patch=problem, confirmed=True)
            results.append(f"  • {p.get('category', 'other')}: {r.splitlines()[0][:100]}")
            if r.startswith("[WAYLAND] Applied"):
                applied += 1
            else:
                skipped += 1
        return f"[WAYLAND] drain: applied {applied} / refused or reverted {skipped}\n" + "\n".join(results)

    # ─── Status + revert ─────────────────────────────────────────
    def _status(self) -> str:
        actions = self._read_action_log()
        if not actions:
            return "[WAYLAND] No actions on record yet."
        recent = actions[-5:]
        counts = {}
        for a in actions[-50:]:
            counts[a.get("status", "?")] = counts.get(a.get("status", "?"), 0) + 1
        lines = [f"[WAYLAND] last-50 status breakdown: " +
                 ", ".join(f"{k}={v}" for k, v in counts.items())]
        lines.append("recent 5:")
        for a in recent:
            lines.append(f"  {a.get('ts', '?')}  {a.get('kind', '?'):<22} "
                         f"{a.get('status', '-'):<8}  {a.get('file', '-')}  "
                         f"{(a.get('details') or '')[:80]}")
        return "\n".join(lines)

    def _revert_last(self) -> str:
        actions = self._read_action_log()
        # Find the most recent kind=apply,status=ok with a backup path.
        for a in reversed(actions):
            if (a.get("kind") == "apply" and a.get("status") == "ok"
                    and a.get("backup") and os.path.exists(a["backup"])):
                rel = a.get("file")
                if rel and self._restore(rel, a["backup"]) is None:
                    self._audit("manual_revert", file=rel,
                                details=f"reverted from {a['backup']}")
                    return f"[WAYLAND] Reverted {rel} from {a['backup']}."
        return "[WAYLAND] No reversible apply on record."

    # ─── Background drain loop (only runs when auto_apply=True) ──
    def _drain_loop(self):
        time.sleep(180)   # stagger far past boot
        while not self._stop.wait(self.drain_interval_seconds):
            try:
                if self.marduk:
                    iris = self.marduk.get_module("IRIS")
                    if iris and hasattr(iris, "is_speaking") and iris.is_speaking():
                        continue
                self._drain_proposals(limit=1, force=False)
            except Exception as e:
                _log.warning(f"drain loop hiccup: {e}")

    # ─── Mechanics: read / patch / backup / restore ──────────────
    def _validate_patch(self, patch: dict) -> Optional[str]:
        if not isinstance(patch, dict):
            return "patch is not a JSON object"
        rel = patch.get("file", "")
        if not rel:
            return "patch missing 'file'"
        unsafe = self._path_is_safe(rel)
        if unsafe:
            return unsafe
        ptype = patch.get("patch_type")
        if ptype == "find_replace":
            if not patch.get("find"):
                return "find_replace patch missing 'find'"
            if "replace" not in patch:
                return "find_replace patch missing 'replace'"
            with open(rel, "r", encoding="utf-8") as f:
                body = f.read()
            occurrences = body.count(patch["find"])
            if occurrences == 0:
                return "find_replace 'find' string not present in file"
            if occurrences > 1:
                return f"find_replace 'find' is ambiguous ({occurrences} matches) — needs to be unique"
            diff_size = abs(len(patch["replace"]) - len(patch["find"]))
            if diff_size > _MAX_PATCH_BYTES:
                return f"patch too large ({diff_size} bytes diff > {_MAX_PATCH_BYTES})"
        elif ptype == "full_replace":
            if "content" not in patch:
                return "full_replace patch missing 'content'"
            if len(patch["content"]) > _MAX_PATCH_BYTES * 8:
                return f"full_replace too large ({len(patch['content'])} bytes) — needs human review"
        else:
            return f"unknown patch_type {ptype!r} — use 'find_replace' or 'full_replace'"
        return None

    def _backup(self, rel: str) -> str:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = hashlib.sha1(rel.encode()).hexdigest()[:6]
        out = os.path.join(_BACKUP_DIR, f"{slug}_{ts}__{os.path.basename(rel)}.bak")
        shutil.copy2(rel, out)
        return out

    def _restore(self, rel: str, backup_path: str) -> Optional[str]:
        try:
            shutil.copy2(backup_path, rel)
            return None
        except Exception as e:
            return str(e)

    def _apply_patch_to_disk(self, rel: str, patch: dict):
        ptype = patch["patch_type"]
        if ptype == "find_replace":
            with open(rel, "r", encoding="utf-8") as f:
                body = f.read()
            new_body = body.replace(patch["find"], patch["replace"], 1)
            with open(rel, "w", encoding="utf-8") as f:
                f.write(new_body)
        else:  # full_replace
            with open(rel, "w", encoding="utf-8") as f:
                f.write(patch["content"])

    # ─── Smoke test: dispatch a few harmless skills ──────────────
    def _smoke_test(self) -> Optional[str]:
        if not self.marduk:
            return None
        for skill in self.smoke_skills:
            try:
                result = self.marduk.dispatch(skill, {})
                # A returned string starting with "[ERROR" or raised
                # exception both count as failure.
                if isinstance(result, str) and result.startswith(("[ERROR", "[MARDUK")):
                    return f"smoke '{skill}' returned error string: {result[:120]}"
            except Exception as e:
                return f"smoke '{skill}' raised: {e}"
        return None

    # ─── Misc helpers ────────────────────────────────────────────
    def _build_repo_summary(self) -> str:
        lines = []
        skip = {".git", "__pycache__", "data", ".venv", "venv", "node_modules"}
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            depth = root.count(os.sep)
            if depth > 3:
                continue
            for f in sorted(files):
                if f.endswith(".py"):
                    lines.append(os.path.relpath(os.path.join(root, f)).replace("\\", "/"))
        return "\n".join(lines)

    def _extract_json(self, text: str) -> dict:
        if not text:
            return {}
        # Strip fence if present.
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            return {}
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return {}

    def _path_to_module(self, rel: str) -> Optional[str]:
        norm = rel.replace("\\", "/").lstrip("./")
        if not norm.endswith(".py"):
            return None
        mod = norm[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        return mod

    # ─── Audit log ───────────────────────────────────────────────
    def _audit(self, kind: str, file: str = "", details: str = "",
               backup: str = "", status: str = ""):
        record = {
            "ts":       datetime.datetime.now().isoformat(timespec="seconds"),
            "ts_epoch": time.time(),
            "kind":     kind,
            "file":     file,
            "details":  details[:500],
            "backup":   backup,
            "status":   status or kind.split("_")[0],
        }
        try:
            os.makedirs(os.path.dirname(_ACTION_LOG), exist_ok=True)
            with open(_ACTION_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{record['ts']}  {kind:<22} {file}  {details[:200]}\n")
        except OSError:
            pass

    def _read_action_log(self) -> list:
        if not os.path.exists(_ACTION_LOG):
            return []
        out = []
        try:
            with open(_ACTION_LOG, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return out


# `re` is used in _extract_json — import once at module level.
import re
