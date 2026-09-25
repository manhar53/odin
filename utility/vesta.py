# VESTA — Roman — goddess of hearth, household, and the eternal flame
# UI presence: system tray icon, status window, listen-mode controller.
# The "beautiful UI" pass uses customtkinter on top of the existing tk
# foundation — modern dark theme, animated status orb, segmented controls,
# stats panel. Falls back to vanilla tk if customtkinter isn't installed
# so ODIN still boots on minimal Python installs.

import os
import time
import threading
import tkinter as tk
from core.marduk import OdinModule

try:
    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    _HAS_CTK = True
except ImportError:
    _HAS_CTK = False

try:
    import pystray
    from pystray import Menu, MenuItem
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False

try:
    import keyboard
    _HAS_KEYBOARD = True
except ImportError:
    _HAS_KEYBOARD = False


_COLORS = {
    "idle":      "#3a3a3a",
    "listening": "#27c4f5",
    "thinking":  "#f5b127",
    "speaking":  "#27f55c",
}

# Glow halo colors (slightly lighter than the orb fill — emulates a soft ring)
_HALO = {
    "idle":      "#1a1a1a",
    "listening": "#1a3a4a",
    "thinking":  "#4a3a1a",
    "speaking":  "#1a4a2a",
}


class Vesta(OdinModule):
    MODULE_NAME = "VESTA"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("vesta", {})
        self._listen_mode = cfg.get("listen_mode", "always")
        self._hotkey = cfg.get("hotkey", "alt")
        self._show_window = cfg.get("show_window", True)
        # When True, the GUI window is hidden at startup; only the tray icon
        # is visible. The user opens the window via the tray menu or by
        # double-clicking the tray icon. Designed for "always-on background"
        # usage where ODIN sits quietly until you wake it.
        self._start_hidden = cfg.get("start_hidden", False)

        self._state = "idle"
        self._lock = threading.Lock()
        self._heimdall = None
        self._tray = None
        self._gui_root = None
        self._gui_canvas = None
        self._gui_circle = None
        self._gui_label = None
        self._gui_perf_label = None
        self._gui_mode_label = None
        self._gui_persona_label = None
        self._persona = "mythic"
        self._stop = threading.Event()

        self._cmd_count = 0
        self._total_time = 0.0
        self._last_time = None
        self._think_start = None

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "set_listen_mode",
                "description": "Set ODIN listen mode: 'always' or 'hotkey'.",
                "parameters": {
                    "mode": {"type": "string", "description": "'always' or 'hotkey'"}
                },
                "required": ["mode"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        try:
            if skill_name == "set_listen_mode":
                return self._cmd_set_listen_mode(args.get("mode", ""))
        except Exception as e:
            return f"[VESTA] Error: {e}"
        return f"[VESTA] Unknown skill: {skill_name}"

    def set_heimdall(self, heimdall):
        self._heimdall = heimdall

    def set_state(self, state: str):
        with self._lock:
            self._state = state
            if state == "thinking":
                self._think_start = time.perf_counter()
            elif state == "idle" and self._think_start is not None:
                elapsed = time.perf_counter() - self._think_start
                self._cmd_count += 1
                self._total_time += elapsed
                self._last_time = elapsed
                self._think_start = None

        if self._gui_root is not None:
            try:
                self._gui_root.after(0, self._refresh_gui)
            except Exception:
                pass
        if self._tray is not None:
            try:
                self._tray.title = f"ODIN — {state}"
            except Exception:
                pass

    def run(self):
        """Entry point — starts tray + hotkey in daemons, then runs Tk on this thread."""
        # Pull current persona from LOKI now that all modules are registered.
        try:
            current = self.send("LOKI", "get_persona")
            if current in ("mythic", "assistant"):
                self._persona = current
        except Exception:
            pass

        if _HAS_TRAY:
            threading.Thread(target=self._run_tray, daemon=True).start()
        else:
            print("[VESTA] pystray not installed — tray icon disabled.")
        if _HAS_KEYBOARD:
            threading.Thread(target=self._run_hotkey, daemon=True).start()
        else:
            print("[VESTA] keyboard not installed — push-to-talk disabled.")

        if self._show_window:
            self._run_gui()
        else:
            self._stop.wait()

    def start_background(self):
        """Tray-only mode: spin up the tray icon + push-to-talk hotkey on
        daemon threads and return immediately. Used when ASGARD owns the
        main thread for its full-screen pywebview window — VESTA's tk
        settings panel is suppressed (you'd never see it under the throne
        room anyway). Returns the thread refs in case the caller wants
        to monitor them."""
        try:
            current = self.send("LOKI", "get_persona")
            if current in ("mythic", "assistant"):
                self._persona = current
        except Exception:
            pass

        threads = []
        if _HAS_TRAY:
            t = threading.Thread(target=self._run_tray, daemon=True)
            t.start(); threads.append(t)
        else:
            print("[VESTA] pystray not installed — tray icon disabled.")
        if _HAS_KEYBOARD:
            t = threading.Thread(target=self._run_hotkey, daemon=True)
            t.start(); threads.append(t)
        else:
            print("[VESTA] keyboard not installed — push-to-talk disabled.")
        return threads

    def _cmd_set_listen_mode(self, mode: str) -> str:
        mode = mode.strip().lower()
        if mode not in ("always", "hotkey"):
            return "Mode must be 'always' or 'hotkey'."
        self._listen_mode = mode
        if self._heimdall:
            self._heimdall.set_listen_mode(mode)
        if self._gui_root is not None:
            try:
                self._gui_root.after(0, self._refresh_gui)
            except Exception:
                pass
        return f"Listen mode set to {mode}."

    def _toggle_persona(self, _event=None):
        new_mode = "assistant" if self._persona == "mythic" else "mythic"
        try:
            self.send("LOKI", "set_persona", mode=new_mode)
            self._persona = new_mode
        except Exception as e:
            print(f"[VESTA] Persona toggle failed: {e}")
            return
        if self._gui_root is not None:
            try:
                self._gui_root.after(0, self._refresh_gui)
            except Exception:
                pass

    def _set_persona(self, mode: str):
        if mode not in ("mythic", "assistant"):
            return
        if mode == self._persona:
            return
        try:
            self.send("LOKI", "set_persona", mode=mode)
            self._persona = mode
        except Exception as e:
            print(f"[VESTA] Persona switch failed: {e}")
            return
        if self._gui_root is not None:
            try:
                self._gui_root.after(0, self._refresh_gui)
            except Exception:
                pass

    # === GUI (runs on main thread) ===
    def _run_gui(self):
        if _HAS_CTK:
            self._run_gui_ctk()
        else:
            self._run_gui_legacy()

    def _run_gui_ctk(self):
        """Modern UI: customtkinter dark theme + glow halo + segmented
        controls + stats card. Lightweight (no Qt / Electron), boots in
        ~150ms, fits the 8 GB memory budget."""
        root = ctk.CTk()
        root.title("ODIN")
        root.geometry("360x440")
        root.minsize(360, 440)
        root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        self._gui_root = root
        if self._start_hidden:
            # Withdraw before mainloop starts so the window flashes for ~0 frames.
            root.after(10, root.withdraw)

        # ── Header ────────────────────────────────────────────────
        header = ctk.CTkLabel(
            root, text="ODIN", font=ctk.CTkFont(size=22, weight="bold"),
            text_color="#e0e0e0",
        )
        header.pack(pady=(18, 2))
        subhead = ctk.CTkLabel(
            root, text="Omniscient Digital Intelligence Node",
            font=ctk.CTkFont(size=10), text_color="#888888",
        )
        subhead.pack()

        # ── Glowing status orb on a Canvas ───────────────────────
        canvas = tk.Canvas(root, width=140, height=140,
                           bg="#1a1a1a", highlightthickness=0, bd=0)
        canvas.pack(pady=(16, 6))
        # Outer halo (lighter ring around the orb).
        self._gui_halo = canvas.create_oval(10, 10, 130, 130,
                                            fill=_HALO["idle"], outline="")
        # Inner orb.
        self._gui_circle = canvas.create_oval(28, 28, 112, 112,
                                              fill=_COLORS["idle"], outline="")
        self._gui_canvas = canvas

        # State label below orb.
        self._gui_label = ctk.CTkLabel(
            root, text="Idle", font=ctk.CTkFont(size=18, weight="bold"),
            text_color="#e0e0e0",
        )
        self._gui_label.pack(pady=(4, 0))
        self._gui_perf_label = ctk.CTkLabel(
            root, text="—", font=ctk.CTkFont(size=10), text_color="#666666",
        )
        self._gui_perf_label.pack(pady=(2, 14))

        # ── Listen mode: segmented control ───────────────────────
        listen_frame = ctk.CTkFrame(root, fg_color="transparent")
        listen_frame.pack(pady=4)
        ctk.CTkLabel(listen_frame, text="Listen mode",
                     font=ctk.CTkFont(size=10), text_color="#888888").pack()
        self._gui_listen_seg = ctk.CTkSegmentedButton(
            listen_frame, values=["Always", f"Tap {self._hotkey.upper()}"],
            command=self._on_listen_seg_change,
            font=ctk.CTkFont(size=10),
        )
        self._gui_listen_seg.pack(pady=2)
        self._gui_listen_seg.set("Always" if self._listen_mode == "always"
                                  else f"Tap {self._hotkey.upper()}")

        # ── Persona: segmented control ───────────────────────────
        persona_frame = ctk.CTkFrame(root, fg_color="transparent")
        persona_frame.pack(pady=4)
        ctk.CTkLabel(persona_frame, text="Persona",
                     font=ctk.CTkFont(size=10), text_color="#888888").pack()
        self._gui_persona_seg = ctk.CTkSegmentedButton(
            persona_frame, values=["Mythic", "Assistant"],
            command=self._on_persona_seg_change,
            font=ctk.CTkFont(size=10),
        )
        self._gui_persona_seg.pack(pady=2)
        self._gui_persona_seg.set(
            "Mythic" if self._persona == "mythic" else "Assistant"
        )

        # ── Footer hint ──────────────────────────────────────────
        self._gui_mode_label = ctk.CTkLabel(
            root, text=self._mode_text(), font=ctk.CTkFont(size=9),
            text_color="#666666",
        )
        self._gui_mode_label.pack(side="bottom", pady=(0, 10))

        self._refresh_gui()
        root.mainloop()

    def _run_gui_legacy(self):
        """Vanilla-tk fallback when customtkinter isn't installed.
        Kept verbatim from the previous UI so nothing regresses."""
        root = tk.Tk()
        root.title("ODIN")
        root.geometry("280x250")
        root.configure(bg="#101010")
        root.protocol("WM_DELETE_WINDOW", lambda: root.withdraw())
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        self._gui_root = root
        if self._start_hidden:
            root.after(10, root.withdraw)

        canvas = tk.Canvas(root, width=80, height=80, bg="#101010", highlightthickness=0)
        canvas.pack(pady=14)
        self._gui_circle = canvas.create_oval(10, 10, 70, 70, fill=_COLORS["idle"], outline="")
        self._gui_canvas = canvas

        self._gui_label = tk.Label(root, text="Idle", fg="#e0e0e0", bg="#101010",
                                    font=("Segoe UI", 13, "bold"))
        self._gui_label.pack()
        self._gui_perf_label = tk.Label(root, text="—", fg="#666666", bg="#101010",
                                         font=("Segoe UI", 9))
        self._gui_perf_label.pack(pady=4)
        self._gui_persona_label = tk.Label(root, text=self._persona_text(),
                                            fg="#27c4f5", bg="#101010",
                                            font=("Segoe UI", 9, "bold"),
                                            cursor="hand2")
        self._gui_persona_label.pack(pady=4)
        self._gui_persona_label.bind("<Button-1>", self._toggle_persona)
        self._gui_mode_label = tk.Label(root, text=self._mode_text(),
                                         fg="#999999", bg="#101010",
                                         font=("Segoe UI", 8))
        self._gui_mode_label.pack(side="bottom", pady=4)
        self._refresh_gui()
        root.mainloop()

    def _on_listen_seg_change(self, value: str):
        mode = "always" if value == "Always" else "hotkey"
        self._cmd_set_listen_mode(mode)

    def _on_persona_seg_change(self, value: str):
        self._set_persona("mythic" if value == "Mythic" else "assistant")

    def _persona_text(self) -> str:
        if self._persona == "assistant":
            return "Persona: Assistant  ⟳ click for Mythic"
        return "Persona: Mythic  ⟳ click for Assistant"

    def _mode_text(self) -> str:
        if self._listen_mode == "hotkey":
            return f"Tap {self._hotkey.upper()} to talk"
        return "Always listening · say 'Hey ODIN'"

    def _refresh_gui(self):
        if self._gui_root is None:
            return
        with self._lock:
            state = self._state
            count = self._cmd_count
            avg = self._total_time / count if count else 0
            last = self._last_time
        # Orb + halo coloring (works for both ctk and legacy paths).
        if self._gui_circle is not None and self._gui_canvas is not None:
            self._gui_canvas.itemconfig(self._gui_circle,
                                         fill=_COLORS.get(state, "#3a3a3a"))
            if getattr(self, "_gui_halo", None) is not None:
                self._gui_canvas.itemconfig(self._gui_halo,
                                             fill=_HALO.get(state, "#1a1a1a"))
        if self._gui_label is not None:
            # ctk labels use .configure, legacy tk labels use .config. Both
            # accept .configure on modern Pythons, so just use that.
            try:
                self._gui_label.configure(text=state.capitalize())
            except Exception:
                self._gui_label.config(text=state.capitalize())
        if self._gui_perf_label is not None:
            text = (f"#{count}: last {last:.1f}s · avg {avg:.1f}s"
                    if count and last is not None else "—")
            try:
                self._gui_perf_label.configure(text=text)
            except Exception:
                self._gui_perf_label.config(text=text)
        if self._gui_mode_label is not None:
            try:
                self._gui_mode_label.configure(text=self._mode_text())
            except Exception:
                self._gui_mode_label.config(text=self._mode_text())
        # Legacy-path persona label.
        if self._gui_persona_label is not None and not _HAS_CTK:
            self._gui_persona_label.config(text=self._persona_text())
        # ctk segmented controls — sync them when persona/listen change from
        # outside the UI (e.g. voice "set persona mythic").
        if _HAS_CTK and hasattr(self, "_gui_listen_seg") and self._gui_listen_seg:
            target = "Always" if self._listen_mode == "always" else f"Tap {self._hotkey.upper()}"
            if self._gui_listen_seg.get() != target:
                self._gui_listen_seg.set(target)
        if _HAS_CTK and hasattr(self, "_gui_persona_seg") and self._gui_persona_seg:
            target = "Mythic" if self._persona == "mythic" else "Assistant"
            if self._gui_persona_seg.get() != target:
                self._gui_persona_seg.set(target)

    # === TRAY ===
    def _make_icon(self):
        img = Image.new("RGB", (64, 64), color="#101010")
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill="#27c4f5")
        d.ellipse((20, 20, 44, 44), fill="#101010")
        d.ellipse((28, 28, 36, 36), fill="#27c4f5")
        return img

    def _run_tray(self):
        try:
            icon_img = self._make_icon()
        except Exception as e:
            print(f"[VESTA] Could not build tray icon: {e}")
            return

        def _get_asgard():
            try:
                return self.marduk.get_module("ASGARD") if self.marduk else None
            except Exception:
                return None

        def on_show(_icon, _item):
            # Prefer the throne room when ASGARD is up; otherwise un-iconify
            # the legacy tk window so the tray "Show window" never feels dead.
            asgard = _get_asgard()
            if asgard:
                asgard.show()
                asgard.set_phantom(False)
                return
            if self._gui_root is not None:
                try:
                    self._gui_root.after(0, self._gui_root.deiconify)
                except Exception:
                    pass

        def on_throne_active(_icon, _item):
            asgard = _get_asgard()
            if asgard:
                # force_active locks the throne ON until the user goes back to
                # desktop — overrides the foreground-aware auto-hide.
                if hasattr(asgard, "force_active"):
                    asgard.force_active()
                else:
                    asgard.show(); asgard.set_phantom(False)

        def on_throne_phantom(_icon, _item):
            asgard = _get_asgard()
            if asgard:
                asgard.show()
                asgard.set_phantom(True)

        def on_throne_hide(_icon, _item):
            asgard = _get_asgard()
            if asgard:
                asgard.hide()

        def on_set_always(_icon, _item):
            self._cmd_set_listen_mode("always")

        def on_set_hotkey(_icon, _item):
            self._cmd_set_listen_mode("hotkey")

        def on_persona_mythic(_icon, _item):
            self._set_persona("mythic")

        def on_persona_assistant(_icon, _item):
            self._set_persona("assistant")

        def on_quit(_icon, _item):
            self._stop.set()
            try:
                _icon.stop()
            except Exception:
                pass
            os._exit(0)

        menu = Menu(
            MenuItem("Show window", on_show, default=True),
            Menu.SEPARATOR,
            MenuItem("World Tree: Active (opaque, clickable)",   on_throne_active),
            MenuItem("World Tree: Phantom (translucent, ghost)", on_throne_phantom),
            MenuItem("World Tree: Hide",                         on_throne_hide),
            Menu.SEPARATOR,
            MenuItem("Listen always", on_set_always,
                     checked=lambda i: self._listen_mode == "always", radio=True),
            MenuItem(f"Tap-to-talk ({self._hotkey.upper()})", on_set_hotkey,
                     checked=lambda i: self._listen_mode == "hotkey", radio=True),
            Menu.SEPARATOR,
            MenuItem("Mythic (god Odin)", on_persona_mythic,
                     checked=lambda i: self._persona == "mythic", radio=True),
            MenuItem("Assistant (factual)", on_persona_assistant,
                     checked=lambda i: self._persona == "assistant", radio=True),
            Menu.SEPARATOR,
            MenuItem("Quit ODIN", on_quit),
        )
        self._tray = pystray.Icon("odin", icon_img, "ODIN — idle", menu)
        try:
            self._tray.run()
        except Exception as e:
            print(f"[VESTA] Tray error: {e}")

    # === HOTKEY (tap-to-talk) ===
    def _run_hotkey(self):
        if self._heimdall is None:
            print("[VESTA] No HEIMDALL reference — tap-to-talk disabled.")
            return
        try:
            # Single tap = one listen session; ODIN records until silence,
            # responds, returns to idle. add_hotkey fires once per press.
            keyboard.add_hotkey(self._hotkey, self._on_tap)
            self._stop.wait()
        except Exception as e:
            print(f"[VESTA] Hotkey error (try running as admin): {e}")

    def _on_tap(self):
        # Works in BOTH listen modes — in 'always' it's an override that
        # bypasses the wake word, in 'hotkey' it's the only way to start
        # a recording. This lets the user keep wake-word convenience while
        # also having a deterministic Alt-to-talk fallback when the wake
        # word mishears.
        if self._heimdall is None:
            return
        self._heimdall.trigger_record_start()
