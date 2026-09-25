"""Foreground-window state watcher.

Classifies the currently-focused window into one of three states so the
UI modules (ASGARD throne room, HERMOD corner avatar) can decide what to
paint:

    DESKTOP    — user has clicked the Windows desktop / no real app focused.
                 Foreground window class is WorkerW / Progman / Shell_TrayWnd.
    APP        — any normal application is in front (Chrome, VS Code, etc).
    FULLSCREEN — the foreground window covers an entire monitor and is not
                 a desktop class (game, fullscreen video, presentation).

Polls every 200 ms via plain Win32 calls — no event hooks, no admin rights,
trivially cheap. Fires the supplied callback only on TRANSITIONS, not every
tick, so subscribers (ASGARD, HERMOD) don't get hammered.
"""

import ctypes
import threading
import time
from ctypes import wintypes


# ── State constants ────────────────────────────────────────────────
DESKTOP    = "desktop"
APP        = "app"
FULLSCREEN = "fullscreen"


# ── Win32 plumbing ─────────────────────────────────────────────────
_user32 = ctypes.windll.user32

# These classes correspond to the Windows shell desktop layer. WorkerW is
# the modern wallpaper host; Progman is the legacy one. Shell_TrayWnd is
# the taskbar (when user click-throughs from the wallpaper). Treating
# them all as "desktop" matches user intuition.
_DESKTOP_CLASSES = {"WorkerW", "Progman", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize",    wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork",    wintypes.RECT),
        ("dwFlags",   wintypes.DWORD),
    ]


def _get_class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_window_rect(hwnd):
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r


def _get_monitor_rect(hwnd):
    """Return the rcMonitor RECT for the monitor that hosts hwnd, or None."""
    MONITOR_DEFAULTTONEAREST = 2
    h_monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    if not h_monitor:
        return None
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not _user32.GetMonitorInfoW(h_monitor, ctypes.byref(info)):
        return None
    return info.rcMonitor


def current_state() -> str:
    """One-shot read of the foreground state. Safe to call from any thread."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return DESKTOP
    cls = _get_class_name(hwnd)
    if cls in _DESKTOP_CLASSES:
        return DESKTOP
    # Fullscreen check: window rect == monitor rect (within 2 px slop for
    # subpixel rounding and DPI quirks).
    win_rect = _get_window_rect(hwnd)
    mon_rect = _get_monitor_rect(hwnd)
    if mon_rect is not None:
        slop = 2
        if (abs(win_rect.left   - mon_rect.left)   <= slop and
            abs(win_rect.top    - mon_rect.top)    <= slop and
            abs(win_rect.right  - mon_rect.right)  <= slop and
            abs(win_rect.bottom - mon_rect.bottom) <= slop):
            return FULLSCREEN
    return APP


def start_watcher(on_change, poll_interval_sec: float = 0.20) -> threading.Event:
    """Spawn a daemon thread that polls foreground state and fires on_change
    ONLY when the state transitions. Returns a stop-event the caller can set
    to shut the thread down."""
    stop = threading.Event()
    last_state = {"value": None}

    def loop():
        # Seed: read once before announcing so we don't emit a spurious
        # "transition from None" on the very first tick.
        try:
            last_state["value"] = current_state()
            on_change(last_state["value"])
        except Exception as e:
            print(f"[foreground] initial read failed: {e}")
        while not stop.is_set():
            try:
                state = current_state()
                if state != last_state["value"]:
                    last_state["value"] = state
                    on_change(state)
            except Exception as e:
                print(f"[foreground] poll error: {e}")
            stop.wait(poll_interval_sec)

    t = threading.Thread(target=loop, daemon=True, name="foreground-watcher")
    t.start()
    return stop
