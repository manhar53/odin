"""Stage 6: functional dispatch of safe, read-only skills through MARDUK.
No app launches, no messages sent, no cloud-LLM calls, no audio playback.
"""
import sys, yaml, importlib, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

config = yaml.safe_load(open("config.yaml", encoding="utf-8"))
from core.marduk import Marduk
marduk = Marduk(config)

MODULES = [
    ("input.horus","Horus"),("input.aether","Aether"),
    ("memory.thoth","Thoth"),("memory.hermes","Hermes"),("memory.nabu","Nabu"),
    ("intelligence.athena","Athena"),("intelligence.prometheus","Prometheus"),
    ("intelligence.merlin","Merlin"),("intelligence.saraswati","Saraswati"),
    ("intelligence.akasha","Akasha"),("intelligence.chitra","Chitra"),
    ("intelligence.tyr","Tyr"),("intelligence.mimir","Mimir"),("intelligence.wayland","Wayland"),
    ("output.thor","Thor"),("output.mercury","Mercury"),
    ("personality.loki","Loki"),("personality.psyche","Psyche"),
    ("management.chronos","Chronos"),("management.midas","Midas"),("management.saint","Saint"),
    ("management.arjun","Arjun"),
    ("environment.fujin","Fujin"),("environment.sindbad","Sindbad"),
    ("protection.karn","Karn"),("protection.enkidu","Enkidu"),("protection.osiris","Osiris"),
    ("utility.hephaestus","Hephaestus"),("utility.cassandra","Cassandra"),
    ("utility.ogma","Ogma"),("utility.sherlock","Sherlock"),("utility.janus","Janus"),
    ("idle.selene","Selene"),("idle.vyasa","Vyasa"),
]
for mp, cn in MODULES:
    cls = getattr(importlib.import_module(mp), cn)
    marduk.register(cls(config))

# (skill, args, network?) — all read-only / side-effect-free
PROBES = [
    ("check_internet", {}, False),
    ("get_system_stats", {}, False),
    ("get_volume", {}, False),
    ("get_brightness", {}, False),
    ("get_time", {}, False),
    ("list_reminders", {}, False),
    ("list_memories", {}, False),
    ("get_last_exchange", {}, False),
    ("search_memory", {"query": "odin"}, False),
    ("vault_stats", {}, False),
    ("list_topics", {}, False),
    ("search_vault", {"query": "odin"}, False),
    ("get_context", {}, False),
    ("get_user_profile", {}, False),
    ("get_tone", {}, False),
    ("get_persona", {}, False),
    ("get_current_emotion", {}, False),
    ("get_spending_summary", {}, False),
    ("get_budget_status", {}, False),
    ("get_health_summary", {}, False),
    ("get_focus_status", {}, False),
    ("check_firewall", {}, False),
    ("list_startup_programs", {}, False),
    ("list_secret_labels", {}, False),
    ("list_backups", {}, False),
    ("list_macros", {}, False),
    ("list_alerts", {}, False),
    ("check_ollama", {}, False),
    ("check_disk_health", {}, False),
    ("list_directory", {"path": "C:/odin/data"}, False),
    ("get_path_info", {"path": "C:/odin/config.yaml"}, False),
    ("find_files", {"pattern": "*.yaml", "directory": "C:/odin"}, False),
    ("list_contact_aliases", {}, False),
    ("list_favorites", {}, False),
    ("get_idle_status", {}, False),
    ("simulation_report", {}, False),
    ("wayland_status", {}, False),
    ("detect_language", {"text": "bonjour mon ami"}, False),
    ("translate_to_english", {"text": "hola amigo"}, False),
    ("take_screenshot", {}, False),
    ("get_screen_text", {}, False),
    ("explain_last_failure", {}, False),
    # network (free public endpoints — graceful-degrade contract)
    ("get_weather", {"city": "Bangalore"}, True),
    ("get_my_location", {}, True),
    ("get_definition", {"term": "serendipity"}, True),
]

BAD_MARKERS = ("traceback", "exception", "not available", "no module named", "errno")
fails, flags = [], []
for skill, args, net in PROBES:
    t = time.perf_counter()
    try:
        out = marduk.dispatch(skill, args)
        dt = time.perf_counter() - t
        s = str(out).replace("\n", " ")[:140]
        low = s.lower()
        flag = any(m in low for m in BAD_MARKERS)
        tag = "FLAG" if flag else "ok"
        if flag:
            flags.append((skill, s))
        print(f"[{tag:>4}] {skill:24s} ({dt:5.1f}s) {s}")
    except Exception as e:
        dt = time.perf_counter() - t
        fails.append((skill, f"{type(e).__name__}: {e}"))
        print(f"[FAIL] {skill:24s} ({dt:5.1f}s) {type(e).__name__}: {e}")

print("\n" + "=" * 60)
print(f"RAISED: {len(fails)}  SUSPICIOUS OUTPUT: {len(flags)}")
for s, e in fails:
    print(f"  [X] {s}: {e}")
for s, e in flags:
    print(f"  [?] {s}: {e}")
