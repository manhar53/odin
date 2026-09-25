# HERMOD — Norse — Odin's messenger, son of Odin
# The corner-avatar UI: a small frameless pywebview window pinned bottom-
# right of the screen. Visible whenever a regular app is in focus (the
# throne room is auto-hidden over apps), so ODIN stays one click/glance
# away without painting over the user's work.
#
# Interactions:
#   click avatar         → toggle the mini chat panel
#   click-and-drag       → reposition the bubble (saved to disk)
#   type + Enter         → routes through the same fast-route + cloud chain
#                          NARADA uses, so it works without spoken voice
#   Esc / × button       → collapse the panel
#   ⛯ button             → poke ASGARD to show the throne room
#
# IRIS speech also surfaces here when the throne room is hidden — so when
# the user is in Chrome and ODIN replies, the answer slides into the mini
# panel instead of nowhere.

import json
import os
import threading
from pathlib import PurePath

from core.marduk import OdinModule

try:
    import webview
    _HAS_WEBVIEW = True
except ImportError:
    _HAS_WEBVIEW = False

try:
    import ctypes
    _HAS_CTYPES = True
except ImportError:
    _HAS_CTYPES = False


_UI_DIR     = os.path.join(os.path.dirname(__file__), "hermod_ui")
_INDEX_PATH = os.path.join(_UI_DIR, "index.html")
_WINDOW_TITLE = "ODIN — Hermod"

# Default avatar window size. Picks up enough horizontal room for the panel
# (320 px) + avatar (~88 px including drop-shadow halo) + gap + safe margin.
_WINDOW_WIDTH  = 460
_WINDOW_HEIGHT = 420


def _ui_file_url() -> str:
    return PurePath(_INDEX_PATH).as_uri()


def _screen_size():
    """(screen_w, screen_h) of the primary display."""
    if os.name != "nt" or not _HAS_CTYPES:
        return (1920, 1080)
    u = ctypes.windll.user32
    return (u.GetSystemMetrics(0), u.GetSystemMetrics(1))


def _default_position():
    """Bottom-right of the primary monitor, with a 12 px breathing margin from
    each edge so the avatar doesn't slam into the taskbar."""
    sw, sh = _screen_size()
    margin = 12
    x = sw - _WINDOW_WIDTH  - margin
    y = sh - _WINDOW_HEIGHT - margin - 48   # extra 48 px above the taskbar
    return (x, y)


# ── JS-side bridge ──────────────────────────────────────────────────
class _HermodApi:
    """Exposed to JS as window.pywebview.api.*"""
    def __init__(self, hermod):
        self._h = hermod

    def submit_text(self, text):
        try:
            self._h.handle_text(text)
        except Exception as e:
            print(f"[HERMOD] submit_text error: {e}")
        return "ok"

    def toggle_throne(self):
        try:
            self._h._on_throne_button()
        except Exception as e:
            print(f"[HERMOD] toggle_throne error: {e}")
        return "ok"

    def upload(self):
        """User tapped + — open a native file picker and feed ODIN whatever
        they choose. Runs on a worker so the JS call returns immediately."""
        try:
            threading.Thread(target=self._h.upload_files, daemon=True).start()
        except Exception as e:
            print(f"[HERMOD] upload error: {e}")
        return "ok"

    def start_drag(self, sx, sy):
        try:
            self._h._on_drag_start(int(sx), int(sy))
        except Exception:
            pass
        return "ok"

    def drag_to(self, sx, sy):
        try:
            self._h._on_drag_move(int(sx), int(sy))
        except Exception:
            pass
        return "ok"

    def end_drag(self):
        try:
            self._h._on_drag_end()
        except Exception:
            pass
        return "ok"


class Hermod(OdinModule):
    MODULE_NAME = "HERMOD"
    LAYER = "OUTPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("hermod", {})
        self._enabled         = cfg.get("enabled", True)
        self._show_when_app   = cfg.get("show_when_app", True)
        self._position_path   = cfg.get("position_path", "data/knowledge/hermod_position.json")
        self._visible         = False    # window starts hidden; foreground watcher decides

        self._window  = None
        self._asgard  = None
        self._iris    = None
        self._gil     = None
        self._loki    = None
        self._js_api  = _HermodApi(self)

        # Drag bookkeeping. _drag_anchor is the screen coord at mousedown;
        # _window_origin is the window's top-left at the same moment. While
        # dragging, the JS sends fresh screen coords and we move the window
        # by (current - anchor) added to the original origin.
        self._drag_anchor = None
        self._window_origin = None

    # ─── MARDUK skill surface ──────────────────────────────────────
    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "show_corner_avatar",
                "description": "Reveal the corner avatar bubble.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
            {
                "name": "hide_corner_avatar",
                "description": "Hide the corner avatar bubble.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
            {
                "name": "expand_corner_avatar",
                "description": "Open the corner avatar's chat panel.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        try:
            if skill_name == "show_corner_avatar":
                self.show(); return "Corner avatar visible."
            if skill_name == "hide_corner_avatar":
                self.hide(); return "Corner avatar hidden."
            if skill_name == "expand_corner_avatar":
                self.expand(); return "Corner avatar expanded."
        except Exception as e:
            return f"[HERMOD] Error: {e}"
        return f"[HERMOD] Unknown skill: {skill_name}"

    # ─── External wiring ──────────────────────────────────────────
    def wire(self, *, asgard=None, iris=None, gil=None, loki=None, thoth=None):
        """main.py calls this once HEIMDALL + ASGARD + GIL are constructed.
        Keeps coupling explicit instead of fishing modules out of MARDUK every call."""
        self._asgard = asgard
        self._iris   = iris
        self._gil    = gil
        self._loki   = loki
        self._thoth  = thoth
        if iris and hasattr(iris, "add_speak_listener"):
            iris.add_speak_listener(self._on_iris_speak)

    def wire_heimdall(self, heimdall):
        if hasattr(heimdall, "add_listening_listener"):
            heimdall.add_listening_listener(self._on_heimdall_listening)

    # ─── Pywebview window setup ───────────────────────────────────
    def create_window(self):
        """Build the pywebview window object. MUST be called BEFORE
        webview.start() — ASGARD's run() loop is what kicks the event loop,
        so HERMOD just registers its window into the same loop."""
        if not self._enabled or not _HAS_WEBVIEW:
            return None
        if not os.path.exists(_INDEX_PATH):
            print(f"[HERMOD] UI bundle missing at {_INDEX_PATH} — corner avatar disabled.")
            return None
        x, y = self._load_position()
        self._window = webview.create_window(
            _WINDOW_TITLE,
            url=_ui_file_url(),
            x=x, y=y,
            width=_WINDOW_WIDTH, height=_WINDOW_HEIGHT,
            frameless=True,
            transparent=True,         # pywebview honors this on Windows via WS_EX_LAYERED
            on_top=True,
            easy_drag=False,
            background_color="#000000",   # pywebview takes 6-digit hex; alpha comes from transparent=True
            js_api=self._js_api,
            hidden=True,                  # foreground watcher will show when appropriate
            resizable=False,
        )
        return self._window

    # ─── Visibility ───────────────────────────────────────────────
    def show(self):
        if not self._window:
            return
        try:
            self._window.show()
            self._visible = True
        except Exception as e:
            print(f"[HERMOD] show failed: {e}")

    def hide(self):
        if not self._window:
            return
        try:
            self._window.hide()
            self._visible = False
        except Exception as e:
            print(f"[HERMOD] hide failed: {e}")

    def expand(self):
        self._eval("hermod.expand()")

    def collapse(self):
        self._eval("hermod.collapse()")

    # ─── Foreground-state callback (subscribed in main.py) ────────
    def on_foreground_state(self, state: str):
        """state ∈ {'desktop', 'app', 'fullscreen'}. ASGARD owns 'desktop';
        HERMOD owns 'app'; both hide under 'fullscreen' so games / videos /
        Zoom shares are uncluttered. Idempotent — set_visibility from any
        thread is fine because pywebview.show/hide is thread-safe enough."""
        if not self._enabled:
            return
        if state == "app" and self._show_when_app:
            if not self._visible:
                self.show()
        else:
            # desktop or fullscreen → hide
            if self._visible:
                self.hide()

    # ─── IRIS + HEIMDALL event listeners ──────────────────────────
    def _on_iris_speak(self, text: str):
        if not text:
            return
        # Always feed the text in. The mini panel auto-expands when chunks
        # arrive (CSS handles it). If we're hidden, the user won't see it —
        # that's fine, ASGARD will. We don't unhide HERMOD just to speak.
        self._eval(f"hermod.speakChunk({json.dumps(text)})")
        self._eval("hermod.setState('speaking')")

    def _on_heimdall_listening(self, is_listening: bool):
        if is_listening:
            self._eval("hermod.clearReply(); hermod.setState('listening')")
        else:
            self._eval("hermod.setState('thinking')")

    # ─── Throne toggle button on the panel ────────────────────────
    def _on_throne_button(self):
        if not self._asgard:
            return
        # Bring the throne room forward, opaque, even if we're in app foreground.
        try:
            self._asgard.show()
            self._asgard.set_phantom(False)
        except Exception as e:
            print(f"[HERMOD] throne open failed: {e}")

    # ─── File upload (the + button) ───────────────────────────────
    # Reuses ASGARD's drop-routing so there's a single source of truth for
    # "what does ODIN do when fed a file". We just pick the file(s) and the
    # routing layer; ASGARD speaks the result, which lands back in our thread
    # via the IRIS speak listener.
    _CODE_EXTS = {
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".cc",
        ".h", ".cs", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".sql",
        ".html", ".css", ".yaml", ".yml", ".json", ".toml",
    }
    _SHEET_EXTS = {".xlsx", ".xlsm", ".xltx", ".xls"}

    def upload_files(self):
        if not self._window:
            return
        try:
            paths = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=True
            )
        except Exception as e:
            print(f"[HERMOD] file dialog failed: {e}")
            return
        if not paths:
            return
        for path in paths:
            name = os.path.basename(path)
            ext = os.path.splitext(name)[1].lower()
            if ext in self._CODE_EXTS:
                layer = "INTELLIGENCE"        # → SARASWATI.explain_code
            elif ext in self._SHEET_EXTS:
                layer = "UTILITY"             # → GANESH.excel_summarize
            else:
                layer = "MEMORY"              # → archived to the Brain vault
            # Show the attachment in the chat thread.
            self._eval(f"hermod.addUserMessage({json.dumps('📎 ' + name)}, true)")
            payload = json.dumps({"layer": layer,
                                  "files": [{"name": name, "path": path}],
                                  "url": "", "text": ""})
            if self._asgard and hasattr(self._asgard, "handle_drop_json"):
                try:
                    self._asgard.handle_drop_json(payload)
                except Exception as e:
                    self._speak_local(f"I couldn't take in {name}: {e}")
            else:
                self._speak_local("My roots aren't reachable right now, so I can't file that.")

    def _speak_local(self, text: str):
        """Speak via IRIS if available (so it threads into the chat), else
        push straight into the panel."""
        if self._iris:
            self._iris.speak(text)
        else:
            self._eval(f"hermod.speakChunk({json.dumps(text)}); hermod.setState('idle')")

    # ─── Text submit: same path NARADA uses ───────────────────────
    def handle_text(self, text: str):
        """Run a typed line through the fast-route → cloud chain. Mirrors
        NARADA's text-mode logic so behavior is consistent across input modes."""
        text = (text or "").strip()
        if not text:
            return
        threading.Thread(target=self._route_text_async, args=(text,), daemon=True).start()

    def _route_text_async(self, text: str):
        reply = None
        try:
            # 1) Fast routes (the same _FAST_ROUTES list HEIMDALL uses).
            from input.heimdall import _FAST_ROUTES
            for pattern, skill, args_fn in _FAST_ROUTES:
                m = pattern.search(text)
                if not m:
                    continue
                try:
                    args = args_fn(m)
                    result = self.marduk.dispatch(skill, args)
                    if result and not str(result).startswith("MARDUK"):
                        reply = str(result)
                        break
                except Exception as e:
                    print(f"[HERMOD] fast-route error on '{skill}': {e}")

            # 2) Cloud fallback (SARASWATI) for unmatched queries.
            if reply is None and self.marduk:
                sara = self.marduk.get_module("SARASWATI")
                if sara and getattr(sara, "providers", None):
                    try:
                        reply = sara.execute("ask_ai", {"question": text})
                    except Exception as e:
                        reply = f"Cloud lookup failed: {e}"
                else:
                    reply = "I can't reach the cloud and local thinking is reserved for the voice loop."

            # 3) Speak it through IRIS — that fans out to both ASGARD (if
            #    throne visible) and HERMOD (us) via the speak listener.
            if reply and self._iris:
                self._iris.speak(reply)
            elif reply:
                self._eval(f"hermod.speakChunk({json.dumps(reply)}); hermod.setState('idle')")
        except Exception as e:
            err = f"Failed to handle text: {e}"
            self._eval(f"hermod.speakChunk({json.dumps(err)}); hermod.setState('idle')")

    # ─── Drag-to-reposition + persistence ─────────────────────────
    def _current_window_origin(self):
        """Best-effort read of the window's top-left in screen coords. pywebview
        doesn't expose x/y after creation, so we hit the underlying HWND."""
        if not _HAS_CTYPES:
            return self._load_position()
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, _WINDOW_TITLE)
            if not hwnd:
                return self._load_position()
            r = (ctypes.c_long * 4)()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            return (int(r[0]), int(r[1]))
        except Exception:
            return self._load_position()

    def _on_drag_start(self, sx, sy):
        self._drag_anchor   = (sx, sy)
        self._window_origin = self._current_window_origin()

    def _on_drag_move(self, sx, sy):
        if self._drag_anchor is None or self._window_origin is None or not self._window:
            return
        ax, ay = self._drag_anchor
        ox, oy = self._window_origin
        new_x = ox + (sx - ax)
        new_y = oy + (sy - ay)
        try:
            self._window.move(new_x, new_y)
        except Exception:
            pass

    def _on_drag_end(self):
        # Persist the final position so it sticks across sessions.
        try:
            x, y = self._current_window_origin()
            self._save_position(x, y)
        except Exception as e:
            print(f"[HERMOD] save position failed: {e}")
        self._drag_anchor = None
        self._window_origin = None

    def _load_position(self):
        try:
            with open(self._position_path, "r", encoding="utf-8") as f:
                d = json.load(f)
                return (int(d.get("x", -1)), int(d.get("y", -1)))
        except Exception:
            pass
        return _default_position()

    def _save_position(self, x, y):
        os.makedirs(os.path.dirname(self._position_path), exist_ok=True)
        with open(self._position_path, "w", encoding="utf-8") as f:
            json.dump({"x": int(x), "y": int(y)}, f)

    # ─── JS bridge ────────────────────────────────────────────────
    def _eval(self, js: str):
        if not self._window:
            return
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass
