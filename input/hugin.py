# HUGIN — Norse, one of Odin's two ravens (with MUNIN). Each dawn the
# ravens fly across the worlds gathering news; each evening they return to
# Odin's shoulders to whisper what they've seen. HUGIN is "thought" — the
# inbound messenger of intelligence from afar.
#
# In ODIN, HUGIN is the universal inbound HTTP webhook. Any external
# service (Zapier, n8n, IFTTT, GitHub Actions, Slack/Discord bots, custom
# scripts, curl from your phone) can POST a JSON message and HUGIN feeds it
# through the same fast-route pipeline NARADA uses. This is the "23+
# channels" answer in ODIN's own way — one ingress, infinite bridges.
#
# Security model — local-only by default:
#   - Binds 127.0.0.1 by default. Loud warning if user sets bind_host=0.0.0.0.
#   - Bearer-token auth (configurable). Token in `Authorization: Bearer <X>`.
#   - Token comparison via secrets.compare_digest (constant-time).
#   - Allow-list of source-IP CIDRs (optional).
#   - Disabled by default — must set narada-style token OR allow_open_local.
#
# Protocol:
#   POST /message
#     Headers: Authorization: Bearer <token>, Content-Type: application/json
#     Body:    {"text": "<command>", "reply_to": "<callback_url>"}
#     Returns: {"status": "ok|denied|error", "reply": "<text>", ...}
#
# Reuses heimdall module-level helpers (FAST_ROUTES, normalize_email,
# normalize_url, IDENTITY/CAPABILITY/SMALL_TALK) so HUGIN inherits every
# improvement to voice routing automatically.

import json
import os
import secrets
import threading
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    _normalize_url_speech,
    _is_multi_step,
)


class Hugin:
    """Universal inbound webhook gateway. Plain class (not OdinModule) —
    same pattern as HEIMDALL and NARADA: owns its own dispatch, doesn't
    register skills with MARDUK."""

    def __init__(self, config: dict, marduk, gil, thoth, loki=None, nabu=None):
        self.config = config
        self.marduk = marduk
        self.gil = gil
        self.thoth = thoth
        self.loki = loki
        self.nabu = nabu

        cfg = config.get("hugin", {})
        self.bind_host: str = cfg.get("bind_host", "127.0.0.1")
        self.port: int = int(cfg.get("port", 8765))
        # Token: config wins, env fallback. Empty disables HUGIN entirely
        # unless allow_open_local is True (still 127.0.0.1-bound).
        self.token: str = (cfg.get("auth_token")
                           or os.environ.get("HUGIN_AUTH_TOKEN", "")).strip()
        self.allow_open_local: bool = bool(cfg.get("allow_open_local", False))
        self.log_path: str = cfg.get("log_path", "data/logs/hugin.log")
        # Reply mode controls. When 'reply_to' is supplied in a request,
        # HUGIN POSTs the response back. Set max_reply_chars to bound it.
        self.max_reply_chars: int = int(cfg.get("max_reply_chars", 4000))
        self.callback_timeout: int = int(cfg.get("callback_timeout", 5))

        self._thread: Optional[threading.Thread] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._enabled = False

    def start(self) -> bool:
        if not self.token and not self.allow_open_local:
            print("[HUGIN] No auth_token configured and allow_open_local=False → "
                  "webhook disabled. Set hugin.auth_token or HUGIN_AUTH_TOKEN env, "
                  "or set hugin.allow_open_local: true for localhost-only no-auth mode.")
            return False
        if self.bind_host not in ("127.0.0.1", "localhost", "::1") and not self.token:
            # Open binding without a token is reckless. Refuse.
            print(f"[HUGIN] REFUSING TO START: bind_host={self.bind_host!r} is "
                  f"non-localhost but no auth_token is set. Set a token first.")
            return False

        handler = _make_handler(self)
        try:
            self._server = ThreadingHTTPServer((self.bind_host, self.port), handler)
        except OSError as e:
            print(f"[HUGIN] Could not bind {self.bind_host}:{self.port}: {e}. "
                  f"Pick a different port in hugin.port.")
            return False
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name="HUGIN-webhook")
        self._thread.start()
        self._enabled = True
        auth_mode = "token" if self.token else "OPEN (localhost)"
        print(f"[HUGIN] Webhook ingress online — http://{self.bind_host}:{self.port}/message "
              f"({auth_mode}).")
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._enabled = False

    # ── Auth ────────────────────────────────────────────────────────
    def is_authorised(self, header_value: str) -> bool:
        if not self.token:
            # Open mode only honoured for localhost binding.
            return self.bind_host in ("127.0.0.1", "localhost", "::1")
        if not header_value:
            return False
        if not header_value.lower().startswith("bearer "):
            return False
        supplied = header_value[7:].strip()
        return secrets.compare_digest(supplied, self.token)

    # ── Pipeline ────────────────────────────────────────────────────
    def process_text(self, text: str) -> str:
        """Run the same fast-route ladder NARADA uses, then fall back to
        cloud. We deliberately do NOT touch local 1B for webhook traffic —
        anything that doesn't hit a fast-route is a real question and the
        cloud answers faster + smarter than the local 1B."""
        text = (text or "").strip()
        if not text:
            return "Empty message."
        text = _strip_self_corrections(text)
        text = _normalize_email_speech(text)
        text = _normalize_url_speech(text)

        # Mirror to THOTH/NABU like NARADA does so the conversation timeline
        # includes webhook turns.
        try:
            self.thoth.execute("store_message", {"role": "user", "content": f"[webhook] {text}"})
        except Exception:
            pass
        if self.nabu:
            try:
                self.nabu.execute("mirror_message", {"role": "user", "content": f"[webhook] {text}"})
            except Exception:
                pass

        reply = self._try_fast_pipeline(text)
        if reply is None:
            reply = self._cloud_fallback(text)

        try:
            self.thoth.execute("store_message", {"role": "assistant", "content": reply})
        except Exception:
            pass
        return reply

    def _try_fast_pipeline(self, cmd: str) -> Optional[str]:
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
        if _is_multi_step(cmd):
            multi = self._try_multistep(cmd)
            if multi is not None:
                return multi
        return self._try_single(cmd)

    def _try_single(self, cmd: str) -> Optional[str]:
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
                print(f"[HUGIN] Fast route error on '{skill}': {e}")
        return None

    def _try_multistep(self, command: str) -> Optional[str]:
        import re as _re
        parts = _re.split(
            r"\s+\b(?:and(?:\s+then)?|then|after\s+that)\b\s+",
            command.strip(), flags=_re.I,
        )
        if len(parts) < 2:
            return None
        out = []
        for part in parts:
            part = part.strip().rstrip(".!?,;: ").strip()
            if not part:
                continue
            r = self._try_single(part)
            if r is None:
                return None
            out.append(r)
        return " ".join(out) if out else None

    def _cloud_fallback(self, text: str) -> str:
        if not self.marduk:
            return "Cloud unavailable."
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return ("No fast-route matched and no cloud LLM configured. "
                    "Set a Gemini/Groq/Claude key in saraswati.* to enable text mode.")
        try:
            return sara.execute("ask_ai", {"question": text})
        except Exception as e:
            return f"Cloud call failed: {e}"

    def post_callback(self, url: str, payload: dict) -> None:
        """Fire-and-forget POST back to the caller-supplied callback URL.
        Errors are logged but never raised — webhook responses are best-effort."""
        if not url:
            return
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=self.callback_timeout).read()
        except Exception as e:
            self._log_line(f"callback to {url} failed: {e}")

    # ── Logging ─────────────────────────────────────────────────────
    def _log_line(self, line: str) -> None:
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat()} {line}\n")
        except OSError:
            pass


def _make_handler(hugin_ref: "Hugin"):
    """Closure-bind HUGIN to the request handler class."""

    class _Handler(BaseHTTPRequestHandler):
        # Quiet by default — own log file via Hugin._log_line.
        def log_message(self, fmt, *args):
            hugin_ref._log_line(f"{self.address_string()} {fmt % args}")

        def _reply_json(self, status_code: int, obj: dict):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # /health for liveness probes.
            if self.path == "/health":
                self._reply_json(200, {"status": "ok", "service": "HUGIN"})
                return
            self._reply_json(404, {"error": "POST /message only"})

        def do_POST(self):
            if self.path != "/message":
                self._reply_json(404, {"error": "unknown path"})
                return
            if not hugin_ref.is_authorised(self.headers.get("Authorization", "")):
                self._reply_json(401, {"error": "unauthorised"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 100_000:
                self._reply_json(400, {"error": "missing or oversized body"})
                return
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception:
                self._reply_json(400, {"error": "invalid JSON"})
                return
            text = (data.get("text") or "").strip()
            reply_to = (data.get("reply_to") or "").strip()
            if not text:
                self._reply_json(400, {"error": "missing 'text'"})
                return
            try:
                reply = hugin_ref.process_text(text)
            except Exception as e:
                self._reply_json(500, {"error": f"processing failed: {e}"})
                return
            reply_trimmed = (reply or "")[:hugin_ref.max_reply_chars]
            payload = {"status": "ok", "reply": reply_trimmed}
            self._reply_json(200, payload)
            if reply_to:
                threading.Thread(
                    target=hugin_ref.post_callback,
                    args=(reply_to, payload),
                    daemon=True, name="HUGIN-callback",
                ).start()

    return _Handler
