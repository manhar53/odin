# ODIN — Omniscient Digital Intelligence Node
# "The one who saw the deep"
# main.py — System bootstrap

import os
import sys
import time
import webbrowser
import yaml

# Boot timing — prints elapsed seconds at each major phase so slow boots are diagnosable.
_BOOT_T0 = time.perf_counter()

def _t(label: str):
    print(f"[BOOT {time.perf_counter() - _BOOT_T0:5.1f}s] {label}")

_t("interpreter ready, beginning imports")


def _hydrate_env_from_windows_registry():
    """On Windows, `setx FOO bar` writes to HKCU:\\Environment but the running
    shell never reloads — so a `python main.py` from a shell that predates
    the setx misses those vars. This helper reads HKCU:\\Environment for the
    known ODIN secret names and fills any that aren't already set, so users
    don't need to restart their PowerShell window after running setx.
    No-op on non-Windows."""
    if os.name != "nt":
        return
    # Only the specific names ODIN uses — never blanket-import the registry
    # (would pollute the process env with unrelated user variables).
    wanted = (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_IDS",
        "GMAIL_SENDER", "GMAIL_APP_PASSWORD",
        "HUGIN_AUTH_TOKEN",
        "GEMINI_API_KEY", "GROQ_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "GOOGLE_SEARCH_API_KEY", "GOOGLE_SEARCH_CX",
        "NEWS_API_KEY", "YOUTUBE_API_KEY", "FIRECRAWL_API_KEY",
        "CALENDARIFIC_API_KEY",
    )
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            loaded = []
            for name in wanted:
                if os.environ.get(name):
                    continue   # already present (current shell, OS, etc.)
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                    if value:
                        os.environ[name] = value
                        loaded.append(name)
                except OSError:
                    continue   # not set in registry — fine
        if loaded:
            print(f"[BOOT] Hydrated {len(loaded)} env var(s) from registry: {', '.join(loaded)}")
    except Exception as e:
        # Registry access could fail in sandboxed shells; non-fatal.
        print(f"[BOOT] env hydrate skipped: {e}")


_hydrate_env_from_windows_registry()


def _register_chrome_as_default():
    """Make Chrome the preferred browser for every webbrowser.open() call across
    the codebase (THOR's _open_app, APOLLO music links, MERCURY mail/whatsapp,
    SINDBAD maps, CHRONOS calendar). Falls through silently if Chrome isn't
    installed — webbrowser then resolves to the OS default.

    Must run before any module-level webbrowser.get() call so the registration
    is in place by the time skills fire."""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.exists(path):
            webbrowser.register(
                "chrome", None,
                webbrowser.BackgroundBrowser(path),
                preferred=True,
            )
            print(f"[BOOT] Chrome registered as default browser: {path}")
            return path
    print("[BOOT] Chrome not found in standard paths — using OS default browser.")
    return None


_register_chrome_as_default()

os.makedirs("data/memory", exist_ok=True)
os.makedirs("data/backups", exist_ok=True)
os.makedirs("data/logs", exist_ok=True)
os.makedirs("data/knowledge", exist_ok=True)

from core.marduk import Marduk
from core.gilgamesh import Gilgamesh

from input.heimdall import Heimdall
from input.horus import Horus
from input.aether import Aether
from input.narada import Narada
from input.hugin import Hugin

from memory.thoth import Thoth
from memory.hermes import Hermes
from memory.nabu import Nabu

from intelligence.athena import Athena
from intelligence.prometheus import Prometheus
from intelligence.merlin import Merlin
from intelligence.saraswati import Saraswati
from intelligence.seshat import Seshat
from intelligence.akasha import Akasha
from intelligence.chitra import Chitra
from intelligence.tyr import Tyr
from intelligence.mimir import Mimir
from intelligence.wayland import Wayland
from intelligence.brihaspati import Brihaspati

from output.iris import Iris
from output.thor import Thor
from output.mercury import Mercury
from output.argus import Argus
from output.asgard import Asgard
from output.hermod import Hermod

from personality.loki import Loki
from personality.psyche import Psyche
from personality.apollo import Apollo

from management.chronos import Chronos
from management.midas import Midas
from management.saint import Saint
from management.arjun import Arjun
from management.aurora import Aurora

from environment.fujin import Fujin
from environment.sindbad import Sindbad

from protection.karn import Karn
from protection.enkidu import Enkidu
from protection.osiris import Osiris

from utility.hephaestus import Hephaestus
from utility.cassandra import Cassandra
from utility.ogma import Ogma
from utility.sherlock import Sherlock
from utility.janus import Janus
from utility.vesta import Vesta
from utility.ganesh import Ganesh
from utility.stirling import Stirling

from idle.selene import Selene
from idle.vyasa import Vyasa

import threading

_t("all module imports done")


def load_config() -> dict:
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def boot():
    print("\n" + "=" * 50)
    print("  ODIN - Omniscient Digital Intelligence Node")
    print("  The one who saw the deep")
    print("=" * 50 + "\n")

    config = load_config()
    _t("config loaded")

    # ── MARDUK: orchestrator first ─────────────────
    marduk = Marduk(config)
    _t("MARDUK online")

    # ── Construct all modules — time the slow ones individually ────
    t = time.perf_counter()
    thoth = Thoth(config); hermes = Hermes(config); nabu = Nabu(config)
    _t(f"memory layer (THOTH/HERMES/NABU) constructed in {time.perf_counter()-t:.1f}s")

    t = time.perf_counter()
    iris = Iris(config)
    _t(f"IRIS constructed in {time.perf_counter()-t:.1f}s (piper voice + pygame.mixer.init)")

    t = time.perf_counter()
    rest = [
        Athena(config), Prometheus(config), Merlin(config),
        Saraswati(config), Seshat(config), Akasha(config), Chitra(config), Tyr(config), Mimir(config), Wayland(config),
        Brihaspati(config),
        Thor(config), Mercury(config), Argus(config), Asgard(config), Hermod(config),
        Loki(config), Psyche(config), Apollo(config),
        Chronos(config), Midas(config), Saint(config), Arjun(config), Aurora(config),
        Fujin(config), Sindbad(config),
        Karn(config), Enkidu(config), Osiris(config),
        Hephaestus(config), Cassandra(config), Ogma(config), Sherlock(config), Janus(config), Vesta(config), Ganesh(config), Stirling(config),
        Selene(config), Vyasa(config),
        Horus(config), Aether(config),
    ]
    _t(f"remaining {len(rest)} modules constructed in {time.perf_counter()-t:.1f}s")

    modules = [thoth, hermes, nabu, iris] + rest
    for module in modules:
        marduk.register(module)
    _t("all modules registered with MARDUK")

    # ── GILGAMESH: brain connects to MARDUK (this is the slow one — Ollama) ────
    gil = Gilgamesh(config, marduk)
    _t("GILGAMESH ready (Ollama warm)")

    # ── IRIS: voice output ─────────────────────────
    iris = marduk.get_module("IRIS")

    # ── THOTH: memory for conversation ────────────
    thoth = marduk.get_module("THOTH")

    # ── NABU: vault scribe (Obsidian sync) ────────
    nabu = marduk.get_module("NABU")

    # ── MERLIN: situational context ────────────────
    merlin = marduk.get_module("MERLIN")

    # ── LOKI: tone and personality ─────────────────
    loki = marduk.get_module("LOKI")

    # ── VESTA: tray + GUI + hotkey ─────────────────
    vesta = marduk.get_module("VESTA")

    print("\n[ODIN] All systems nominal.")
    # The spoken wake phrase depends on the wake ENGINE: openWakeWord uses
    # its model's phrase ("hey jarvis"), the whisper fallback uses
    # odin.wake_phrases. Print the one that's actually live.
    h_cfg = config.get("heimdall", {})
    if str(h_cfg.get("wake_engine", "openwakeword")).lower() == "openwakeword":
        wake = os.path.splitext(os.path.basename(
            str(h_cfg.get("oww_model", "hey_jarvis"))))[0].replace("_", " ")
    else:
        wake = (config.get("odin", {}).get("wake_phrases") or ["hey sage"])[0]
    print(f"[ODIN] Say '{wake}' to begin.\n")

    # ── HEIMDALL: perception loop — never sleeps. Loads Whisper models. ────
    t = time.perf_counter()
    heimdall = Heimdall(config, marduk, gil, thoth, merlin, loki, iris, vesta, nabu=nabu)
    _t(f"HEIMDALL ready in {time.perf_counter()-t:.1f}s (Whisper models loaded)")

    # ── VYASA: wire HEIMDALL so the self-test loop can pause while the user
    # is mid-conversation. Loop is OFF by default — opt in with voice
    # "start self-test" or directly via marduk.dispatch.
    vyasa = marduk.get_module("VYASA")
    if vyasa:
        vyasa.attach_heimdall(heimdall)

    # ── NARADA: text-mode gateway via Telegram. Stays dormant unless
    # narada.telegram_bot_token + allowed_user_ids are configured.
    try:
        narada = Narada(config, marduk, gil, thoth, loki=loki, nabu=nabu)
        narada.start()
    except Exception as e:
        print(f"[BOOT] NARADA failed to start: {e}")

    # ── HUGIN: universal inbound webhook on localhost. Stays dormant
    # unless hugin.auth_token is set OR hugin.allow_open_local is true.
    try:
        hugin = Hugin(config, marduk, gil, thoth, loki=loki, nabu=nabu)
        hugin.start()
    except Exception as e:
        print(f"[BOOT] HUGIN failed to start: {e}")

    _t(f"TOTAL BOOT TIME")

    # ── ASGARD: throne-room UI (main thread). When enabled, VESTA drops
    # to tray-only (no tk window) and ASGARD owns the main event loop.
    # When disabled, falls through to the legacy VESTA tk path.
    asgard = marduk.get_module("ASGARD")
    asgard_enabled = bool(asgard) and config.get("asgard", {}).get("enabled", True)

    if asgard_enabled:
        # Wire IRIS speak → speaking-state animation; HEIMDALL listening if it
        # exposes the listener hook (added separately).
        try:
            asgard.wire_iris(iris)
        except Exception as e:
            print(f"[BOOT] ASGARD wire_iris failed: {e}")
        try:
            asgard.wire_heimdall(heimdall)
        except Exception as e:
            print(f"[BOOT] ASGARD wire_heimdall failed: {e}")

        # ── HERMOD: corner avatar that takes over when an app is foreground.
        hermod = marduk.get_module("HERMOD")
        if hermod and config.get("hermod", {}).get("enabled", True):
            try:
                hermod.wire(asgard=asgard, iris=iris, gil=gil, loki=loki, thoth=thoth)
                hermod.wire_heimdall(heimdall)
                asgard.set_companion(hermod)
            except Exception as e:
                print(f"[BOOT] HERMOD wiring failed: {e}")

        if vesta:
            vesta.set_heimdall(heimdall)
            vesta.start_background()   # tray + push-to-talk only
        threading.Thread(target=heimdall.start, daemon=True).start()
        asgard.run()   # blocks main thread on the pywebview event loop
    elif vesta:
        vesta.set_heimdall(heimdall)
        threading.Thread(target=heimdall.start, daemon=True).start()
        vesta.run()  # Tk mainloop blocks the main thread; tray + hotkey run in daemons
    else:
        heimdall.start()


if __name__ == "__main__":
    boot()
