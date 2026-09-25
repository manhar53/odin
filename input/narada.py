# NARADA — Hindu sage, divine messenger who travels between mortal and divine realms.
# Carries ODIN's voice off the laptop: text-mode gateway via Telegram. The user
# can talk to ODIN from their phone, without speaking, without being on this
# machine — same fast-route pipeline, same modules, just routed over Telegram.
#
# Architectural placement: lives in input/ alongside HEIMDALL because it is a
# perception/input bridge — not a registered MARDUK module. It owns its own
# dispatch loop and calls marduk.dispatch directly (same pattern as HEIMDALL).
#
# Key differences from HEIMDALL:
#   - No audio. No Whisper. No barge-in. No TTS.
#   - Allow-list authentication (only configured Telegram user IDs may reply).
#   - Unknown / complex queries skip GIL entirely and route to SARASWATI cloud.
#     Reason: Telegram users tolerate a 2-3 s cloud latency; they do NOT tolerate
#     a 30-60 s local 1B tool-call. The local 1B is the runtime voice — for
#     text-mode the smarter cloud is the better tradeoff.
#   - Disabled by default. Requires telegram.bot_token + allowed_user_ids in
#     config (or env vars) before the thread spawns.

import asyncio
import os
import re
import threading
from typing import Optional

from input.heimdall import (
    _FAST_ROUTES,
    _IDENTITY_QUESTION,
    _CAPABILITY_QUESTION,
    _CAPABILITIES_LINE,
    _SMALL_TALK,
    _pick_identity_response,
    _strip_self_corrections,
    _normalize_email_speech,
    _is_multi_step,
)

try:
    from telegram import Update
    from telegram.ext import (
        ApplicationBuilder, MessageHandler, CommandHandler,
        ContextTypes, filters,
    )
    _HAS_PTB = True
except ImportError:
    _HAS_PTB = False


# Fenced-code paste detector. NARADA-specific (voice never sees ``` fences).
# Captures language tag and body so we can route directly to SARASWATI's
# code-specialist skills instead of going through the general ask_ai chain.
_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+#.\-]*)\s*\n(.*?)```", re.DOTALL)


class Narada:
    """Text-mode gateway. Mirrors HEIMDALL's routing pipeline without the
    audio frontend. Speaks Telegram messages instead of speaking aloud."""

    def __init__(self, config: dict, marduk, gil, thoth, loki=None, nabu=None):
        self.config = config
        self.marduk = marduk
        self.gil = gil
        self.thoth = thoth
        self.loki = loki
        self.nabu = nabu

        cfg = config.get("narada", {})
        # Token can come from config.yaml OR the env var. Env preferred for
        # secrets. Empty → bot stays dormant; no thread spawned.
        self.token: str = (cfg.get("telegram_bot_token")
                           or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        # Allow-list of Telegram user IDs (ints). Config takes a list; env
        # var is comma-separated. Empty allow-list with a token present is
        # a configuration error — bot would echo everyone — so we refuse.
        allowed = cfg.get("allowed_user_ids", [])
        if not allowed:
            env_ids = os.environ.get("TELEGRAM_ALLOWED_IDS", "").strip()
            if env_ids:
                allowed = [s for s in env_ids.split(",") if s.strip()]
        self.allowed_ids: set[int] = set()
        for x in allowed:
            try:
                self.allowed_ids.add(int(x))
            except (TypeError, ValueError):
                continue

        self._thread: Optional[threading.Thread] = None
        self._app = None
        self._enabled = False

    def start(self) -> bool:
        """Launch the Telegram bot in a daemon thread. Returns True if started.
        Stays silent (and dormant) when no token / no allow-list / library
        missing — so adding NARADA to main.py never breaks a vanilla boot."""
        if not _HAS_PTB:
            print("[NARADA] python-telegram-bot not installed — gateway disabled.")
            return False
        if not self.token:
            print("[NARADA] No Telegram bot token configured — gateway disabled. "
                  "Set narada.telegram_bot_token in config.yaml or TELEGRAM_BOT_TOKEN env.")
            return False
        if not self.allowed_ids:
            print("[NARADA] No allowed_user_ids configured — refusing to start. "
                  "Without an allow-list the bot would respond to anyone who finds it. "
                  "Set narada.allowed_user_ids or TELEGRAM_ALLOWED_IDS env.")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name="NARADA-telegram")
        self._thread.start()
        self._enabled = True
        print(f"[NARADA] Telegram gateway online. Authorised IDs: {sorted(self.allowed_ids)}")
        return True

    # ── Telegram run loop ─────────────────────────────────────────────
    def _run(self):
        # Each thread gets its own event loop — PTB needs one and the main
        # thread's loop is owned by Tk (VESTA).
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            app = ApplicationBuilder().token(self.token).build()
            app.add_handler(CommandHandler("start", self._on_start))
            app.add_handler(CommandHandler("help", self._on_help))
            app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
            self._app = app
            # stop_signals=None is required because PTB tries to install a
            # SIGINT handler by default; signal handlers only work in the
            # main thread. close_loop=False lets us run inside this thread's
            # event loop without PTB shutting it down on exit.
            app.run_polling(
                close_loop=False,
                stop_signals=None,
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
        except Exception as e:
            print(f"[NARADA] Telegram polling loop crashed: {e}")

    # ── Handlers ──────────────────────────────────────────────────────
    async def _on_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorised(update):
            return
        await update.message.reply_text(
            "ODIN online. Send me a message — I'll route it through the same "
            "fast-routes the voice gateway uses, and fall back to cloud AI for "
            "anything that needs real thought."
        )

    async def _on_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorised(update):
            return
        await update.message.reply_text(
            "Talk to me like you would by voice. Examples:\n"
            "• weather in Bangalore\n"
            "• remind me to drink water every hour\n"
            "• explain how OAuth refresh tokens work\n"
            "• research transformer attention\n"
            "• 100 USD to INR\n"
            "Unknown queries are forwarded to cloud AI (Gemini → Groq → Claude)."
        )

    async def _on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not self._authorised(update):
            # Silent drop — don't tell strangers the bot exists.
            return
        text = (update.message.text or "").strip()
        if not text:
            return
        # Process off the event loop so a slow vault search / cloud call
        # doesn't block subsequent Telegram updates.
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(None, self._process_text, text)
        if not reply:
            reply = "I couldn't form a reply for that."
        # Telegram message hard cap is 4096 chars.
        for chunk in _split_for_telegram(reply):
            await update.message.reply_text(chunk)

    def _authorised(self, update: Update) -> bool:
        user = update.effective_user
        if not user:
            return False
        ok = user.id in self.allowed_ids
        if not ok:
            print(f"[NARADA] Blocked message from unauthorised id={user.id} "
                  f"({user.username or user.first_name!r}).")
        return ok

    # ── Core text pipeline ────────────────────────────────────────────
    def _process_text(self, command: str) -> str:
        """Mirror of HEIMDALL._handle without the audio side: pre-process →
        identity / capability / small-talk → fast-routes → SARASWATI cloud
        fallback. Local 1B is intentionally skipped — for text mode the
        smarter cloud is the better tradeoff."""
        command = _strip_self_corrections(command)
        command = _normalize_email_speech(command)
        # Memory mirror so the conversation timeline includes Telegram turns.
        try:
            self.thoth.execute("store_message", {"role": "user", "content": command})
        except Exception:
            pass
        if self.nabu:
            try:
                self.nabu.execute("mirror_message", {"role": "user", "content": command})
            except Exception:
                pass

        # Paste-code workflow: if the message contains a ``` fence, intercept
        # before fast-routes / cloud and dispatch to the code-specialist skill
        # implied by surrounding words (review / fix / explain — default explain).
        reply = self._try_paste_code(command)
        if reply is None:
            reply = self._try_fast_pipeline(command)
        if reply is None:
            reply = self._cloud_fallback(command)

        try:
            self.thoth.execute("store_message", {"role": "assistant", "content": reply})
        except Exception:
            pass
        if self.nabu:
            try:
                self.nabu.execute("mirror_message", {"role": "assistant", "content": reply})
            except Exception:
                pass
        return reply

    def _try_paste_code(self, command: str) -> Optional[str]:
        """If the message contains a ```fenced``` code block, route to the
        right SARASWATI code skill based on the words around it. The user's
        ask in their own words wins ('fix this'); 'explain' is the default."""
        m = _FENCE_RE.search(command)
        if not m:
            return None
        if not self.marduk:
            return None
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return ("Got code, but no cloud provider is configured — set a Gemini/Groq/Claude key "
                    "in saraswati.* to enable code explain / review / fix.")
        language = (m.group(1) or "").strip()
        code = (m.group(2) or "").strip()
        surrounding = (command[:m.start()] + " " + command[m.end():]).lower()
        if re.search(r"\b(fix|debug|broken|not\s+working|error)\b", surrounding):
            # Need an error description for the fix path. If none, fall back
            # to review which gives the user actionable bug-spotting anyway.
            err_match = re.search(
                r"\b(?:error|traceback|exception|output|stderr)\s*[:\-]\s*(.+?)(?:\Z|```)",
                command, re.IGNORECASE | re.DOTALL,
            )
            if err_match:
                error = err_match.group(1).strip()
                return sara.execute("fix_code", {
                    "code": code, "error": error, "language": language,
                })
            return sara.execute("review_code", {"code": code, "language": language})
        if re.search(r"\b(review|check|critique|audit)\b", surrounding):
            return sara.execute("review_code", {"code": code, "language": language})
        # Default: explain.
        return sara.execute("explain_code", {"code": code, "language": language})

    def _try_fast_pipeline(self, command: str) -> Optional[str]:
        """Run the same identity → capability → small-talk → FAST_ROUTES chain
        HEIMDALL uses. Multi-step splitting is supported too."""
        cmd = command.strip()
        if _IDENTITY_QUESTION.search(cmd):
            persona = "mythic"
            if self.loki:
                try:
                    persona = self.loki.execute("get_persona", {}) or "mythic"
                except Exception:
                    pass
            return _pick_identity_response(persona)
        if _CAPABILITY_QUESTION.search(cmd):
            return _CAPABILITIES_LINE
        for pattern, reply in _SMALL_TALK:
            if pattern.match(cmd):
                return reply
        # Multi-step: split on conjunctions, fast-route each half.
        if _is_multi_step(cmd):
            multi = self._try_multistep(cmd)
            if multi is not None:
                return multi
        return self._try_single_fast_route(cmd)

    def _try_single_fast_route(self, cmd: str) -> Optional[str]:
        for pattern, skill, args_fn in _FAST_ROUTES:
            m = pattern.search(cmd)
            if not m:
                continue
            try:
                args = args_fn(m)
                result = self.marduk.dispatch(skill, args)
                if result and not result.startswith("MARDUK"):
                    return result
            except Exception as e:
                print(f"[NARADA] Fast route error on '{skill}': {e}")
        return None

    def _try_multistep(self, command: str) -> Optional[str]:
        parts = re.split(
            r"\s+\b(?:and(?:\s+then)?|then|after\s+that)\b\s+",
            command.strip(),
            flags=re.I,
        )
        if len(parts) < 2:
            return None
        results = []
        for part in parts:
            part = part.strip().rstrip(".!?,;: ").strip()
            if not part:
                continue
            r = self._try_single_fast_route(part)
            if r is None:
                return None  # one half needed the LLM — bail and let cloud handle whole thing
            results.append(r)
        return " ".join(results) if results else None

    def _cloud_fallback(self, command: str) -> str:
        """Anything that doesn't fast-route goes to SARASWATI. We bypass the
        local 1B GIL entirely for Telegram — cloud is faster + smarter and the
        user is reading text, not waiting on TTS."""
        if not self.marduk:
            return "Cloud unavailable."
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return ("I can't reach the cloud, and the local model is reserved for the "
                    "voice gateway. Configure a Gemini or Groq key in saraswati.* to enable text mode.")
        try:
            reply = sara.execute("ask_ai", {"question": command})
        except Exception as e:
            return f"Cloud call failed: {e}"
        if not reply or reply.startswith(("No cloud", "Need a question", "[SARASWATI]")):
            return reply or "Cloud returned nothing."
        return reply


def _split_for_telegram(text: str, limit: int = 4000) -> list[str]:
    """Telegram caps a single message at 4096 chars. Split on paragraph
    boundaries where possible so a long research note doesn't get chopped
    mid-sentence."""
    if len(text) <= limit:
        return [text]
    chunks, buf = [], ""
    for para in text.split("\n\n"):
        if len(buf) + len(para) + 2 > limit and buf:
            chunks.append(buf.rstrip())
            buf = ""
        if len(para) > limit:
            # Single mega-paragraph: hard-slice on character boundary.
            for i in range(0, len(para), limit):
                chunks.append(para[i:i + limit])
            buf = ""
            continue
        buf += para + "\n\n"
    if buf.strip():
        chunks.append(buf.rstrip())
    return chunks
