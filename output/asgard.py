# ASGARD — Norse — realm of the gods
# The World Tree UI: a borderless full-screen pywebview window rendering
# Yggdrasil (painted art + living overlay). Two interaction modes:
#
#   ACTIVE  — opaque, captures mouse/keyboard. You see ODIN; he gets clicks.
#   PHANTOM — semi-transparent + click-through. Window stays painted on top
#             of the desktop, but every click passes through to whatever app
#             is underneath (Chrome, VS Code, Explorer). The "always on
#             desktop, get out of the way when I work" mode.
#
# A single left-click on the throne room flips ACTIVE → PHANTOM (since once
# you're done admiring ODIN you'll want to keep working). To bring him back
# to ACTIVE, use the hotkey or the tray menu — clicks in PHANTOM mode go
# straight to the desktop, so they can't be used to summon ODIN back.
#
# Hotkeys (global, registered with `keyboard` lib):
#   Ctrl + Alt + O  — toggle ACTIVE  / PHANTOM
#   Ctrl + Alt + H  — toggle SHOW    / HIDE (fully removes the window)
#
# Tech: pywebview on Edge WebView2 + ctypes for Win32 layered-window flags.
# pywin32 already a project dep. Boots in <1 s on Windows 10/11.

import ctypes
import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path, PurePath

from core.marduk import OdinModule

try:
    import webview
    _HAS_WEBVIEW = True
except ImportError:
    _HAS_WEBVIEW = False

try:
    import keyboard
    _HAS_KEYBOARD = True
except ImportError:
    _HAS_KEYBOARD = False


# ── HTML bundle path ────────────────────────────────────────────────
_UI_DIR = os.path.join(os.path.dirname(__file__), "asgard_ui")
_INDEX_PATH = os.path.join(_UI_DIR, "index.html")


def _ui_file_url() -> str:
    return PurePath(_INDEX_PATH).as_uri()


# ── Win32 constants for click-through layered windows ───────────────
_GWL_EXSTYLE        = -20
_WS_EX_LAYERED      = 0x00080000
_WS_EX_TRANSPARENT  = 0x00000020
_WS_EX_TOOLWINDOW   = 0x00000080
_LWA_ALPHA          = 0x00000002

_WINDOW_TITLE = "ODIN — Asgard"


def _find_hwnd(title: str = _WINDOW_TITLE):
    """Return the HWND of our throne-room window, or 0 if not found. Done
    via FindWindowW since pywebview doesn't expose the HWND directly."""
    if os.name != "nt":
        return 0
    try:
        return ctypes.windll.user32.FindWindowW(None, title)
    except Exception:
        return 0


def _set_window_mode(hwnd, *, click_through: bool, alpha: int):
    """Flip the window between captures-input and pass-through. alpha is
    0..255 (255 = fully opaque). Always sets WS_EX_LAYERED so opacity can
    be applied via SetLayeredWindowAttributes."""
    if not hwnd:
        return
    u = ctypes.windll.user32
    ex_style = u.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    new_style = ex_style | _WS_EX_LAYERED
    if click_through:
        new_style |= _WS_EX_TRANSPARENT
    else:
        new_style &= ~_WS_EX_TRANSPARENT
    u.SetWindowLongW(hwnd, _GWL_EXSTYLE, new_style)
    u.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, alpha)), _LWA_ALPHA)


# ── JS-side bridge ──────────────────────────────────────────────────
class _AsgardApi:
    """pywebview exposes any method on this object to JS as
    `window.pywebview.api.<name>()`. Single click in the HTML scene calls
    on_click, which fades ASGARD into PHANTOM so the user can work."""
    def __init__(self, asgard):
        self._asgard = asgard

    def on_click(self):
        # Overlay mode: a click on empty sky fades ASGARD into PHANTOM so
        # the user can work. Windowed mode: clicks are just clicks — a
        # normal app never dismisses itself.
        try:
            if not self._asgard._windowed:
                self._asgard.set_phantom(True)
        except Exception as e:
            print(f"[ASGARD] on_click failed: {e}")
        return "ok"

    def submit_text(self, text):
        """Command bar line — runs the same fast-route → cloud pipeline as
        HERMOD's typed input, so behavior matches all other text entries."""
        try:
            self._asgard.submit_command(text)
        except Exception as e:
            print(f"[ASGARD] submit_text failed: {e}")
        return "ok"

    def on_drop(self, payload_json):
        """Files/links/text dropped on a planet. Returns a short ack string
        the UI shows as a toast; the real work happens async and speaks."""
        try:
            return self._asgard.handle_drop_json(payload_json)
        except Exception as e:
            print(f"[ASGARD] on_drop failed: {e}")
            return "Drop failed — see ODIN log."

    def get_module_info(self, module_name):
        """Skill list for the dossier panel — straight from the live
        MARDUK registry so it never goes stale."""
        try:
            return self._asgard.get_module_info_json(module_name)
        except Exception as e:
            print(f"[ASGARD] get_module_info failed: {e}")
            return "{}"


class Asgard(OdinModule):
    MODULE_NAME = "ASGARD"
    LAYER = "OUTPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("asgard", {})
        self._enabled       = cfg.get("enabled", True)
        # Window mode: "windowed" = normal app (title bar, taskbar, alt-tab,
        # no on-top, no click-through, no auto-hide). "overlay" = the legacy
        # living-wallpaper. Voice is unaffected either way — HEIMDALL listens
        # regardless of which window has focus.
        self._windowed = str(cfg.get("window_mode", "windowed")).lower() != "overlay"
        self._win_w = int(cfg.get("window_width", 1200))
        self._win_h = int(cfg.get("window_height", 780))
        self._fullscreen    = cfg.get("fullscreen", True)
        self._show_on_speak = cfg.get("show_on_speak", True)
        # Auto-snap from PHANTOM back to ACTIVE when ODIN has something to say,
        # so the user actually sees the answer. They can click again to dismiss.
        self._snap_active_on_speak = cfg.get("snap_active_on_speak", True)
        self._hotkey_toggle = cfg.get("hotkey_toggle", "ctrl+alt+o")
        self._hotkey_hide   = cfg.get("hotkey_hide",   "ctrl+alt+h")
        self._phantom_alpha = int(cfg.get("phantom_alpha", 90))      # 0..255
        self._active_alpha  = int(cfg.get("active_alpha",  255))
        self._idle_state_delay_sec = float(cfg.get("idle_state_delay_sec", 0.4))

        self._window = None
        self._iris = None
        self._heimdall_ref = None
        self._hermod = None     # set by main.py via set_companion()
        self._iris_poller_stop = threading.Event()
        self._foreground_stop = None
        # Foreground-aware: when True, ASGARD hides itself off the desktop
        # and HERMOD (corner avatar) shows instead. The user can override
        # with hotkey or tray "Throne: Active" to force-show.
        self._foreground_aware = bool(cfg.get("foreground_aware", True)) and not self._windowed
        self._user_forced_show = False   # set by tray/hotkey "Active" to override auto-hide

        self._visible = True
        self._phantom = False
        self._hwnd = 0
        self._js_api = _AsgardApi(self)
        # Drop-routing destinations. Vault root mirrors NABU's config so
        # planet-drops land where Obsidian (and HERMES indexing) can see them.
        self._vault_root = os.path.expanduser(config.get("nabu", {}).get("vault_root", "~/Brain"))
        self._backup_root = config.get("osiris", {}).get("backup_path", "data/backups")

    # ─── MARDUK skill surface ──────────────────────────────────────
    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "show_world_tree",
                "description": "Reveal the World Tree window.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
            {
                "name": "hide_world_tree",
                "description": "Hide the World Tree window. ODIN keeps listening; UI just goes away.",
                "parameters": {},
                "required": [],
                "internal_only": True,
            },
            {
                "name": "set_tree_mode",
                "description": "Set the World Tree interaction mode: 'active' (opaque, clickable) or 'phantom' (translucent, click-through).",
                "parameters": {
                    "mode": {"type": "string", "description": "'active' or 'phantom'"}
                },
                "required": ["mode"],
                "internal_only": True,
            },
            {
                "name": "show_in_world_tree",
                "description": "Display arbitrary text in the World Tree answer panel without speaking.",
                "parameters": {
                    "text": {"type": "string", "description": "Text to display."}
                },
                "required": ["text"],
                "internal_only": True,
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        try:
            if skill_name == "show_world_tree":
                self.show()
                return "The World Tree stands."
            if skill_name == "hide_world_tree":
                self.hide()
                return "The World Tree fades."
            if skill_name == "set_tree_mode":
                mode = (args.get("mode") or "").strip().lower()
                if mode not in ("active", "phantom"):
                    return "Mode must be 'active' or 'phantom'."
                self.set_phantom(mode == "phantom")
                return f"World Tree mode set to {mode}."
            if skill_name == "show_in_world_tree":
                self.show_text(args.get("text", ""))
                return "Displayed on the World Tree."
        except Exception as e:
            return f"[ASGARD] Error: {e}"
        return f"[ASGARD] Unknown skill: {skill_name}"

    # ─── External wiring ──────────────────────────────────────────
    def wire_iris(self, iris):
        """IRIS.speak fires every sanitized chunk into us so the UI
        animates speaking + surfaces text in the blue answer panel.
        Also subscribes to amplitude updates so the sun's corona throbs
        with the actual audio rather than a generic CSS loop."""
        self._iris = iris
        if hasattr(iris, "add_speak_listener"):
            iris.add_speak_listener(self._on_iris_speak)
        else:
            print("[ASGARD] IRIS has no add_speak_listener — speaking-state animations disabled.")
        if hasattr(iris, "add_mouth_level_listener"):
            iris.add_mouth_level_listener(self._on_mouth_level)
        threading.Thread(target=self._iris_idle_poller, daemon=True).start()

    def _on_mouth_level(self, level: float):
        # The amplitude streamer fires ~20 Hz during piper playback. A trailing
        # 0.0 fires when speech ends → corona decays back to idle breathing.
        if level <= 0.001:
            self._eval("odin.resetMouth && odin.resetMouth()")
        else:
            self._eval(f"odin.setMouthLevel && odin.setMouthLevel({level:.3f})")

    def wire_heimdall(self, heimdall):
        """HEIMDALL flips the listening pulse + tells the answer panel to
        clear on the new turn so prior responses don't pile up."""
        self._heimdall_ref = heimdall
        if hasattr(heimdall, "add_listening_listener"):
            heimdall.add_listening_listener(self._on_heimdall_listening)

    def set_companion(self, hermod):
        """Hand ASGARD a reference to HERMOD so the throne room can hide and
        let HERMOD take over when the user is in an app, and vice-versa."""
        self._hermod = hermod

    # ─── Run loop (called from main.py main thread) ───────────────
    def run(self):
        if not self._enabled:
            print("[ASGARD] disabled in config — skipping throne-room UI.")
            return
        if not _HAS_WEBVIEW:
            print("[ASGARD] pywebview not installed — falling back to console-only.")
            return
        if not os.path.exists(_INDEX_PATH):
            print(f"[ASGARD] UI bundle missing at {_INDEX_PATH} — aborting throne room.")
            return

        url = _ui_file_url()
        if self._windowed:
            # NORMAL APP WINDOW: title bar, taskbar entry, alt-tab, resize,
            # minimize. Never on-top, never click-through, never auto-hidden.
            # The user works in other apps; ODIN keeps listening (HEIMDALL is
            # UI-independent) and this window is just the visual cortex you
            # summon from the taskbar. Drag-drop works because focusing
            # Explorer/Chrome no longer makes the window vanish.
            self._window = webview.create_window(
                _WINDOW_TITLE,
                url=url,
                width=self._win_w,
                height=self._win_h,
                resizable=True,
                background_color="#05070f",
                js_api=self._js_api,
            )
            self._visible = True
        else:
            # OVERLAY (legacy living-wallpaper): starts HIDDEN when
            # foreground-aware; the watcher shows it the moment the user is
            # on the desktop. Avoids the boot flash over the user's apps.
            starts_hidden = self._foreground_aware
            self._window = webview.create_window(
                _WINDOW_TITLE,
                url=url,
                fullscreen=self._fullscreen,
                frameless=True,
                easy_drag=False,
                # transparent=True so the user's desktop wallpaper + icons
                # show through around ODIN. background_color must be a valid
                # 6-digit hex — actual transparency comes from the flag.
                transparent=True,
                background_color="#000000",
                on_top=True,
                js_api=self._js_api,
                hidden=starts_hidden,
            )
            self._visible = not starts_hidden

        # Let HERMOD register its window into the SAME webview event loop —
        # pywebview can host multiple windows but they must all be created
        # before webview.start() is called.
        if self._hermod and hasattr(self._hermod, "create_window"):
            try:
                self._hermod.create_window()
            except Exception as e:
                print(f"[ASGARD] HERMOD window create failed: {e}")

        if _HAS_KEYBOARD:
            threading.Thread(target=self._run_hotkeys, daemon=True).start()
        else:
            print("[ASGARD] keyboard not installed — hotkeys disabled.")

        # Foreground watcher: routes desktop/app/fullscreen transitions to
        # both windows so the throne paints over wallpaper only.
        if self._foreground_aware:
            try:
                from output import win32_foreground
                self._foreground_stop = win32_foreground.start_watcher(
                    self._on_foreground_state, poll_interval_sec=0.20
                )
            except Exception as e:
                print(f"[ASGARD] foreground watcher failed to start: {e}")

        try:
            webview.start(self._on_ui_ready, debug=False)
        except Exception as e:
            print(f"[ASGARD] webview.start failed: {e}")
        finally:
            self._iris_poller_stop.set()
            if self._foreground_stop is not None:
                self._foreground_stop.set()

    def _on_ui_ready(self):
        """Called once the DOM is ready. Resolve our HWND so we can flip
        click-through/opacity from any thread."""
        # FindWindowW can briefly return 0 if pywebview hasn't shown the
        # window yet — retry a few times so we don't lose the hotkey path.
        for _ in range(40):
            self._hwnd = _find_hwnd()
            if self._hwnd:
                break
            time.sleep(0.05)
        if not self._hwnd:
            print("[ASGARD] could not resolve HWND — click-through disabled, hotkeys still work via webview.")
        # Boot UI in idle state.
        self._eval("odin.setState('idle')")
        if self._windowed:
            # Let CSS adapt (e.g. hide the overlay-only phantom hotkey hint).
            self._eval("document.body.classList.add('windowed')")
        # Solar orrery: light up the active planet on every MARDUK dispatch.
        # Guarded JS call so an older UI bundle without moduleActive is a no-op.
        if self.marduk and hasattr(self.marduk, "add_dispatch_listener"):
            self.marduk.add_dispatch_listener(self._on_module_dispatch)

    def _on_module_dispatch(self, module_name: str, skill_name: str):
        self._eval(
            f"odin.moduleActive && odin.moduleActive({json.dumps(module_name)}, {json.dumps(skill_name)})"
        )
        # ODIN is opening something for the user — get the World Tree off the
        # screen IMMEDIATELY so the new window doesn't land behind our
        # fullscreen on-top surface. The foreground watcher then takes over
        # (HERMOD shows once the app has focus).
        if skill_name == "open_application" and self._visible and self._foreground_aware:
            self._user_forced_show = False
            self.hide()
            if self._hermod:
                try:
                    self._hermod.show()
                except Exception:
                    pass

    # ─── Speech & listening event listeners ───────────────────────
    def _on_iris_speak(self, text: str):
        if not text:
            return
        # Overlay mode only: un-hide / snap to active so the answer is
        # readable. Windowed mode respects the user's window management —
        # if they minimized ODIN, the voice carries the reply.
        if not self._windowed:
            if self._show_on_speak and not self._visible:
                self.show()
            if self._snap_active_on_speak and self._phantom:
                self.set_phantom(False)
        self._eval(f"odin.speakChunk({json.dumps(text)})")

    def _on_heimdall_listening(self, is_listening: bool):
        if is_listening:
            # New turn — clear stale text from the previous answer so the
            # panel doesn't carry over the prior Q&A.
            self._eval("odin.beginTurn(); odin.setState('listening')")
        else:
            self._eval("odin.setState('thinking')")

    # ─── Idle poller: watches IRIS.is_speaking() ──────────────────
    def _iris_poller(self):
        if not self._iris:
            return False
        try:
            return self._iris.is_speaking()
        except Exception:
            return False

    def _iris_idle_poller(self):
        last_speaking = False
        quiet_since = None
        # MIND panel: watch the working-memory file (what ODIN currently holds
        # in attention — last learned link/file/screen/found path) and mirror
        # it into the UI whenever it changes. Cheap mtime poll, ~2s cadence.
        mind_path = "data/knowledge/last_learned.json"
        mind_mtime = 0.0
        mind_check_at = 0.0
        while not self._iris_poller_stop.is_set():
            speaking = self._iris_poller()
            now = time.monotonic()
            if speaking:
                last_speaking = True
                quiet_since = None
            else:
                if last_speaking:
                    if quiet_since is None:
                        quiet_since = now
                    elif now - quiet_since >= self._idle_state_delay_sec:
                        self._eval("odin.setState('idle')")
                        last_speaking = False
                        quiet_since = None
            if now >= mind_check_at:
                mind_check_at = now + 2.0
                try:
                    mt = os.path.getmtime(mind_path)
                    if mt != mind_mtime:
                        mind_mtime = mt
                        with open(mind_path, "r", encoding="utf-8") as f:
                            mind = json.load(f)
                        payload = json.dumps({
                            "title": str(mind.get("title", ""))[:80],
                            "kind": str(mind.get("kind", ""))[:24],
                            "ts": str(mind.get("ts", ""))[:19],
                        })
                        self._eval(f"odin.setMind && odin.setMind({payload})")
                except (OSError, ValueError):
                    pass
            time.sleep(0.15)

    # ─── Visibility / mode controls ───────────────────────────────
    def show(self):
        if self._window:
            try:
                self._window.show()
                self._visible = True
                if not self._hwnd:
                    self._hwnd = _find_hwnd()
                # Re-apply current mode after a show (Win32 styles can reset).
                self._apply_mode_styles()
            except Exception as e:
                print(f"[ASGARD] show failed: {e}")

    def hide(self):
        if self._window:
            try:
                self._window.hide()
                self._visible = False
            except Exception as e:
                print(f"[ASGARD] hide failed: {e}")

    def toggle_visibility(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    def set_phantom(self, phantom: bool):
        """Flip click-through + opacity. PHANTOM = translucent, lets clicks
        through to the desktop. ACTIVE = opaque, captures input.
        Windowed mode: meaningless for a normal app window — no-op."""
        if self._windowed:
            return
        self._phantom = bool(phantom)
        if not self._hwnd:
            self._hwnd = _find_hwnd()
        self._apply_mode_styles()
        # Mirror to JS so CSS can dim the UI for visual cue.
        self._eval(f"document.body.classList.toggle('phantom', {str(self._phantom).lower()})")

    def force_active(self):
        """User explicitly asked for the throne (tray menu / hotkey).
        Locks the throne ON-screen until the next foreground transition
        away from 'desktop' OR they hide it again. Avoids the watcher
        snatching the throne away half a second after the user opened it."""
        self._user_forced_show = True
        self.show()
        self.set_phantom(False)
        if self._hermod:
            try: self._hermod.hide()
            except Exception: pass

    def _on_foreground_state(self, state: str):
        """state ∈ {'desktop', 'app', 'fullscreen'}. Routes show/hide to
        the throne + tells HERMOD to do the inverse."""
        if state == "desktop":
            # Wallpaper has focus — throne room belongs here.
            self._user_forced_show = False   # any new desktop entry resets the override
            self.show()
            self.set_phantom(False)
            if self._hermod:
                try: self._hermod.on_foreground_state(state)
                except Exception: pass
        elif state == "app":
            if self._user_forced_show:
                return    # user pinned the throne; honor their override
            self.hide()
            if self._hermod:
                try: self._hermod.on_foreground_state(state)
                except Exception: pass
        else:   # fullscreen
            # Games / videos / Zoom shares — both go dark.
            self.hide()
            if self._hermod:
                try: self._hermod.on_foreground_state(state)
                except Exception: pass

    def toggle_phantom(self):
        self.set_phantom(not self._phantom)

    def _apply_mode_styles(self):
        if self._windowed:
            return   # normal app window — no layered-window alpha games
        if not self._hwnd:
            return
        alpha = self._phantom_alpha if self._phantom else self._active_alpha
        _set_window_mode(self._hwnd, click_through=self._phantom, alpha=alpha)

    def show_text(self, text: str):
        if not text:
            return
        if not self._visible:
            self.show()
        self._eval(f"odin.beginTurn(); odin.showAnswerText({json.dumps(text)})")

    # ─── Command bar ──────────────────────────────────────────────
    def submit_command(self, text: str):
        """Typed line from the orrery command bar. HERMOD owns the text
        pipeline (fast-routes → cloud chain), so delegate to it."""
        text = (text or "").strip()
        if not text:
            return
        if self._hermod and hasattr(self._hermod, "handle_text"):
            self._hermod.handle_text(text)
        elif self._iris:
            self._iris.speak("The command bar needs Hermod, and he is not awake.")

    # ─── Dossier data ─────────────────────────────────────────────
    def get_module_info_json(self, module_name: str) -> str:
        name = str(module_name or "").strip().upper()
        mod = self.marduk.get_module(name) if self.marduk else None
        if not mod:
            return json.dumps({"name": name, "skills": []})
        skills = [
            {"name": s.get("name", "?"), "description": s.get("description", "")}
            for s in mod.skills
        ]
        return json.dumps({"name": name, "layer": mod.LAYER, "skills": skills})

    # ─── Drag & drop: feed the planets ────────────────────────────
    # Each layer-planet has a purpose (keep in sync with PLANETS[].drop
    # in app.js). Work runs on a worker thread; results are spoken via
    # IRIS so they land in the answer panel like any other reply.
    def handle_drop_json(self, payload_json: str) -> str:
        try:
            payload = json.loads(payload_json or "{}")
        except Exception:
            return "Drop payload unreadable."
        layer = str(payload.get("layer") or "MEMORY").upper()
        files = [f for f in (payload.get("files") or []) if isinstance(f, dict)]
        url = str(payload.get("url") or "").strip()
        text = str(payload.get("text") or "").strip()

        if files and not any(f.get("path") for f in files):
            return ("This pywebview build doesn't expose dropped file paths "
                    "(needs pywebviewFullPath). Try a link or text instead.")
        if not files and not url and not text:
            return "Nothing droppable detected."

        threading.Thread(target=self._handle_drop, args=(layer, files, url, text),
                         daemon=True).start()
        if url:
            return "ok"
        names = ", ".join(f.get("name", "?") for f in files[:3]) or "text"
        return f"Routing {names} to {layer}…"

    def _handle_drop(self, layer: str, files: list, url: str, text: str):
        try:
            if url:
                self._speak_result(self._drop_url(layer, url))
                return
            if files:
                for f in files:
                    path = f.get("path") or ""
                    if path and os.path.exists(path):
                        self._speak_result(self._drop_file(layer, path))
                    else:
                        self._speak_result(f"I couldn't reach {f.get('name', 'that file')} on disk.")
                return
            if text:
                self._speak_result(self._drop_text(layer, text))
        except Exception as e:
            print(f"[ASGARD] drop handling failed: {e}")
            self._speak_result("That offering slipped through my fingers. Check the log.")

    def _drop_url(self, layer: str, url: str) -> str:
        # Links are knowledge; SESHAT learns them into the vault regardless
        # of planet — the planet just adds a focus hint for the distiller.
        focus = {
            "INTELLIGENCE": "technical depth — concepts, architecture, how it works",
            "PROTECTION": "risks, vulnerabilities, security implications",
            "MANAGEMENT": "actionable tasks, deadlines, decisions",
        }.get(layer, "")
        result = self.send("SESHAT", "learn_link", url=url, focus=focus)
        return str(result)

    def _drop_file(self, layer: str, path: str) -> str:
        ext = Path(path).suffix.lower()
        name = Path(path).name

        if layer == "OUTPUT":
            if ext in (".txt", ".md", ".log"):
                try:
                    content = Path(path).read_text(encoding="utf-8", errors="replace")[:600]
                except Exception as e:
                    return f"Couldn't read {name}: {e}"
                self.send("IRIS", "speak_text", text=content)
                return ""   # IRIS is already reading the content itself
            try:
                os.startfile(path)  # no skill opens arbitrary files; Saturn does it directly
                return f"Opened {name}."
            except Exception as e:
                return f"Couldn't open {name}: {e}"

        if layer == "UTILITY" and ext in (".xlsx", ".xlsm", ".xltx", ".xls"):
            return str(self.send("GANESH", "excel_summarize", path=path))

        if layer == "INTELLIGENCE" and ext in (
            ".py", ".js", ".ts", ".java", ".c", ".cpp", ".cs", ".go", ".rs",
            ".rb", ".php", ".sh", ".ps1", ".sql", ".html", ".css", ".yaml", ".yml", ".json",
        ):
            try:
                code = Path(path).read_text(encoding="utf-8", errors="replace")[:12000]
            except Exception as e:
                return f"Couldn't read {name}: {e}"
            return str(self.send("SARASWATI", "explain_code", code=code, language=ext.lstrip(".")))

        if layer == "PROTECTION":
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest_dir = Path(self._backup_root) / "dropped" / stamp
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest_dir / name)
            return f"Sealed a backup of {name} in the vaults of Mars."

        # Documents ODIN can actually read → SESHAT extracts, distills, and
        # vaults them (answerable offline after), rather than filing the raw
        # bytes away blind.
        if ext in (".pdf", ".docx", ".doc", ".pptx", ".ppt", ".odt", ".rtf",
                   ".epub", ".txt", ".md", ".markdown", ".csv", ".html", ".htm"):
            focus = {
                "INTELLIGENCE": "technical depth — concepts, architecture, how it works",
                "PROTECTION": "risks, vulnerabilities, security implications",
                "MANAGEMENT": "actionable tasks, deadlines, decisions",
            }.get(layer, "")
            return str(self.send("SESHAT", "learn_file", path=path, focus=focus))

        # Truly unknown/binary: archive to the Brain vault where Obsidian and
        # HERMES indexing can still reach it.
        dest_dir = Path(self._vault_root) / "ODIN" / "learned" / "dropped"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / name
        if dest.exists():
            dest = dest_dir / f"{dest.stem}_{datetime.now().strftime('%H%M%S')}{dest.suffix}"
        shutil.copy2(path, dest)
        return f"{name} archived to the Brain vault."

    def _drop_text(self, layer: str, text: str) -> str:
        if layer == "INPUT" or layer == "CORE":
            # Mercury (and the sun itself) treat dropped text as a command.
            self.submit_command(text)
            return ""   # the pipeline speaks its own reply
        if layer == "MANAGEMENT":
            return str(self.send("CHRONOS", "set_reminder", text=text[:200], when="in 1 hour"))
        if layer == "INTELLIGENCE":
            return str(self.send("SARASWATI", "ask_ai",
                                 question=f"Summarize this in two sentences:\n\n{text[:6000]}"))
        # Default: pin it as a vault note.
        dest_dir = Path(self._vault_root) / "ODIN" / "learned" / "dropped"
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        note = dest_dir / f"drop_{stamp}.md"
        note.write_text(f"# Dropped note — {stamp}\n\n{text}\n", encoding="utf-8")
        return "Noted and pinned to the Brain vault."

    def _speak_result(self, result: str):
        result = (result or "").strip()
        if not result:
            return
        if self._iris:
            self._iris.speak(result)
        else:
            self.show_text(result)

    # ─── Hotkeys ──────────────────────────────────────────────────
    def _run_hotkeys(self):
        try:
            keyboard.add_hotkey(self._hotkey_toggle, self.toggle_phantom,
                                suppress=False, trigger_on_release=False)
            keyboard.add_hotkey(self._hotkey_hide, self.toggle_visibility,
                                suppress=False, trigger_on_release=False)
            print(f"[ASGARD] hotkeys: {self._hotkey_toggle} = active/phantom, "
                  f"{self._hotkey_hide} = hide/show")
            keyboard.wait()
        except Exception as e:
            print(f"[ASGARD] hotkey listener failed: {e}")

    # ─── JS bridge ────────────────────────────────────────────────
    def _eval(self, js: str):
        if not self._window:
            return
        try:
            self._window.evaluate_js(js)
        except Exception:
            pass
