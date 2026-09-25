# ARGUS — Greek hundred-eyed giant, ever-watchful guardian.
# The eye that sees the live web and the hand that acts on it. ARGUS drives
# a real Chrome browser via the Chrome DevTools Protocol (no Playwright dep)
# using the browser-use library — he can log in, fill forms, click checkout,
# scrape gated pages, anything a person can do with a browser.
#
# Why ARGUS sits in output/ alongside THOR and MERCURY:
#   The browser-use Agent is LLM-driven (cloud picks each step), but the
#   RESULT acts on the world: clicks, types, submits. Decisions live in
#   intelligence/, world-effects live in output/. ARGUS is an effector.
#
# Memory & cost shape:
#   - Chrome is launched only when browse() is called and closed when the
#     task ends. No idle process — preserves the 8 GB ceiling.
#   - Each agent step is a cloud LLM call with a screenshot. Gemini-2.0-flash
#     is the default (free tier 1500/day, vision-capable, ~2s/step). Claude
#     is fallback for tasks Gemini gets stuck on.
#
# Safety:
#   - Destructive intents (buy/send/submit/cancel/delete/book/...) require
#     explicit "confirmed" in the task — a belt-and-braces refusal so an
#     over-eager local 1B can't autopilot ODIN into spending money.
#   - Headless is OFF by default. The user sees what ARGUS is doing and can
#     close the window mid-task to abort.

import asyncio
import os
import re
import threading

from core.marduk import OdinModule

try:
    from browser_use import Agent, Browser, ChatGoogle, ChatAnthropic
    from browser_use.browser.profile import BrowserProfile
    _HAS_BROWSER_USE = True
except ImportError:
    _HAS_BROWSER_USE = False
    BrowserProfile = None  # type: ignore


# Words that imply ARGUS would change something irreversible. False positives
# are fine (the user just re-issues with "confirmed"); false negatives cost
# real money, so the regex errs on the side of catching too much.
_DESTRUCTIVE_INTENT = re.compile(
    r"\b(?:buy|purchase|pay|checkout|order(?:\s+now)?|"
    r"send(?:\s+(?:a|the))?|submit|post(?:\s+a)?|"
    r"delete|remove|cancel|unsubscribe|"
    r"transfer|withdraw|deposit|donate|"
    r"book(?:\s+(?:a|the))?|reserve(?:\s+(?:a|the))?|"
    r"sign\s+up|register(?:\s+for)?|create\s+account|"
    r"sell|trade|invest|bid)\b",
    re.I,
)

# User's explicit go-ahead. Has to be in the task itself, not implied.
_CONFIRMED = re.compile(
    r"\b(?:confirmed|i\s+confirm|go\s+ahead|do\s+it|yes\s+proceed|"
    r"i\s+approve|i'm\s+sure)\b",
    re.I,
)


class Argus(OdinModule):
    MODULE_NAME = "ARGUS"
    LAYER = "OUTPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("argus", {})
        self.enabled = _HAS_BROWSER_USE
        self.headless = bool(cfg.get("headless", False))
        self.max_steps = int(cfg.get("max_steps", 25))
        self.confirm_destructive = bool(cfg.get("confirm_destructive", True))
        self.timeout_seconds = int(cfg.get("timeout_seconds", 300))
        # Provider order for the agent LLM. Gemini first because (1) free tier,
        # (2) vision-capable, (3) ~2s/step vs ~5s for Claude. Claude fallback
        # for harder multi-step planning where Gemini gets stuck.
        self.llm_chain: list[str] = cfg.get("llm_chain", ["gemini", "claude"])
        # Chrome path — same auto-detect logic as main.py's _register_chrome_as_default.
        self.chrome_path: str = cfg.get("chrome_path", "") or self._find_chrome()
        # Persistent profile so sites stay LOGGED IN across ARGUS runs. Without
        # this, every browse() launches a fresh Chrome that's NOT signed in to
        # WhatsApp Web / Gmail / Instagram, and the agent gets stuck on QR /
        # login pages. With a persistent user_data_dir, the user signs in once
        # and ARGUS reuses the session forever after. Default lives under data/
        # so it travels with the ODIN install.
        # IMPORTANT: don't point at the user's primary Chrome profile — Chrome
        # refuses to launch with a profile that's already in use by another
        # Chrome instance.
        self.user_data_dir: str = cfg.get("user_data_dir", "data/chrome_profile")
        if self.user_data_dir:
            self.user_data_dir = os.path.abspath(self.user_data_dir)
            os.makedirs(self.user_data_dir, exist_ok=True)

        if not _HAS_BROWSER_USE:
            print("[ARGUS] browser-use not installed — browser automation disabled.")
        elif not self.chrome_path:
            print("[ARGUS] Chrome not found — install Google Chrome to enable ARGUS.")
            self.enabled = False
        else:
            print(f"[ARGUS] Online. Chrome={self.chrome_path}. "
                  f"Headless={self.headless}, max_steps={self.max_steps}, "
                  f"llm_chain={self.llm_chain}. "
                  f"Profile={self.user_data_dir or '(ephemeral)'}.")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "browse",
                "description": (
                    "Drive a real Chrome browser to complete a multi-step web task: "
                    "log in, fill forms, click buttons, navigate, scrape gated pages. "
                    "Use this when search_web is not enough because the task requires "
                    "INTERACTING with a page. Examples: 'find the latest invoice in "
                    "my Gmail', 'check my Amazon order status', 'fill the contact form "
                    "on example.com with my details'. Returns the agent's final answer. "
                    "Destructive intents (buy / send / submit / cancel) require the "
                    "word 'confirmed' in the task; otherwise ARGUS refuses."
                ),
                "parameters": {
                    "task": {"type": "string", "description": "Natural-language description of what to do in the browser."},
                    "start_url": {"type": "string", "description": "Optional starting URL. If omitted, the agent navigates from a blank tab."},
                },
                "required": ["task"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        if skill_name != "browse":
            return f"[ARGUS] Unknown skill: {skill_name}"
        try:
            return self._browse(**args)
        except Exception as e:
            return f"[ARGUS] Error: {e}"

    def _browse(self, task: str, start_url: str = "") -> str:
        if not self.enabled:
            return ("Browser automation unavailable — install browser-use and ensure "
                    "Chrome is installed.")
        task = (task or "").strip()
        if not task:
            return "Need a task description."
        if start_url:
            task = f"Start at {start_url}. Then: {task}"

        # Belt-and-braces destructive-action gate. If a local 1B speculatively
        # decides to "buy this" without user follow-up, this refusal catches it.
        if (self.confirm_destructive
                and _DESTRUCTIVE_INTENT.search(task)
                and not _CONFIRMED.search(task)):
            return ("This task looks destructive (it could buy, send, book, or "
                    "change something irreversibly). Re-issue with 'confirmed' "
                    "in the task once you're sure. Example: 'submit the form on "
                    "example.com with my details, confirmed.'")

        llm = self._build_llm()
        if llm is None:
            return ("No cloud LLM available for the browser agent. Configure "
                    "SARASWATI.gemini_api_key or anthropic_api_key.")

        # Run the async agent inside a fresh event loop on a worker thread.
        # Main thread is owned by Tk (VESTA); NARADA owns its own thread.
        # Each browse() gets a fresh loop so nothing leaks across calls.
        result_box: dict = {"value": ""}

        def _runner():
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                # BrowserProfile persists cookies + localStorage across runs.
                # Without it, every browse() opens a brand-new Chrome instance
                # with no login state — WhatsApp Web shows the QR page, Gmail
                # shows the sign-in page, Instagram shows the login wall. With
                # a persistent user_data_dir, the user signs in ONCE and ARGUS
                # picks up where they left off thereafter.
                profile = (BrowserProfile(
                    executable_path=self.chrome_path,
                    headless=self.headless,
                    user_data_dir=self.user_data_dir,
                ) if (BrowserProfile and self.user_data_dir) else None)
                if profile is not None:
                    browser = Browser(is_local=True, browser_profile=profile)
                else:
                    browser = Browser(
                        is_local=True,
                        executable_path=self.chrome_path,
                        headless=self.headless,
                    )
                agent = Agent(task=task, llm=llm, browser=browser)
                history = asyncio.get_event_loop().run_until_complete(
                    agent.run(max_steps=self.max_steps)
                )
                # Try several signals to give the user a useful answer.
                # browser-use history exposes: final_result(), is_successful(),
                # is_done(), errors(), number_of_steps(), action_results().
                final = None
                steps = 0
                ok = None
                last_action_summary = ""
                if history is not None:
                    for getter, slot in (("final_result", "final"),
                                          ("number_of_steps", "steps"),
                                          ("is_successful", "ok")):
                        try:
                            v = getattr(history, getter)()
                            if slot == "final":
                                final = v
                            elif slot == "steps":
                                steps = v
                            elif slot == "ok":
                                ok = v
                        except Exception:
                            pass
                    # Last-action description as fallback (when no final text)
                    try:
                        ars = history.action_results()
                        if ars:
                            last = ars[-1]
                            extracted = getattr(last, "extracted_content", None) or ""
                            last_action_summary = str(extracted)[:200]
                    except Exception:
                        pass
                if final:
                    result_box["value"] = str(final)
                elif ok is True:
                    result_box["value"] = (
                        f"Browser task completed successfully ({steps} step(s)). "
                        f"{('Last action: ' + last_action_summary) if last_action_summary else 'No text was extracted from the page — check the browser window to confirm.'}"
                    )
                elif ok is False:
                    result_box["value"] = (
                        f"Browser task ended without success ({steps} step(s)). "
                        f"{('Last action: ' + last_action_summary) if last_action_summary else 'The agent gave up; check the browser window for what happened.'}"
                    )
                else:
                    result_box["value"] = (
                        f"Browser task ran for {steps} step(s) but ODIN can't tell if it succeeded. "
                        f"{('Last action: ' + last_action_summary) if last_action_summary else 'Check the browser window manually.'}"
                    )
            except Exception as e:
                result_box["value"] = f"[ARGUS] Browse failed: {e}"

        worker = threading.Thread(target=_runner, daemon=True, name="ARGUS-browse")
        worker.start()
        worker.join(timeout=self.timeout_seconds)
        if worker.is_alive():
            return (f"[ARGUS] Task exceeded {self.timeout_seconds}s — the browser "
                    "may still be running; close it manually if needed.")
        return result_box["value"]

    # ── LLM construction (reuses SARASWATI keys via MARDUK) ────────────
    def _build_llm(self):
        """Pick the first LLM in our preference chain whose key is configured.
        We piggyback on SARASWATI's config so the user doesn't set keys twice."""
        if not self.marduk:
            return None
        for provider in self.llm_chain:
            key = self._extract_key(provider)
            if not key:
                continue
            sara = self.marduk.get_module("SARASWATI")
            model = (sara.models.get(provider) if sara and hasattr(sara, "models") else None)
            try:
                if provider == "gemini":
                    return ChatGoogle(model=model or "gemini-2.0-flash", api_key=key)
                if provider == "claude":
                    return ChatAnthropic(model=model or "claude-sonnet-4-6", api_key=key)
            except Exception as e:
                print(f"[ARGUS] {provider} LLM init failed: {e}")
                continue
        return None

    def _extract_key(self, provider: str) -> str:
        env_map = {"gemini": "GEMINI_API_KEY", "claude": "ANTHROPIC_API_KEY"}
        cfg_map = {"gemini": "gemini_api_key", "claude": "anthropic_api_key"}
        sara_cfg = self.config.get("saraswati", {})
        return (sara_cfg.get(cfg_map[provider], "")
                or os.environ.get(env_map[provider], "")).strip()

    @staticmethod
    def _find_chrome() -> str:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return ""
