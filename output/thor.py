# THOR — Norse — god of strength and decisive action
# Execution: runs commands, opens apps, controls system, takes action

import subprocess
import os
import json
import threading
import time
import webbrowser
import psutil
import pyautogui
from core.marduk import OdinModule

try:
    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    _HAS_PYCAW = True
except ImportError:
    _HAS_PYCAW = False


# Common spoken names → executable name (Windows). The 'start' command
# also resolves anything in the App Paths registry (chrome, spotify, etc.)
_APP_ALIASES = {
    "calculator":      "calc",
    "command prompt":  "cmd",
    "terminal":        "wt",
    "file explorer":   "explorer",
    "explorer":        "explorer",
    "task manager":    "taskmgr",
    "control panel":   "control",
    "paint":           "mspaint",
    "wordpad":         "write",
    "settings":        "ms-settings:",
    "browser":         "msedge",
    "edge":            "msedge",
    "vs code":         "code",
    "visual studio code": "code",
}

# Known sites — opened in the default browser instead of as a registered app.
# Anything not here is tried as an app first.
_KNOWN_URLS = {
    "youtube":         "https://www.youtube.com",
    "youtube music":   "https://music.youtube.com",
    "gmail":           "https://mail.google.com",
    "email":           "https://mail.google.com",
    "mail":            "https://mail.google.com",
    "inbox":           "https://mail.google.com",
    "google":          "https://www.google.com",
    "google drive":    "https://drive.google.com",
    "google calendar": "https://calendar.google.com",
    "google maps":     "https://maps.google.com",
    "github":          "https://github.com",
    "twitter":         "https://twitter.com",
    "x":               "https://x.com",
    "reddit":          "https://www.reddit.com",
    "instagram":       "https://www.instagram.com",
    "facebook":        "https://www.facebook.com",
    "amazon":          "https://www.amazon.com",
    "netflix":         "https://www.netflix.com",
    "stack overflow":  "https://stackoverflow.com",
    "wikipedia":       "https://www.wikipedia.org",
    "chatgpt":         "https://chat.openai.com",
    "claude":          "https://claude.ai",
    "whatsapp":        "https://web.whatsapp.com",
    "linkedin":        "https://www.linkedin.com",
}


def _find_start_menu_shortcut(name: str) -> str | None:
    """Search Start Menu locations for a .lnk matching `name`. Returns the
    absolute path to the highest-scoring match, or None.

    Scoring biases toward shortcuts whose filename IS the search term
    (e.g. "Steam.lnk") over shortcuts that merely contain it as a substring
    ("VLC media player - reset preferences and cache files.lnk"). The
    cleanest launcher is almost always the shortest name that contains
    the term."""
    roots = [
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    name_l = name.lower().strip()
    if not name_l:
        return None
    candidates = []  # list of (score, length, path)
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if not f.lower().endswith(".lnk"):
                    continue
                base = f[:-4].lower()
                # Score buckets:
                #   100  exact name match
                #    80  starts with name + word boundary
                #    60  contains name as a whole word
                #    40  substring anywhere
                score = 0
                if base == name_l:
                    score = 100
                elif base.startswith(name_l + " ") or base.startswith(name_l + "-"):
                    score = 80
                elif f" {name_l} " in f" {base} " or base.endswith(" " + name_l):
                    score = 60
                elif name_l in base:
                    score = 40
                if score > 0:
                    candidates.append((score, len(base), os.path.join(dirpath, f)))
    if not candidates:
        return None
    # Highest score first; among equal scores, the SHORTEST filename
    # (least extra text) wins — that's almost always the canonical launcher.
    candidates.sort(key=lambda t: (-t[0], t[1]))
    return candidates[0][2]


# Module-level cache for the pycaw IAudioEndpointVolume. Re-creating this on
# every volume command churns comtypes COM-pointer reference counts, which
# was producing intermittent "access violation reading 0xFFFFFFFFFFFFFFFF"
# crashes inside _compointer_base.__del__ when Python garbage-collected the
# transient endpoint object on a different thread than it was created.
# Caching keeps a single Python reference alive for the process lifetime,
# so there's no churn and no double-free in __del__.
_AUDIO_ENDPOINT_CACHE = None


def _audio_endpoint():
    global _AUDIO_ENDPOINT_CACHE
    if not _HAS_PYCAW:
        return None
    if _AUDIO_ENDPOINT_CACHE is not None:
        return _AUDIO_ENDPOINT_CACHE
    # GIL/HEIMDALL invoke skills from worker threads — pycaw's COM calls fail
    # there unless this thread has CoInitialize'd. Re-initialising is a no-op
    # (raises OSError 'already initialized'); we swallow it.
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    try:
        devices = AudioUtilities.GetSpeakers()
        # Newer pycaw (~20240210+) wraps IMMDevice in an AudioDevice class
        # that exposes the endpoint volume directly. Older pycaw returned
        # the raw IMMDevice with an Activate(IID, ...) method. Support both.
        if hasattr(devices, "EndpointVolume"):
            _AUDIO_ENDPOINT_CACHE = cast(devices.EndpointVolume, POINTER(IAudioEndpointVolume))
        else:
            iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            _AUDIO_ENDPOINT_CACHE = cast(iface, POINTER(IAudioEndpointVolume))
        return _AUDIO_ENDPOINT_CACHE
    except Exception as e:
        print(f"[THOR] Audio endpoint init failed: {e}")
        return None


class Thor(OdinModule):
    MODULE_NAME = "THOR"
    LAYER = "OUTPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        # Lazy-loaded app inventory from PowerShell `Get-StartApps`. Includes
        # UWP / Store apps (with AUMID), Chrome PWAs, classic Win32 apps,
        # URL shortcuts — everything Windows considers "an app" the user
        # can click to launch. Loaded in a background thread so boot isn't
        # delayed by the ~500-1000 ms PowerShell startup cost.
        self._startapps_cache: dict[str, str] | None = None  # lower(name) -> AppID
        self._startapps_lock = threading.Lock()
        threading.Thread(target=self._preload_startapps, daemon=True,
                         name="THOR-startapps").start()

    def _preload_startapps(self):
        apps = self._load_startapps()
        with self._startapps_lock:
            self._startapps_cache = apps
        if apps:
            uwp = sum(1 for v in apps.values() if "!" in v)
            print(f"[THOR] App inventory loaded: {len(apps)} apps ({uwp} UWP/Store + {len(apps)-uwp} classic).")

    @staticmethod
    def _load_startapps() -> dict[str, str]:
        """Run PowerShell `Get-StartApps` to enumerate every launchable app
        Windows knows about — UWP/Store apps with AUMIDs, Win32 classic apps,
        Chrome PWAs, URL shortcuts. Returns dict: lower(name) -> AppID."""
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return {}
            data = json.loads(result.stdout)
            out = {}
            for entry in data:
                name = (entry.get("Name") or "").strip()
                app_id = (entry.get("AppID") or "").strip()
                if name and app_id:
                    out[name.lower()] = app_id
            return out
        except Exception as e:
            print(f"[THOR] Get-StartApps failed: {e}")
            return {}

    def _startapps(self, timeout: float = 3.0) -> dict[str, str]:
        """Return the cached inventory. If the background loader is still
        running, wait up to `timeout` for it; otherwise force a sync load."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._startapps_lock:
                if self._startapps_cache is not None:
                    return self._startapps_cache
            time.sleep(0.05)
        apps = self._load_startapps()
        with self._startapps_lock:
            self._startapps_cache = apps
        return apps

    def _find_startapp(self, name: str) -> tuple[str, str] | None:
        """Match a spoken name to a Get-StartApps entry. Returns
        (canonical_name, app_id) or None. Tries: exact match → friendly
        alias → starts-with → substring; shortest match wins on ties."""
        apps = self._startapps()
        if not apps:
            return None
        name_l = name.lower().strip()
        # Aliases for common short names that don't match the canonical
        # Windows display name (Chrome → "Google Chrome", VS Code → "Visual
        # Studio Code"). Map spoken-form → canonical name to look up.
        ALIASES = {
            "chrome":  "google chrome",
            "edge":    "microsoft edge",
            "vscode":  "visual studio code",
            "vs code": "visual studio code",
            "code":    "visual studio code",
            "calc":    "calculator",
            "ytm":     "youtube music",
            "yt music": "youtube music",
            "ig":      "instagram",
            "fb":      "facebook",
            "wa":      "whatsapp",
        }
        for candidate in (name_l, ALIASES.get(name_l, "")):
            if candidate and candidate in apps:
                return candidate, apps[candidate]
        # starts-with / substring fallbacks — shortest canonical name wins
        starts = [(n, aid) for n, aid in apps.items() if n.startswith(name_l)]
        if starts:
            starts.sort(key=lambda t: len(t[0]))
            return starts[0]
        subs = [(n, aid) for n, aid in apps.items() if name_l in n]
        if subs:
            subs.sort(key=lambda t: len(t[0]))
            return subs[0]
        return None

    @staticmethod
    def _launch_app_id(name: str, app_id: str) -> None:
        """Launch a Get-StartApps entry by its AppID. Strategy depends on
        the format:
          - AUMID (contains '!'): UWP/Store app, launch via shell:appsFolder
          - URL: open in browser
          - File path: os.startfile directly
          - Plain name / GUID / Chrome PWA: find Start Menu .lnk and use that
            (the .lnk encodes the right launch arguments for PWAs etc.)
        """
        if "!" in app_id:
            # UWP / Store / Built-in Windows app
            subprocess.Popen(
                ["explorer.exe", f"shell:appsFolder\\{app_id}"],
                shell=False,
            )
            return
        if app_id.startswith(("http://", "https://")):
            webbrowser.open(app_id)
            return
        if (":\\" in app_id or app_id.startswith("/")) and not app_id.startswith("{"):
            try:
                os.startfile(app_id)
                return
            except Exception:
                pass
        # Chrome PWAs, GUID-prefixed shortcuts, plain Win32 names —
        # always find the .lnk in Start Menu and launch THAT. The .lnk
        # carries the correct args (e.g. Chrome PWA needs --app-id=...).
        shortcut = _find_start_menu_shortcut(name)
        if shortcut:
            os.startfile(shortcut)
            return
        # Last resort: let `start` resolve via App Paths registry
        subprocess.Popen(f'start "" "{app_id}"', shell=True,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "open_application",
                "description": "Open an application or program",
                "parameters": {
                    "app_name": {"type": "string", "description": "App name or executable path"}
                },
                "required": ["app_name"]
            },
            {
                "name": "run_command",
                "description": "Run a shell command and return output",
                "parameters": {
                    "command": {"type": "string", "description": "Shell command to execute"}
                },
                "required": ["command"]
            },
            {
                "name": "run_python",
                "description": "Execute a Python code snippet",
                "parameters": {
                    "code": {"type": "string", "description": "Python code to run"}
                },
                "required": ["code"]
            },
            {
                "name": "get_system_stats",
                "description": "Get current CPU, RAM, and disk usage",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "kill_process",
                "description": "Kill a running process by name",
                "parameters": {
                    "process_name": {"type": "string", "description": "Name of the process to kill"}
                },
                "required": ["process_name"]
            },
            {
                "name": "type_text",
                "description": "Type text into the currently focused window",
                "parameters": {
                    "text": {"type": "string", "description": "Text to type"}
                },
                "required": ["text"]
            },
            {
                "name": "press_key",
                "description": "Press a keyboard key or shortcut",
                "parameters": {
                    "key": {"type": "string", "description": "Key name (e.g. enter, ctrl+c, alt+tab)"}
                },
                "required": ["key"]
            },
            {
                "name": "shutdown",
                "description": "Shut down the computer",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "restart",
                "description": "Restart the computer",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_brightness",
                "description": "Set screen brightness on a laptop display (0-100 percent)",
                "parameters": {
                    "level": {"type": "integer", "description": "Brightness percent, 0 to 100"}
                },
                "required": ["level"],
                "internal_only": True
            },
            {
                "name": "get_brightness",
                "description": "Get the current screen brightness",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "volume_up",
                "description": "Raise the system volume",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "volume_down",
                "description": "Lower the system volume",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "mute",
                "description": "Toggle system mute",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_volume",
                "description": "Set the system volume to an absolute percent (0-100)",
                "parameters": {
                    "level": {"type": "integer", "description": "Volume percent, 0 to 100"}
                },
                "required": ["level"],
                "internal_only": True
            },
            {
                "name": "get_volume",
                "description": "Get the current system volume",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "sleep_computer",
                "description": "Put the computer to sleep",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "media_play_pause",
                "description": "Play or pause whatever media is currently active.",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "media_next",
                "description": "Skip to the next track or video",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "media_previous",
                "description": "Go back to the previous track or video",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "media_stop",
                "description": "Stop any currently playing media",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "show_desktop",
                "description": "Minimize all windows and show the desktop",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "open_application": self._open_app,
            "run_command": self._run_cmd,
            "run_python": self._run_python,
            "get_system_stats": self._sys_stats,
            "kill_process": self._kill_process,
            "type_text": self._type_text,
            "press_key": self._press_key,
            "shutdown": self._shutdown,
            "restart": self._restart,
            "set_brightness": self._set_brightness,
            "get_brightness": self._get_brightness,
            "volume_up": self._volume_up,
            "volume_down": self._volume_down,
            "mute": self._mute,
            "set_volume": self._set_volume,
            "get_volume": self._get_volume,
            "sleep_computer": self._sleep_computer,
            "media_play_pause": self._media_play_pause,
            "media_next": self._media_next,
            "media_previous": self._media_previous,
            "media_stop": self._media_stop,
            "show_desktop": self._show_desktop,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[THOR] Error: {e}"
        return f"[THOR] Unknown skill: {skill_name}"

    def _open_app(self, app_name: str = "") -> str:
        key = app_name.lower().strip()
        # Strip natural-language prefixes — "open MY gmail" / "open THE chrome".
        for prefix in ("my ", "the ", "a "):
            if key.startswith(prefix):
                key = key[len(prefix):]
                break
        # 1) URL-shaped strings → browser. (URLs are unambiguous.)
        if key.startswith(("http://", "https://")) or key.endswith((".com", ".org", ".net", ".io", ".dev")):
            webbrowser.open(app_name if "://" in app_name else f"https://{app_name}")
            return f"Opening {app_name}."
        # 2) Get-StartApps inventory — THE canonical Windows app list. Covers
        # UWP/Store apps (WhatsApp, Calculator, Camera), Chrome PWAs (YouTube
        # Music), Win32 classic apps (Chrome, Steam, Notepad, VS Code), URL
        # shortcuts. Single source of truth for "what's installed on this PC".
        match = self._find_startapp(key)
        if match:
            canonical, app_id = match
            try:
                self._launch_app_id(canonical, app_id)
                return f"Opening {canonical.title()}."
            except Exception as e:
                print(f"[THOR] Launch failed for {canonical} ({app_id}): {e}")
        # 3) Spoken-name → known exe alias (e.g. for things Get-StartApps
        # doesn't list — bare exe names like "cmd").
        target = _APP_ALIASES.get(key)
        if target:
            # Quote the target so multi-word values can't be shell-split into
            # bogus commands ('start the folder X' tries to run 'the' as a
            # command). DEVNULL on stderr suppresses 'cannot find the file X'
            # echoes that leaked into the console previously.
            subprocess.Popen(f'start "" "{target}"', shell=True,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
            return f"Opening {app_name}."
        # 4) Known web destinations → fallback for things without a local app.
        # (Gmail, GitHub, etc.)
        if key in _KNOWN_URLS:
            webbrowser.open(_KNOWN_URLS[key])
            return f"Opening {app_name} on the web."
        # 5) Last resort: let `start` resolve via App Paths registry.
        # Quoted + stderr-silenced — same reasons as above.
        subprocess.Popen(f'start "" "{app_name}"', shell=True,
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
        return f"Tried to open {app_name}. If nothing happened, I couldn't find it."

    def _set_brightness(self, level: int = 50) -> str:
        try:
            level = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return "Brightness level must be an integer 0 to 100."
        cmd = (
            f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods)."
            f"WmiSetBrightness(1, {level})"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return "Could not change brightness — likely a desktop monitor without WMI brightness."
        return f"Brightness set to {level} percent."

    def _get_brightness(self) -> str:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness"],
            capture_output=True, text=True, timeout=5
        )
        val = (result.stdout or "").strip().splitlines()
        val = val[0] if val else ""
        if val.isdigit():
            return f"Brightness is at {val} percent."
        return "Brightness reading unavailable on this display."

    def _volume_up(self) -> str:
        for _ in range(2):
            pyautogui.press("volumeup")
        return "Volume up."

    def _volume_down(self) -> str:
        for _ in range(2):
            pyautogui.press("volumedown")
        return "Volume down."

    def _mute(self) -> str:
        pyautogui.press("volumemute")
        return "Mute toggled."

    def _set_volume(self, level: int = 50) -> str:
        try:
            level = max(0, min(100, int(level)))
        except (TypeError, ValueError):
            return "Volume must be an integer 0 to 100."
        endpoint = _audio_endpoint()
        if endpoint is None:
            return "Absolute volume control unavailable — install pycaw."
        # SetMasterVolumeLevelScalar takes (float scalar 0.0–1.0, GUID* context).
        # Passing None for the GUID is the documented way to omit the context.
        # Unmute first — silently setting volume on a muted system is a common gotcha.
        try:
            if endpoint.GetMute():
                endpoint.SetMute(0, None)
        except Exception:
            pass
        endpoint.SetMasterVolumeLevelScalar(float(level) / 100.0, None)
        return f"Volume set to {level} percent."

    def _get_volume(self) -> str:
        endpoint = _audio_endpoint()
        if endpoint is None:
            return "Volume reading unavailable — install pycaw."
        scalar = endpoint.GetMasterVolumeLevelScalar()
        return f"Volume is at {int(round(scalar * 100))} percent."

    def _run_cmd(self, command: str = "") -> str:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=15
        )
        out = (result.stdout or result.stderr or "Done.").strip()[:500]
        return out

    def _run_python(self, code: str = "") -> str:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(code)
            tmp = f.name
        try:
            result = subprocess.run(
                ["python", tmp], capture_output=True, text=True, timeout=15
            )
            out = (result.stdout or result.stderr or "No output.").strip()[:500]
            return out
        except subprocess.TimeoutExpired:
            return "Code timed out after 15 seconds."
        finally:
            os.unlink(tmp)

    def _sys_stats(self) -> str:
        cpu = psutil.cpu_percent(interval=0.5)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage("C:\\")
        ram_used = ram.used // (1024 ** 2)
        ram_total = ram.total // (1024 ** 2)
        disk_free = disk.free // (1024 ** 3)
        return (f"CPU at {cpu}%. "
                f"RAM: {ram_used}MB used of {ram_total}MB. "
                f"Disk C: {disk_free}GB free.")

    def _kill_process(self, process_name: str = "") -> str:
        killed = []
        for proc in psutil.process_iter(["name"]):
            if process_name.lower() in proc.info["name"].lower():
                proc.kill()
                killed.append(proc.info["name"])
        if killed:
            return f"Killed: {', '.join(killed)}."
        return f"No process found matching '{process_name}'."

    def _type_text(self, text: str = "") -> str:
        pyautogui.typewrite(text, interval=0.03)
        return f"Typed: {text}"

    def _press_key(self, key: str = "") -> str:
        keys = key.lower().replace("+", " ").split()
        if len(keys) > 1:
            pyautogui.hotkey(*keys)
        else:
            pyautogui.press(keys[0])
        return f"Pressed: {key}"

    def _shutdown(self) -> str:
        os.system("shutdown /s /t 10")
        return "Shutting down in 10 seconds."

    def _restart(self) -> str:
        os.system("shutdown /r /t 10")
        return "Restarting in 10 seconds."

    def _sleep_computer(self) -> str:
        # rundll32 keeps the call non-blocking. /h would hibernate; we want
        # a normal sleep that wakes on lid/keypress.
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")
        return "Sleeping."

    def _media_play_pause(self) -> str:
        pyautogui.press("playpause")
        return "Toggled play/pause."

    def _media_next(self) -> str:
        pyautogui.press("nexttrack")
        return "Next track."

    def _media_previous(self) -> str:
        pyautogui.press("prevtrack")
        return "Previous track."

    def _media_stop(self) -> str:
        pyautogui.press("stop")
        return "Stopped."

    def _show_desktop(self) -> str:
        # Win+D — toggles between "all windows minimised" and "previous state".
        pyautogui.hotkey("win", "d")
        return "Desktop."
