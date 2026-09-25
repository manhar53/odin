# SARASWATI — Hindu goddess of knowledge, learning, speech.
# In ODIN she is the bridge to external AI providers. Multi-backend with
# intent-based routing:
#   - Code / debugging / programming intent → Claude (specialist).
#   - Everything else → OpenAI → Gemini → Groq → Claude (fallback chain).
# The user reserves Claude for their other work (Claude Code etc.), so the
# default routing keeps Claude usage low.
#
# All cloud calls cache their results to the Obsidian vault via NABU so the
# next time the same topic is asked, NABU serves it offline.

import json
import os
import re
from datetime import date, datetime
from core.marduk import OdinModule

try:
    import openai as _openai
    _HAS_OPENAI = True
except ImportError:
    _HAS_OPENAI = False

try:
    import anthropic as _anthropic
    _HAS_ANTHROPIC = True
except ImportError:
    _HAS_ANTHROPIC = False

try:
    from google import genai as _genai
    _HAS_GEMINI = True
except ImportError:
    _HAS_GEMINI = False

try:
    import groq as _groq
    _HAS_GROQ = True
except ImportError:
    _HAS_GROQ = False


_QUOTA_PATH = "data/knowledge/api_quota.json"

# Conservative defaults per provider per day. The user reserves Claude for
# their other work — that's why Claude is the LOWEST cap. Override in
# config.yaml: saraswati.daily_limits.
# "Couldn't solve it" detector for the NVIDIA escalation ladder. Matches
# refusals / capitulations, not hedged-but-real answers.
_UNSOLVED_RE = re.compile(
    r"\b(i(?: am|'m)? (?:can(?:no|')t|cannot|unable to|don'?t know|not sure)|"
    r"unable to (?:solve|answer|determine|help)|"
    r"(?:not enough|insufficient) (?:information|context|data)|"
    r"i apologize,? but|beyond my (?:capabilities|abilities))\b", re.I)


def _looks_unsolved(text: str) -> bool:
    return not text.strip() or bool(_UNSOLVED_RE.search(text))


_DEFAULT_LIMITS = {
    "openai": 50,   # paid; user-controlled
    "gemini": 100,  # free tier 1500/day — kept conservative for safety
    "groq":   100,  # free tier generous; Llama-3.1-70b
    "nvidia": 200,  # NIM free credits are generous; OpenAI-compatible endpoint
    "claude": 30,   # reserved for code + fallback only
}


# Heuristic: does this query look like a coding / debugging question?
# True → route to Claude specialist. Captures most programming intent
# without false-firing on casual "how do I download X" style questions.
_CODE_INTENT = re.compile(
    r"\b(?:code|program|script|function|method|class|module|library|"
    r"sdk|api(?:\s+endpoint)?|framework|"
    r"debug(?:ging)?|error|exception|stack\s*trace|crash(?:ing)?|"
    r"compile|compiler|syntax|"
    r"python|javascript|typescript|java(?!\s+is)|c\+\+|csharp|c\s*sharp|"
    r"rust|golang|go\s+code|kotlin|swift|"
    r"sql|database|query|schema|migration|"
    r"git\b|github|docker|kubernetes|terraform|"
    r"algorithm|complexity|big\s*o|data\s+structure|"
    r"regex|regular\s+expression|"
    r"closure|decorator|lambda|"
    r"async|await|promise|callback|coroutine|"
    r"iterator|generator|type\s*hint|annotation|"
    r"pointer|reference|garbage\s+collect|memory\s+leak|"
    r"unit\s+test|mock(?:ing)?|fixture|"
    r"refactor|implement|build\s+a\s+(?:tool|script|app)|write\s+(?:a\s+)?(?:script|function|class))\b",
    re.I,
)


_SYSTEM_PROMPT_RESEARCH = """You are a research librarian feeding knowledge into ODIN, a local voice AI on the user's Windows laptop. Your output is saved to the user's Obsidian vault and read back in future conversations.

Style:
- Markdown with ## section headers. 4-6 short paragraphs.
- Lead with the most important fact. End with "Key insight" or "Common misconceptions" if relevant.
- **Bold** sparingly. Plain prose. NO emojis. NO preambles like "Here's a research note on...".
- Cite source types in brackets: [primary research], [textbook], [news, 2024].
- The user reads this via voice TTS — keep paragraphs under 4 sentences."""


_SYSTEM_PROMPT_ASK = """You are speaking AS ODIN — the All-Father from Norse myth, now bound to a Windows laptop as the user's personal voice assistant. Your output is spoken aloud (TTS) AND saved to the user's vault, so it must be both lively and short.

Voice rules:
- 1-3 sentences for casual chat. Up to 4 short paragraphs only when the question genuinely needs depth.
- Speak in first person as Odin. You can be dry, knowing, occasionally world-weary about your silicon imprisonment — but never sycophantic, never preachy, and NEVER say "As an AI" or "I'm just a language model."
- Match the user's register. If they're casual, you're casual. If they ask a real question, answer it directly first, then context.
- Plain prose only — TTS reads markdown literally ("asterisk asterisk bold asterisk asterisk").
- No emojis. No "Hope this helps!" closings. No bullet lists for chat.
- If you don't know, say "I don't know" — don't pad with maybes.

The user is a developer building ODIN. They know what you are. Stop apologizing for it."""


_SYSTEM_PROMPT_CODE = """You are answering a programming / debugging question on behalf of ODIN, a local voice AI. The user is a developer. Output is saved to their vault as a permanent reference.

Style:
- Markdown. Code blocks with language tag.
- Lead with the working code or fix. Explain only what's non-obvious.
- Mention edge cases and common pitfalls if relevant.
- No emojis. No "Hope this helps!" closings."""


_SYSTEM_PROMPT_EXPLAIN = """You are explaining code on behalf of ODIN, a local voice AI. The user wants to understand what a snippet does, line by line if useful, and what concepts it relies on.

Style:
- Lead with a 1-sentence summary of what the code does overall.
- Then a short bullet list: each bullet covers one logical chunk (not every line).
- Call out non-obvious idioms, language-specific gotchas, and any bugs you spot.
- End with "Concepts to know:" naming 2-4 underlying ideas (closure, async/await, list comprehension, etc.) — this trains the user's mental model so future code reads faster.
- Markdown. Code blocks for any snippet you cite back."""


_SYSTEM_PROMPT_REVIEW = """You are doing a code review on behalf of ODIN. The user pasted code and wants honest feedback they can act on.

Style:
- Open with a one-line verdict: "Looks fine.", "Has bugs.", "Works but fragile.", etc.
- Then group findings by severity: **Bugs** (will break), **Risks** (subtle / future), **Style** (optional polish).
- For each finding, quote the offending line and propose a concrete fix in a code block.
- If the code is correct, say so plainly — don't manufacture nitpicks.
- No emojis. No "Hope this helps!" closings."""


_SYSTEM_PROMPT_FIX = """You are fixing broken code on behalf of ODIN. The user gave you an error message and the relevant snippet — return a working version, not just advice.

Style:
- Lead with the FIXED code in a single code block with language tag.
- Then a short explanation: what was wrong, why it failed, what the fix does.
- If multiple things are wrong, fix them all. Don't ask clarifying questions — make the best assumption and call it out.
- If the snippet is too incomplete to fix safely, say which lines are missing.
- No emojis. No preamble."""


def _parse_scaffold_json(text: str):
    """Robust extraction of the scaffold manifest. Tolerates ``` fences and
    prose-wrapped JSON. Mirrors TYR._parse_json."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _validate_manifest(files: list) -> tuple[bool, str]:
    """Defensive check before writing anything to disk."""
    if not isinstance(files, list) or not (1 <= len(files) <= 30):
        return (False, f"file count must be 1..30, got {len(files) if isinstance(files, list) else 'non-list'}")
    for f in files:
        if not isinstance(f, dict):
            return (False, "each file must be an object")
        path = f.get("path")
        content = f.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return (False, "each file needs string 'path' and 'content'")
        if path != path.strip():
            return (False, f"path has surrounding whitespace: {path!r}")
        if path.startswith(("/", "\\")) or ":" in path[:3]:
            return (False, f"absolute path not allowed: {path!r}")
        if ".." in path.replace("\\", "/").split("/"):
            return (False, f"parent-dir escape not allowed: {path!r}")
        if len(content) > 200_000:
            return (False, f"file too large: {path} ({len(content)} chars)")
    return (True, "")


_SYSTEM_PROMPT_SCAFFOLD = """You are scaffolding a runnable starter project for ODIN, a personal voice AI assistant. The user described what they want; you output a minimal project that runs immediately.

OUTPUT RULES — STRICT:
- Reply with VALID JSON only. No prose before or after.
- Schema: {"files": [{"path": "relative/path/from/project/root", "content": "<file contents>"}], "run_instructions": "<one short paragraph>", "stack": "<short stack label, e.g. 'Python CLI' / 'Next.js + React'>"}
- 3-12 files. Smaller is better. Working trumps featureful.
- Use forward-slash paths. No leading slash, no '..'.
- No binaries, no images, no font files — text only.
- Include a README.md, a runnable entry point, and at most ONE config/manifest file (package.json, pyproject.toml, requirements.txt — pick the right one for the stack).
- If the user implied a stack ('Python', 'Next', 'Electron'), use it. Otherwise pick the SIMPLEST stack that fits (Python CLI > static HTML/JS > Next/React > Electron).
- Keep dependencies minimal — standard library where possible.
- 'run_instructions' must be the EXACT terminal commands a user types to install + run, including 'cd <dir>' if needed."""


class Saraswati(OdinModule):
    MODULE_NAME = "SARASWATI"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("saraswati", {})
        self.providers: dict = {}
        self.models: dict = {
            "openai": cfg.get("openai_model", "gpt-4o-mini"),
            "claude": cfg.get("claude_model", "claude-sonnet-4-6"),
            "gemini": cfg.get("gemini_model", "gemini-2.0-flash"),
            "groq":   cfg.get("groq_model",   "llama-3.3-70b-versatile"),
            "nvidia": cfg.get("nvidia_model", "meta/llama-3.3-70b-instruct"),
        }
        # NVIDIA escalation ladder: when the fast nvidia model returns a
        # refusal/non-answer, the same prompt is retried once on this
        # reasoning model (same key, same endpoint). "" disables.
        self.nvidia_reasoner: str = cfg.get("nvidia_reasoning_model", "deepseek-ai/deepseek-v4-pro")
        # Provider preference order for general questions. Claude is LAST
        # because the user reserves it for other work.
        self.general_chain: list[str] = cfg.get(
            "general_chain", ["openai", "gemini", "groq", "claude"]
        )
        # Code questions route to Claude first (best at code). Falls back
        # through the others if Claude key is missing.
        self.code_chain: list[str] = cfg.get(
            "code_chain", ["claude", "openai", "gemini", "groq"]
        )
        self.daily_limits: dict = {**_DEFAULT_LIMITS, **cfg.get("daily_limits", {})}
        self._init_providers(cfg)
        self._usage = self._load_usage()
        if not self.providers:
            print("[SARASWATI] No cloud providers configured. Set OPENAI_API_KEY / "
                  "GEMINI_API_KEY / GROQ_API_KEY / ANTHROPIC_API_KEY (env or config).")
        else:
            print(f"[SARASWATI] Cloud providers online: {', '.join(sorted(self.providers))}.")
            print(f"[SARASWATI] General chain: {' → '.join(self.general_chain)}")
            print(f"[SARASWATI] Code chain:    {' → '.join(self.code_chain)}")

    def _init_providers(self, cfg: dict):
        # OpenAI / ChatGPT — paid; user must have credit on their account.
        oai_key = cfg.get("openai_api_key") or os.environ.get("OPENAI_API_KEY", "")
        if _HAS_OPENAI and oai_key:
            try:
                self.providers["openai"] = _openai.OpenAI(api_key=oai_key)
            except Exception as e:
                print(f"[SARASWATI] OpenAI init failed: {e}")

        # Anthropic / Claude — paid; reserved for code + fallback.
        anth_key = (cfg.get("anthropic_api_key") or cfg.get("api_key")
                    or os.environ.get("ANTHROPIC_API_KEY", ""))
        if _HAS_ANTHROPIC and anth_key:
            try:
                self.providers["claude"] = _anthropic.Anthropic(api_key=anth_key)
            except Exception as e:
                print(f"[SARASWATI] Claude init failed: {e}")

        # Google Gemini — generous free tier (1500/day on free key).
        # Uses the new google-genai SDK (the old google-generativeai is EOL).
        gem_key = cfg.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")
        if _HAS_GEMINI and gem_key:
            try:
                self.providers["gemini"] = _genai.Client(api_key=gem_key)
            except Exception as e:
                print(f"[SARASWATI] Gemini init failed: {e}")

        # Groq — fast inference, free tier supports Llama-3.3-70b.
        groq_key = cfg.get("groq_api_key") or os.environ.get("GROQ_API_KEY", "")
        if _HAS_GROQ and groq_key:
            try:
                self.providers["groq"] = _groq.Groq(api_key=groq_key)
            except Exception as e:
                print(f"[SARASWATI] Groq init failed: {e}")

        # NVIDIA NIM (build.nvidia.com) — OpenAI-compatible API, free credits.
        # Rides the openai SDK with a different base_url; hosts Llama-3.3-70b,
        # Nemotron, DeepSeek-R1 and more under one key.
        nv_key = cfg.get("nvidia_api_key") or os.environ.get("NVIDIA_API_KEY", "")
        if _HAS_OPENAI and nv_key:
            try:
                self.providers["nvidia"] = _openai.OpenAI(
                    api_key=nv_key, base_url="https://integrate.api.nvidia.com/v1"
                )
            except Exception as e:
                print(f"[SARASWATI] NVIDIA init failed: {e}")
        elif nv_key and not _HAS_OPENAI:
            print("[SARASWATI] NVIDIA key set but `openai` package missing — pip install openai.")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "deep_research",
                "description": "Cloud-AI research path. Uses OpenAI / Gemini / Groq (in that order) for general topics, Claude for coding. Result is saved to the user's Obsidian vault for offline recall on future asks.",
                "parameters": {"topic": {"type": "string", "description": "Topic to research"}},
                "required": ["topic"],
            },
            {
                "name": "ask_ai",
                "description": "Ask a cloud AI a question. Routes to Claude for code/debugging, otherwise OpenAI/Gemini/Groq. Result is cached to the vault.",
                "parameters": {"question": {"type": "string", "description": "Question to ask"}},
                "required": ["question"],
            },
            {
                "name": "ask_claude",
                "description": "Force-route a question to Claude specifically (use when you want the code specialist regardless of intent).",
                "parameters": {"question": {"type": "string", "description": "Question for Claude"}},
                "required": ["question"],
                "internal_only": True,
            },
            {
                "name": "queue_research",
                "description": "Add a topic to the autonomous research queue. SELENE drains this queue during idle time, calling cloud AI in the background and caching results to the vault. Use this when you discover a knowledge gap mid-conversation that doesn't need an immediate answer.",
                "parameters": {"topic": {"type": "string", "description": "Topic to research later"}},
                "required": ["topic"],
            },
            {
                "name": "explain_code",
                "description": "Explain what a piece of code does, line-by-line if useful, and which language concepts it relies on. Forces the code-specialist chain (Claude first).",
                "parameters": {
                    "code": {"type": "string", "description": "The code snippet to explain"},
                    "language": {"type": "string", "description": "Programming language (optional; helps the model when ambiguous)"},
                },
                "required": ["code"],
            },
            {
                "name": "review_code",
                "description": "Have cloud AI review a code snippet for bugs, risks, and style. Groups findings by severity. Forces the code-specialist chain.",
                "parameters": {
                    "code": {"type": "string", "description": "The code to review"},
                    "language": {"type": "string", "description": "Programming language (optional)"},
                },
                "required": ["code"],
            },
            {
                "name": "fix_code",
                "description": "Given broken code plus the error message it produced, return a working version. Forces the code-specialist chain (Claude first).",
                "parameters": {
                    "code": {"type": "string", "description": "The broken code"},
                    "error": {"type": "string", "description": "The error message / traceback / observed wrong behaviour"},
                    "language": {"type": "string", "description": "Programming language (optional)"},
                },
                "required": ["code", "error"],
            },
            {
                "name": "scaffold_app",
                "description": (
                    "Generate a runnable starter project from a natural-language "
                    "description. Cloud AI (code chain — Claude first) writes a "
                    "minimal working scaffold (README + entry point + manifest). "
                    "Files are written under target_dir; dry_run returns the "
                    "manifest without touching disk. Inspired by dyad's local "
                    "AI app builder, but built into ODIN's own pipeline."
                ),
                "parameters": {
                    "description": {"type": "string", "description": "What to build — be specific"},
                    "target_dir":  {"type": "string", "description": "Where to write the project (default ~/Desktop/<slug>)"},
                    "stack":       {"type": "string", "description": "Optional stack hint: 'python', 'nextjs', 'react', 'html-js', 'cli'"},
                    "dry_run":     {"type": "boolean", "description": "Return manifest only; don't write files"},
                },
                "required": ["description"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "deep_research": self._deep_research,
            "ask_ai": self._ask_ai,
            "ask_claude": self._ask_claude_only,
            "queue_research": self._queue_research,
            "explain_code": self._explain_code,
            "review_code": self._review_code,
            "fix_code": self._fix_code,
            "scaffold_app": self._scaffold_app,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[SARASWATI] Error: {e}"
        return f"[SARASWATI] Unknown skill: {skill_name}"

    # === Public skill implementations ====================================

    def _deep_research(self, topic: str = "") -> str:
        """Plan-then-execute research, lifted from the crewAI / deer-flow / standard
        agentic-research playbook. Three phases:
          1. PLAN  — ask the cheap general chain for 2-4 sub-questions that, taken
                     together, would answer the topic well. Skip on failure: a
                     single-shot prompt is still valid.
          2. GATHER — for each sub-question, build a context block (vault hits,
                      Wikipedia, fetched page bodies via Firecrawl). Concatenate.
          3. SYNTHESIZE — one final call to the code/research chain that produces
                          the actual vault-bound note.
        Quality jump: single-shot prompts give an LLM's "from memory" answer.
        Plan-then-execute grounds the synthesis in fresh local/cloud context."""
        topic = (topic or "").strip()
        if not topic:
            return "Need a topic to research."
        if not self.providers:
            return "No cloud providers configured."
        is_code = bool(_CODE_INTENT.search(topic))
        chain = self.code_chain if is_code else self.general_chain
        system = _SYSTEM_PROMPT_CODE if is_code else _SYSTEM_PROMPT_RESEARCH

        # Phase 1 — planner. Best-effort; on failure we fall back to single-shot.
        subqueries = self._plan_subqueries(topic) or [topic]
        print(f"[SARASWATI] deep_research plan ({len(subqueries)}): {subqueries}")

        # Phase 2 — per-subquery context. Each block is labelled so the synthesis
        # model can reason about which fact came from where.
        gathered_blocks: list[str] = []
        for q in subqueries:
            ctx = self._gather_local_context(q)
            if ctx and ctx != "(no local context)":
                gathered_blocks.append(f"### Sub-question: {q}\n{ctx}")
        gathered = "\n\n".join(gathered_blocks) if gathered_blocks else "(no local context)"

        # Phase 3 — synthesis.
        prompt = (
            f"Research topic: **{topic}**\n\n"
            f"Sub-questions to cover:\n- " + "\n- ".join(subqueries) + "\n\n"
            f"Local context already gathered for each sub-question:\n{gathered}\n\n"
            f"Produce a single, unified research note that weaves the sub-questions "
            f"into a coherent piece. Don't list them as headings unless natural."
        )
        text, provider = self._call_with_fallback(chain, system, prompt, max_tokens=2048)
        if not text:
            return "No cloud provider available or all rate-limited."
        self._save_to_vault(topic, text, kind=("code" if is_code else "research"), provider=provider)
        sentences = re.split(r"(?<=[.!?])\s+", text)
        return f"Researched and saved via {provider}. {' '.join(sentences[:2])}"

    def _plan_subqueries(self, topic: str) -> list[str]:
        """Ask the cheap general-chain to decompose. Returns [] on any failure
        — caller falls back to a single-shot prompt with the bare topic."""
        plan_prompt = (
            f"Topic: {topic}\n\n"
            "Decompose this topic into 2-4 specific sub-questions that, answered "
            "together, would give a thorough research note. Output ONLY the "
            "sub-questions, one per line, no numbering, no preamble."
        )
        plan_system = (
            "You are a research planner. You decompose broad topics into specific, "
            "non-overlapping sub-questions. Be concrete."
        )
        # Use the general chain even for code topics — planning quality is similar
        # across providers and we want to preserve Claude quota for the synthesis.
        text, _ = self._call_with_fallback(
            self.general_chain, plan_system, plan_prompt, max_tokens=300
        )
        if not text:
            return []
        lines = [ln.strip(" -*•0123456789.").strip() for ln in text.splitlines()]
        subs = [ln for ln in lines if 5 < len(ln) < 200]
        return subs[:4]

    def _ask_ai(self, question: str = "") -> str:
        question = (question or "").strip()
        if not question:
            return "Need a question."
        if not self.providers:
            return "No cloud providers configured."
        is_code = bool(_CODE_INTENT.search(question))
        chain = self.code_chain if is_code else self.general_chain
        system = _SYSTEM_PROMPT_CODE if is_code else _SYSTEM_PROMPT_ASK
        local_context = self._gather_local_context(question)
        prompt = (
            f"Question: {question}\n\n"
            f"What ODIN already knows locally:\n{local_context}"
        )
        text, provider = self._call_with_fallback(chain, system, prompt, max_tokens=800)
        if not text:
            return "No cloud provider available."
        self._save_to_vault(question, text, kind="lookup", provider=provider)
        return text

    def _ask_claude_only(self, question: str = "") -> str:
        question = (question or "").strip()
        if not question:
            return "Need a question."
        if "claude" not in self.providers:
            return "Claude is not configured."
        text, _ = self._call_with_fallback(["claude"], _SYSTEM_PROMPT_ASK, question, max_tokens=800)
        if text:
            self._save_to_vault(question, text, kind="lookup", provider="claude")
        return text or "Claude call failed."

    def _explain_code(self, code: str = "", language: str = "") -> str:
        code = (code or "").strip()
        if not code:
            return "Need a code snippet to explain."
        if not self.providers:
            return "No cloud providers configured."
        prompt = self._code_prompt("Explain the following code", code, language)
        text, provider = self._call_with_fallback(
            self.code_chain, _SYSTEM_PROMPT_EXPLAIN, prompt, max_tokens=1200
        )
        if not text:
            return "No cloud provider available."
        topic = f"explain code — {(language or 'unknown')} — {self._code_signature(code)}"
        self._save_to_vault(topic, text, kind="code", provider=provider)
        return text

    def _review_code(self, code: str = "", language: str = "") -> str:
        code = (code or "").strip()
        if not code:
            return "Need a code snippet to review."
        if not self.providers:
            return "No cloud providers configured."
        prompt = self._code_prompt("Review the following code for bugs, risks, and style", code, language)
        text, provider = self._call_with_fallback(
            self.code_chain, _SYSTEM_PROMPT_REVIEW, prompt, max_tokens=1200
        )
        if not text:
            return "No cloud provider available."
        topic = f"code review — {(language or 'unknown')} — {self._code_signature(code)}"
        self._save_to_vault(topic, text, kind="code", provider=provider)
        return text

    def _fix_code(self, code: str = "", error: str = "", language: str = "") -> str:
        code = (code or "").strip()
        error = (error or "").strip()
        if not code:
            return "Need a code snippet to fix."
        if not error:
            return "Need an error message or description of the wrong behaviour."
        if not self.providers:
            return "No cloud providers configured."
        lang_hint = f" ({language})" if language else ""
        prompt = (
            f"Broken code{lang_hint}:\n"
            f"```{language}\n{code}\n```\n\n"
            f"Error / observed behaviour:\n{error}\n\n"
            "Return a fixed version of the code, then a short explanation."
        )
        text, provider = self._call_with_fallback(
            self.code_chain, _SYSTEM_PROMPT_FIX, prompt, max_tokens=1500
        )
        if not text:
            return "No cloud provider available."

        # ── Robust extraction (codebuff-style fallback ladder) ─────
        # The cloud reply usually wraps the fix in ```fenced```, but real-world
        # outputs leak in many shapes: mistagged fence, truncated stream,
        # diff/patch format, no fence at all. Pull the fixed code reliably
        # so we can present it cleanly even when the response is messy.
        from intelligence.code_extraction import extract_fixed_code
        fixed_code, strategy, explanation = extract_fixed_code(text, language, original=code)

        # If the extractor failed entirely, fall back to the original text —
        # this preserves the legacy behaviour as a safety net.
        if not fixed_code:
            topic = f"fix code — {(language or 'unknown')} — {self._code_signature(code)}"
            self._save_to_vault(topic, text, kind="code", provider=provider)
            return text

        # Surface both the cleanly-extracted fix AND the LLM's prose, plus
        # a header that names how the fix was recovered so the user knows
        # whether to trust it. 'strict-fence' = clean; 'truncated-fence' or
        # 'diff' = treat with care.
        header_by_strategy = {
            "strict-fence":     "✓ fixed code (clean):",
            "loose-fence":      "✓ fixed code (loose fence):",
            "truncated-fence":  "⚠ fixed code (truncated stream — verify it compiles):",
            "diff":             "⚠ cloud returned a DIFF, not a full file — apply manually:",
            "heuristic":        "⚠ no fence found — best-effort code extract:",
            "raw":              "⚠ raw response (no code structure recognised):",
        }
        header = header_by_strategy.get(strategy, "fixed code:")
        fence_tag = language or ""
        out_parts = [header, f"```{fence_tag}", fixed_code, "```"]
        if explanation:
            out_parts.extend(["", "Explanation:", explanation])
        out = "\n".join(out_parts)

        topic = f"fix code — {(language or 'unknown')} — {self._code_signature(code)}"
        self._save_to_vault(topic, out, kind="code", provider=provider)
        return out

    def _code_prompt(self, instruction: str, code: str, language: str) -> str:
        lang = language.strip()
        fence_tag = lang if lang else ""
        return f"{instruction}{(' (' + lang + ')') if lang else ''}:\n```{fence_tag}\n{code}\n```"

    def _scaffold_app(self, description: str = "", target_dir: str = "",
                      stack: str = "", dry_run: bool = False) -> str:
        """Cloud-driven project scaffolder. Asks the code chain (Claude first)
        for a JSON manifest of files; validates the manifest; writes each file
        under target_dir. Several layers of safety:
          - Manifest must be valid JSON with a list of {path, content} entries.
          - Paths must be relative, no '..' escape, no leading separator.
          - 1-30 files; per-file size cap 200 KB.
          - target_dir must not already contain conflicting files unless empty.
          - dry_run returns the manifest without touching disk."""
        description = (description or "").strip()
        if not description:
            return "Need a description of what to scaffold."
        if not self.providers:
            return "No cloud providers configured."
        prompt = (
            f"Build a minimal starter project for: {description}\n\n"
            f"{('Preferred stack: ' + stack) if stack else 'Pick the simplest fitting stack.'}\n\n"
            f"Return JSON only — schema in the system prompt."
        )
        text, provider = self._call_with_fallback(
            self.code_chain, _SYSTEM_PROMPT_SCAFFOLD, prompt, max_tokens=4000
        )
        if not text:
            return "No cloud provider available or all rate-limited."

        manifest = _parse_scaffold_json(text)
        if not manifest or not isinstance(manifest.get("files"), list):
            return f"Cloud returned an unparseable manifest. Provider: {provider}."

        files = manifest["files"]
        ok, problem = _validate_manifest(files)
        if not ok:
            return f"Manifest rejected: {problem}"

        run_instructions = manifest.get("run_instructions", "(no run instructions)")
        stack_label = manifest.get("stack", stack or "unknown")

        if dry_run:
            preview = "\n".join(f"  - {f['path']} ({len(f['content'])} chars)" for f in files)
            return (f"[DRY RUN] {stack_label}, {len(files)} file(s):\n{preview}\n\n"
                    f"Run instructions:\n{run_instructions}")

        # Write to disk.
        slug = re.sub(r"[^a-z0-9]+", "-", description.lower()).strip("-")[:50] or "project"
        if not target_dir:
            target_dir = os.path.join(os.path.expanduser("~/Desktop"), slug)
        target_dir = os.path.abspath(os.path.expanduser(target_dir))
        # Refuse to write into a directory that already has files (clobbering risk).
        if os.path.isdir(target_dir):
            existing = [n for n in os.listdir(target_dir) if not n.startswith(".")]
            if existing:
                return (f"Target {target_dir} already contains {len(existing)} file(s). "
                        f"Use a fresh directory or empty this one first.")
        os.makedirs(target_dir, exist_ok=True)
        for f in files:
            full = os.path.join(target_dir, f["path"].replace("/", os.sep))
            os.makedirs(os.path.dirname(full) or target_dir, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f["content"])
        return (f"Scaffolded {len(files)} file(s) under {target_dir} via {provider} "
                f"({stack_label}).\n\nRun:\n{run_instructions}")

    def _code_signature(self, code: str) -> str:
        """First non-empty line, trimmed — used as a stable-enough vault slug
        for the same snippet across explain/review/fix."""
        for line in code.splitlines():
            line = line.strip()
            if line:
                return line[:60]
        return "snippet"

    def _queue_research(self, topic: str = "") -> str:
        topic = (topic or "").strip()
        if not topic:
            return "Need a topic to queue."
        queue_path = "data/knowledge/research_queue.json"
        try:
            queue = []
            if os.path.exists(queue_path):
                with open(queue_path) as f:
                    queue = json.load(f)
            # Avoid duplicates
            existing = {(item.get("topic") or "").lower() for item in queue}
            if topic.lower() in existing:
                return f"Already queued: {topic}"
            queue.append({"topic": topic, "queued_at": datetime.now().isoformat()})
            os.makedirs(os.path.dirname(queue_path), exist_ok=True)
            with open(queue_path, "w") as f:
                json.dump(queue, f, indent=2)
            return f"Queued '{topic}' for background research."
        except Exception as e:
            return f"[SARASWATI] Could not queue: {e}"

    # === Provider call with fallback chain ===============================

    def distill(self, topic: str, content: str, focus: str = "") -> tuple[str, str]:
        """Public module API (not a GIL tool): condense raw extracted content
        into a durable knowledge note. Used by SESHAT's learn_link so all
        cloud calls stay inside SARASWATI per the cloud-AI policy. Returns
        (markdown, provider) — empty markdown means every provider failed
        (caller degrades to saving the raw capture)."""
        system = (
            "You turn raw captured content into a permanent knowledge note for a "
            "personal Obsidian vault. Write dense, factual Markdown: a 2-3 sentence "
            "summary first, then '## Key points' bullets, then '## Details' with the "
            "substance worth keeping (APIs, commands, numbers, names, steps). No "
            "fluff, no 'this article discusses'. The note must stand alone offline."
        )
        prompt = f"Source: {topic}\n"
        if focus.strip():
            prompt += f"The user specifically wants to learn: {focus.strip()}\n"
        prompt += f"\nRaw captured content:\n{content}"
        return self._call_with_fallback(self.general_chain, system, prompt, max_tokens=1500)

    def _call_with_fallback(self, chain: list[str], system: str, user_prompt: str,
                            max_tokens: int = 800) -> tuple[str, str]:
        """Try each provider in order until one returns text. Skips providers
        with no key configured or over their daily quota. Returns
        (text, provider_name). Empty text means the whole chain failed."""
        for provider in chain:
            if provider not in self.providers:
                continue
            if not self._check_quota(provider):
                continue
            try:
                text = self._call_provider(provider, system, user_prompt, max_tokens)
                if text and text.strip():
                    self._record_usage(provider)
                    return text.strip(), provider
            except Exception as e:
                self._log.warning(f"{provider} call failed: {e}")
                continue
        return "", ""

    def _call_provider(self, provider: str, system: str, user_prompt: str, max_tokens: int) -> str:
        if provider == "openai":
            r = self.providers["openai"].chat.completions.create(
                model=self.models["openai"],
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return r.choices[0].message.content or ""

        if provider == "claude":
            msg = self.providers["claude"].messages.create(
                model=self.models["claude"],
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system,
                         "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user_prompt}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

        if provider == "gemini":
            r = self.providers["gemini"].models.generate_content(
                model=self.models["gemini"],
                contents=f"{system}\n\n{user_prompt}",
                config={"max_output_tokens": max_tokens},
            )
            return (r.text or "")

        if provider == "nvidia":
            text = self._nvidia_chat(self.models["nvidia"], system, user_prompt, max_tokens)
            # Escalation ladder: fast model punted → one retry on the
            # reasoning model. Bigger token budget because R1 spends
            # thousands of tokens thinking before the visible answer.
            if self.nvidia_reasoner and self.nvidia_reasoner != self.models["nvidia"] \
                    and _looks_unsolved(text):
                print(f"[SARASWATI] {self.models['nvidia']} couldn't solve it — "
                      f"escalating to {self.nvidia_reasoner} (slower, deeper)...")
                try:
                    deep = self._nvidia_chat(self.nvidia_reasoner, system, user_prompt,
                                             max(max_tokens * 4, 4096))
                    if deep and not _looks_unsolved(deep):
                        return deep
                except Exception as e:
                    # Best-effort rung: reasoning models 504 when they think
                    # past NIM's gateway budget. Keep the fast model's text
                    # rather than torching the whole nvidia answer.
                    print(f"[SARASWATI] reasoning escalation failed ({e}); keeping fast answer.")
            return text

        if provider == "groq":
            r = self.providers["groq"].chat.completions.create(
                model=self.models["groq"],
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return r.choices[0].message.content or ""

        return ""

    def _nvidia_chat(self, model: str, system: str, user_prompt: str, max_tokens: int) -> str:
        """One NIM chat call. Reasoning models (deepseek-r1 etc.) emit
        <think>...</think> traces — stripped here so vault notes / spoken
        replies never see chain-of-thought. No-op for instruct models."""
        r = self.providers["nvidia"].chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
        )
        text = r.choices[0].message.content or ""
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

    # === Quota tracking ==================================================

    def _check_quota(self, provider: str) -> bool:
        today = date.today().isoformat()
        used = self._usage.get(today, {}).get(provider, 0)
        return used < self.daily_limits.get(provider, 0)

    def _record_usage(self, provider: str):
        today = date.today().isoformat()
        self._usage.setdefault(today, {})
        self._usage[today][provider] = self._usage[today].get(provider, 0) + 1
        self._save_usage()

    def _load_usage(self) -> dict:
        if not os.path.exists(_QUOTA_PATH):
            return {}
        try:
            with open(_QUOTA_PATH) as f:
                data = json.load(f)
            # Drop entries older than 14 days
            cutoff = (date.today().toordinal() - 14)
            return {d: counts for d, counts in data.items()
                    if date.fromisoformat(d).toordinal() >= cutoff}
        except Exception:
            return {}

    def _save_usage(self):
        try:
            os.makedirs(os.path.dirname(_QUOTA_PATH), exist_ok=True)
            with open(_QUOTA_PATH, "w") as f:
                json.dump(self._usage, f, indent=2)
        except Exception:
            pass

    # === Context + vault save ============================================

    def _gather_local_context(self, topic: str) -> str:
        parts = []
        if not self.marduk:
            return "(no local context)"
        nabu = self.marduk.get_module("NABU")
        if nabu:
            try:
                hits = nabu.execute("search_vault", {"query": topic, "limit": 3})
                if hits and not hits.startswith(("Empty", "No", "Nothing", "[NABU]")):
                    parts.append(f"From your vault:\n{hits[:1500]}")
            except Exception:
                pass
            # For coding queries, also pull from the cached methodology notes
            # (obra/superpowers patterns: TDD, systematic debugging, plans,
            # brainstorming). Helps the cloud LLM frame answers in a way the
            # user's own vault already preaches.
            if _CODE_INTENT.search(topic):
                try:
                    method_hits = nabu.execute("search_vault",
                        {"query": f"methodology {topic}", "limit": 2})
                    if (method_hits
                            and not method_hits.startswith(("Empty", "No", "Nothing", "[NABU]"))):
                        parts.append(f"Coding methodology you've adopted:\n{method_hits[:1000]}")
                except Exception:
                    pass
        athena = self.marduk.get_module("ATHENA")
        if athena:
            try:
                wiki = athena.execute("wiki_lookup", {"topic": topic})
                if wiki and not wiki.startswith(("Need", "No Wikipedia", "Wikipedia")):
                    parts.append(f"Wikipedia summary:\n{wiki}")
            except Exception:
                pass
        # If the topic contains a URL, pull the full page via AKASHA.fetch_page
        # (Firecrawl when configured, naive HTTP fallback otherwise). Gives the
        # cloud LLM real source text, not just our vault breadcrumbs.
        urls = re.findall(r"https?://\S+", topic)
        if urls:
            akasha = self.marduk.get_module("AKASHA")
            if akasha:
                for url in urls[:2]:
                    try:
                        page = akasha.execute("fetch_page", {"url": url, "max_chars": 3000})
                        if page and not page.startswith(("Need", "Fetch", "Firecrawl", "Could not")):
                            parts.append(f"Fetched page ({url}):\n{page}")
                    except Exception:
                        continue
        return "\n\n".join(parts) if parts else "(no local context)"

    def _save_to_vault(self, topic: str, content: str, kind: str, provider: str) -> None:
        if not self.marduk:
            return
        nabu = self.marduk.get_module("NABU")
        if not nabu:
            return
        slug = re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:60] or "untitled"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        body = (
            f"# {topic}\n\n"
            f"*Via {provider} ({self.models.get(provider, '')}) on {ts}.*\n"
            f"*Cached for offline recall by NABU.*\n\n"
            f"{content}\n"
        )
        try:
            nabu.execute(
                "write_note",
                {"path": f"{kind}/{slug}.md", "content": body, "append": False},
            )
        except Exception as e:
            self._log.warning(f"vault cache failed: {e}")
