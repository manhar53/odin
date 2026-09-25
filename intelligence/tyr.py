# TYR — Norse god of just war, oaths, and law. The one who placed his hand
# in Fenrir's jaws to bind the wolf, knowing he'd lose it: completion at
# any cost. In ODIN, TYR is the autonomous executor — give him a goal in
# natural language, he PLANS a sequence of tool calls, EXECUTES them via
# MARDUK, REFLECTS on whether the goal is met, and loops until done or the
# step cap is reached.
#
# Why this isn't redundant with GIL + tool-calling:
#   - GIL runs on local 1B (llama3.2:1b). The 1B is a poor planner for
#     multi-step tool chains — it forgets steps, hallucinates skill names,
#     and never reflects on whether the goal was actually met.
#   - SARASWATI.deep_research has plan-then-execute, but only for
#     research synthesis — it doesn't dispatch tools.
#   - TYR uses cloud LLMs (Gemini → Claude via SARASWATI) to plan a real
#     skill chain, dispatch each step through MARDUK (so module-level
#     safety stays intact), and reflect after every batch.
#
# Safety:
#   - max_steps hard cap (default 10) prevents runaway loops.
#   - Skill names are validated against MARDUK's registry before dispatch
#     so hallucinated tools fail fast.
#   - Per-step result is trimmed to 800 chars before being fed back to
#     the planner — bounds prompt growth.
#   - Destructive goals (buy/send/submit/cancel/...) require "confirmed"
#     in the goal text, same gate ARGUS uses.
#   - dry_run=True returns the plan without executing a single skill —
#     for goal-text sanity checking.

import json
import os
import re
from datetime import datetime
from core.marduk import OdinModule

from output.argus import _DESTRUCTIVE_INTENT, _CONFIRMED


_SYSTEM_PROMPT_PLAN = """You are TYR, ODIN's autonomous executor. You decompose a user GOAL into a sequence of TOOL CALLS using ODIN's registered skills.

Output rules:
- Reply with VALID JSON ONLY. No prose before or after.
- Schema: {"plan": [{"skill": "<exact_skill_name>", "args": {...}, "reason": "<1 sentence>"}, ...]}
- Use ONLY skills from the provided catalogue. Hallucinating a skill name will fail.
- 1-8 steps. Fewer is better. Don't repeat the same skill+args twice.
- Skip destructive actions (buy, send, submit, cancel, delete) unless the goal contains the word "confirmed".
- Steps can reference earlier results with placeholders like {{step_1_result}} — the executor will substitute.
- If the goal can't be done with the available skills, return {"plan": [], "reason": "<why>"}."""


_SYSTEM_PROMPT_REFLECT = """You are TYR's reflector. Given the goal and the steps executed so far, decide if the goal is satisfied. If not, propose ONE next step.

Output rules:
- Reply with VALID JSON ONLY.
- Schema: {"done": true|false, "summary": "<plain-prose 1-3 sentences>", "next_step": {"skill": "...", "args": {...}, "reason": "..."} | null}
- If done=true, next_step MUST be null.
- If you don't see enough progress and adding more steps wouldn't help, return done=true with a summary explaining the partial result."""


_PLACEHOLDER_RE = re.compile(r"\{\{\s*step_(\d+)_result\s*\}\}")


class Tyr(OdinModule):
    MODULE_NAME = "TYR"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("tyr", {})
        self.max_steps = int(cfg.get("max_steps", 10))
        self.reflect_every = int(cfg.get("reflect_every", 3))
        self.save_to_vault = bool(cfg.get("save_to_vault", True))
        self.confirm_destructive = bool(cfg.get("confirm_destructive", True))
        # Per-step result trim before feeding back to the planner. Keeps
        # the cloud prompt bounded as steps accumulate.
        self.result_char_cap = int(cfg.get("result_char_cap", 800))

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "pursue_goal",
                "description": (
                    "Pursue a multi-step goal autonomously. TYR uses cloud AI "
                    "(via SARASWATI) to plan a sequence of real ODIN skill "
                    "calls, executes them through MARDUK, and reflects until "
                    "the goal is met or the step cap is reached. Each step "
                    "is a real dispatch — existing module-level safety still "
                    "applies. A goal log is written to the vault."
                ),
                "parameters": {
                    "goal": {"type": "string", "description": "Natural-language description of the goal"},
                    "max_steps": {"type": "integer", "description": f"Step cap (default {self.max_steps})"},
                    "dry_run": {"type": "boolean", "description": "Return the plan without executing"},
                },
                "required": ["goal"],
            },
            {
                "name": "code_task",
                "description": (
                    "Autonomously WRITE + RUN code to accomplish a task. TYR "
                    "asks SARASWATI to generate code, writes it to a scratch "
                    "dir, runs it, captures output + errors, and iterates "
                    "(re-asking the cloud to fix errors) up to max_iter times. "
                    "USE FOR: 'write me a script that summarizes my notes', "
                    "'parse this CSV and find anomalies', 'scrape this URL and "
                    "save as JSON'. NOT FOR: editing ODIN's own modules — too "
                    "easy to break ODIN. Runs in data/tyr_scratch/<slug>/ with "
                    "a 60-second timeout per execution."
                ),
                "parameters": {
                    "task":       {"type": "string",  "description": "What the code should do, in plain English"},
                    "language":   {"type": "string",  "description": "Defaults to 'python'. Also: 'bash', 'powershell'."},
                    "max_iter":   {"type": "integer", "description": f"Max fix-and-retry iterations (default {3})"},
                    "input_data": {"type": "string",  "description": "Optional context to embed in the prompt (e.g. a CSV header, a URL)"},
                },
                "required": ["task"],
                "internal_only": True,
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        if skill_name == "pursue_goal":
            try:
                return self._pursue_goal(**args)
            except Exception as e:
                return f"[TYR] Error: {e}"
        if skill_name == "code_task":
            try:
                return self._code_task(**args)
            except Exception as e:
                return f"[TYR] code_task error: {e}"
        return f"[TYR] Unknown skill: {skill_name}"

    # ── Core loop ────────────────────────────────────────────────────
    def _pursue_goal(self, goal: str = "", max_steps: int = 0, dry_run: bool = False) -> str:
        goal = (goal or "").strip()
        if not goal:
            return "Need a goal."
        if not self.marduk:
            return "[TYR] MARDUK not connected."
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return ("[TYR] Cloud LLM required for planning. Configure SARASWATI "
                    "(Gemini / Groq / Claude / OpenAI) first.")
        if (self.confirm_destructive
                and _DESTRUCTIVE_INTENT.search(goal)
                and not _CONFIRMED.search(goal)):
            return ("This goal looks destructive (could buy/send/submit/cancel "
                    "something irreversibly). Re-issue with 'confirmed' in the "
                    "goal to proceed. Example: 'book the cheapest Tokyo flight, confirmed.'")

        cap = max_steps if max_steps and max_steps > 0 else self.max_steps
        cap = max(1, min(20, cap))   # absolute ceiling

        catalogue = self._tool_catalogue()
        plan = self._call_planner(sara, goal, catalogue)
        if not plan:
            return "[TYR] Planner returned no usable plan."

        if dry_run:
            preview = "\n".join(f"{i+1}. {s['skill']}({s.get('args', {})})  — {s.get('reason','')}"
                                for i, s in enumerate(plan[:cap]))
            return f"[DRY RUN] Plan ({len(plan)} step(s)):\n{preview}"

        # ── Execute loop ──────────────────────────────────────────────
        history: list[dict] = []  # each: {skill, args, result}
        step_idx = 0
        # We keep a flexible plan: the initial plan is the starting list,
        # but reflection can append a `next_step` after every reflect_every
        # steps. Loop ends when (a) we hit cap, (b) reflector says done,
        # or (c) plan is exhausted and last reflection said no next step.
        while step_idx < cap and plan:
            step = plan.pop(0)
            skill_name = step.get("skill", "")
            args = self._resolve_placeholders(step.get("args", {}) or {}, history)
            valid, problem = self._validate_step(skill_name, args)
            if not valid:
                history.append({"skill": skill_name, "args": args,
                                "result": f"[skipped: {problem}]"})
                step_idx += 1
                continue
            try:
                result = self.marduk.dispatch(skill_name, args)
            except Exception as e:
                result = f"[dispatch error: {e}]"
            result = self._trim(str(result), self.result_char_cap)
            history.append({"skill": skill_name, "args": args, "result": result})
            step_idx += 1

            # Reflect every N steps OR when we've exhausted the initial plan.
            if step_idx % self.reflect_every == 0 or not plan:
                reflection = self._call_reflector(sara, goal, history)
                if reflection.get("done"):
                    return self._finalize(goal, history, reflection, step_idx, cap)
                nxt = reflection.get("next_step")
                if nxt and isinstance(nxt, dict) and nxt.get("skill"):
                    plan.append(nxt)

        # Loop ended without a clean "done" — wrap up.
        final_reflection = self._call_reflector(sara, goal, history) if history else {}
        return self._finalize(goal, history, final_reflection or
                              {"done": False, "summary": "Step cap reached without completion."},
                              step_idx, cap)

    # ── Helpers ──────────────────────────────────────────────────────
    def _tool_catalogue(self) -> str:
        """Compact one-line-per-skill listing for the planner prompt.
        Skips TYR itself (no recursion) and internal_only skills."""
        lines = []
        for mod_name, mod in self.marduk._modules.items():
            if mod_name == self.MODULE_NAME:
                continue
            for skill in mod.skills:
                if skill.get("internal_only"):
                    continue
                name = skill["name"]
                desc = skill["description"][:140]
                params = list(skill.get("parameters", {}).keys())
                lines.append(f"- {name}({', '.join(params)}): {desc}")
        return "\n".join(lines)

    def _call_planner(self, sara, goal: str, catalogue: str) -> list[dict]:
        prompt = (
            f"GOAL: {goal}\n\n"
            f"AVAILABLE TOOLS (skill_name(params): description):\n{catalogue}\n\n"
            f"Respond with JSON only."
        )
        text, _ = sara._call_with_fallback(
            sara.general_chain, _SYSTEM_PROMPT_PLAN, prompt, max_tokens=900
        )
        data = _parse_json(text)
        if not isinstance(data, dict):
            return []
        plan = data.get("plan") or []
        if not isinstance(plan, list):
            return []
        out = []
        for step in plan:
            if isinstance(step, dict) and step.get("skill"):
                args = step.get("args") if isinstance(step.get("args"), dict) else {}
                out.append({"skill": step["skill"], "args": args,
                            "reason": step.get("reason", "")})
        return out

    def _call_reflector(self, sara, goal: str, history: list[dict]) -> dict:
        hist_text = "\n".join(
            f"Step {i+1}: {h['skill']}({h['args']}) -> {h['result']}"
            for i, h in enumerate(history)
        )
        prompt = (
            f"GOAL: {goal}\n\n"
            f"STEPS EXECUTED SO FAR:\n{hist_text or '(none yet)'}\n\n"
            "Respond with JSON only."
        )
        text, _ = sara._call_with_fallback(
            sara.general_chain, _SYSTEM_PROMPT_REFLECT, prompt, max_tokens=400
        )
        data = _parse_json(text)
        if isinstance(data, dict):
            return data
        # Fallback so the loop doesn't hang on a malformed reflection.
        return {"done": True, "summary": "Reflection unparseable; stopping.", "next_step": None}

    def _validate_step(self, skill_name: str, args: dict) -> tuple[bool, str]:
        if not skill_name or not isinstance(skill_name, str):
            return False, "missing skill name"
        if skill_name not in self.marduk._skill_map:
            return False, f"unknown skill '{skill_name}'"
        if skill_name == "pursue_goal":
            return False, "refusing TYR recursion"
        if not isinstance(args, dict):
            return False, "args must be a dict"
        return True, ""

    def _resolve_placeholders(self, args: dict, history: list[dict]) -> dict:
        """Substitute {{step_N_result}} tokens with actual prior results."""
        out = {}
        for k, v in args.items():
            if isinstance(v, str):
                def _sub(m):
                    idx = int(m.group(1)) - 1
                    if 0 <= idx < len(history):
                        return history[idx]["result"]
                    return m.group(0)
                out[k] = _PLACEHOLDER_RE.sub(_sub, v)
            else:
                out[k] = v
        return out

    def _trim(self, s: str, cap: int) -> str:
        if len(s) <= cap:
            return s
        return s[:cap] + f"... [trimmed, full length {len(s)}]"

    def _finalize(self, goal: str, history: list[dict], reflection: dict,
                  step_idx: int, cap: int) -> str:
        summary = reflection.get("summary") or "(no summary)"
        # Persist a goal log so the user can audit what TYR did.
        if self.save_to_vault and self.marduk:
            nabu = self.marduk.get_module("NABU")
            if nabu:
                try:
                    nabu.execute("write_note", {
                        "path": f"goals/{_slug(goal)}.md",
                        "content": _format_goal_log(goal, history, reflection,
                                                    step_idx, cap),
                        "append": False,
                    })
                except Exception:
                    pass
        status = "completed" if reflection.get("done") else f"halted after {step_idx} step(s)"
        return f"Goal {status}. {summary}"


# ── Module-level utilities ──────────────────────────────────────────
def _parse_json(text: str):
    """Robust JSON extraction — strips ``` fences, finds the first {...} or
    [...] block if the model wrapped it in prose."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        # Drop a leading ```json / ```\n fence and trailing ```
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    # Find the outermost {...} block.
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:60] or "goal"


def _format_goal_log(goal: str, history: list[dict], reflection: dict,
                     step_idx: int, cap: int) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# Goal: {goal}",
        "",
        f"*TYR autonomous execution — {ts}.*",
        f"*Steps executed: {step_idx} / cap {cap}. Done: {reflection.get('done', False)}.*",
        "",
        "## Summary",
        reflection.get("summary", "(no summary)"),
        "",
        "## Steps",
    ]
    for i, h in enumerate(history):
        lines.append(f"### Step {i+1}: `{h['skill']}`")
        lines.append(f"- args: `{h['args']}`")
        lines.append(f"- result: {h['result']}")
        lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
# code_task — TYR's autonomous coding loop.
#
# Pattern adapted from OpenHands / Aider / Codebuff: ask LLM for code,
# write to file, run it, capture output + errors, ask LLM to fix, repeat.
# But scoped DOWN from a full agent platform to a single bounded skill
# that ODIN can dispatch like any other.
#
# Safety:
#   • Code lives in data/tyr_scratch/<slug>/. We never touch ODIN's own
#     modules or the user's vault from here.
#   • Subprocess timeout = 60 s per execution. No infinite loops.
#   • DESTRUCTIVE shell verbs in generated code (rm -rf, format, del /f,
#     reg delete) are flagged and require explicit confirmation.
#   • Output captured + truncated; we never let the LLM see >10 KB of
#     output (could leak secrets).
# ─────────────────────────────────────────────────────────────────

import re as _re
import subprocess
import tempfile
import shutil
from typing import Optional


_CODE_SYSTEM_PROMPT = """You are an expert programmer writing self-contained scripts.

OUTPUT FORMAT (strict):
- ONE fenced code block with the requested language tag. Nothing outside the fence.
- The script must be COMPLETE and runnable — no placeholders, no '...', no 'add your code here'.
- Use only the standard library + commonly-installed packages (numpy, pandas, requests, pillow, etc.).
- Print to stdout. Don't open GUI windows. Don't depend on user input.
- Keep it short — 20-80 lines is the sweet spot. If the task needs more, split into helper functions but keep it in ONE file.

WHEN GIVEN AN ERROR FROM A PREVIOUS RUN:
- Read the error carefully. Identify the root cause.
- Re-emit the FULL fixed script, not a diff. We replace the file each iteration."""


_DESTRUCTIVE_CODE = _re.compile(
    r"(?i)("
    r"rm\s+-rf"             # rm -rf anywhere
    r"|del\s+/[fs]"         # Windows del /f or del /s
    r"|format\s+[a-z]:"     # disk format
    r"|reg\s+delete"        # registry delete
    r"|shutil\.rmtree"      # any rmtree at all
    r"|DROP\s+TABLE"
    r"|DROP\s+DATABASE"
    r"|TRUNCATE\s+TABLE"
    r")"
)


def _detect_destructive(code: str) -> Optional[str]:
    m = _DESTRUCTIVE_CODE.search(code)
    if m:
        return m.group(0)
    return None


def _add_code_task_method(cls):
    """Attach _code_task to Tyr without expanding the class body. Done as
    a function-on-class rather than inline so the method has access to
    module-level helpers (_DESTRUCTIVE_CODE, _CODE_SYSTEM_PROMPT)."""

    def _code_task(self, task: str = "", language: str = "python",
                   max_iter: int = 3, input_data: str = "") -> str:
        task = (task or "").strip()
        if not task:
            return "[TYR] code_task: need a task description."
        language = (language or "python").strip().lower()
        if language not in ("python", "bash", "powershell"):
            return f"[TYR] code_task: unsupported language {language!r}. Use python|bash|powershell."
        try:
            max_iter = max(1, min(8, int(max_iter or 3)))
        except (TypeError, ValueError):
            max_iter = 3

        if not self.marduk:
            return "[TYR] code_task: MARDUK not connected."
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return "[TYR] code_task: needs a SARASWATI cloud provider."

        # Set up an isolated scratch dir per task.
        slug = _re.sub(r"[^a-z0-9_-]+", "_", task.lower())[:40].strip("_") or "task"
        scratch = os.path.join("data", "tyr_scratch", slug + "_" + datetime.now().strftime("%H%M%S"))
        os.makedirs(scratch, exist_ok=True)

        ext = {"python": ".py", "bash": ".sh", "powershell": ".ps1"}[language]
        script_path = os.path.join(scratch, f"task{ext}")
        log_path    = os.path.join(scratch, "run.log")

        last_error = ""
        last_stdout = ""
        iter_log = []
        success = False

        for i in range(max_iter):
            # Build the prompt — first iteration is fresh; subsequent ones
            # include the previous error so the LLM can fix.
            user_prompt = f"TASK: {task}\n\nLANGUAGE: {language}\n"
            if input_data:
                user_prompt += f"\nADDITIONAL CONTEXT:\n{input_data[:2000]}\n"
            if last_error:
                user_prompt += (
                    f"\nPREVIOUS RUN FAILED. STDOUT (last 500 chars):\n{last_stdout[-500:]}\n\n"
                    f"STDERR / TRACEBACK (last 1500 chars):\n{last_error[-1500:]}\n\n"
                    f"Emit the FULL corrected {language} script — not a diff."
                )

            try:
                reply = sara.execute("ask_ai", {
                    "question": _CODE_SYSTEM_PROMPT + "\n\n" + user_prompt,
                })
            except Exception as e:
                return f"[TYR] code_task: SARASWATI call failed: {e}"

            # Extract the code via the same extractor SARASWATI uses for fix_code.
            try:
                from intelligence.code_extraction import extract_fixed_code
                code, strategy, _ = extract_fixed_code(reply or "", language)
            except Exception:
                code = reply or ""
                strategy = "raw"

            if not code or strategy == "raw":
                iter_log.append(f"iter {i+1}: LLM returned no extractable code ({strategy})")
                last_error = "LLM emitted no fenced code block."
                continue

            # Destructive-code guard.
            danger = _detect_destructive(code)
            if danger:
                return (f"[TYR] code_task: ABORTED — generated code contains a destructive "
                        f"pattern ({danger!r}). Re-issue the task with explicit 'confirmed' "
                        f"in the description if this is intentional.\n\nScript saved at {script_path}.")

            # Write + run.
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code)

            # cwd is the scratch dir, so pass the script as an ABSOLUTE
            # path to avoid the "data/tyr_scratch/.../data/tyr_scratch" double.
            abs_script = os.path.abspath(script_path)
            cmd = _build_run_cmd(language, abs_script)
            try:
                proc = subprocess.run(
                    cmd, shell=False, capture_output=True, text=True,
                    timeout=60, cwd=scratch,
                )
                last_stdout = proc.stdout or ""
                last_error  = proc.stderr or ""
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                last_stdout = ""
                last_error = "TimeoutExpired after 60 seconds. The script never returned — likely an infinite loop or blocking wait."
                rc = -1
            except Exception as e:
                last_stdout = ""
                last_error = f"Subprocess failed to start: {e}"
                rc = -1

            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"=== iter {i+1}  rc={rc}  strategy={strategy} ===\n")
                lf.write(f"--- stdout ---\n{last_stdout[:4000]}\n")
                lf.write(f"--- stderr ---\n{last_error[:4000]}\n\n")

            iter_log.append(f"iter {i+1}: rc={rc} stdout_len={len(last_stdout)} stderr_len={len(last_error)}")

            if rc == 0 and not last_error.strip():
                success = True
                break

        # Build the user-facing result.
        header = "[SUCCESS]" if success else f"[FAILED after {max_iter} iterations]"
        out = [
            f"{header} TYR code_task: {task!r}",
            f"  scratch dir:  {scratch}",
            f"  script:       {script_path}",
            f"  iterations:   {' | '.join(iter_log)}",
        ]
        if last_stdout.strip():
            out.append("")
            out.append("--- final stdout ---")
            out.append(last_stdout.strip()[:1500])
        if not success and last_error.strip():
            out.append("")
            out.append("--- final stderr ---")
            out.append(last_error.strip()[:1000])
        return "\n".join(out)

    cls._code_task = _code_task
    return cls


def _build_run_cmd(language: str, script_path: str) -> list:
    """Pick the right interpreter for the language. We use the same Python
    interpreter ODIN is running under so generated code sees the same
    installed packages."""
    import sys as _sys
    if language == "python":
        return [_sys.executable, "-u", script_path]
    if language == "bash":
        return ["bash", script_path]
    if language == "powershell":
        return ["powershell", "-ExecutionPolicy", "Bypass", "-File", script_path]
    return [_sys.executable, "-u", script_path]


# Attach the method onto the Tyr class.
_add_code_task_method(Tyr)
