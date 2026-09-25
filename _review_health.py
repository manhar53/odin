"""ODIN full-system health review (non-interactive).

Stage 1: import every module file.
Stage 2: instantiate all regular modules + register with MARDUK.
Stage 3: validate every skill dict (name/description/parameters/required, flat param format).
Stage 4: verify Marduk.get_all_tools() builds a valid Ollama tool list (no dupes, JSON-serializable).
Stage 5: probe execute() with an unknown skill name (must return a string, not raise).
Heavy paths (Ollama, Whisper, pygame audio playback, Chrome) are NOT exercised.
"""
import os, sys, json, traceback, io

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.makedirs("data/memory", exist_ok=True)
os.makedirs("data/backups", exist_ok=True)
os.makedirs("data/logs", exist_ok=True)
os.makedirs("data/knowledge", exist_ok=True)

import yaml
with open("config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

failures = []
warnings = []

# ---------- Stage 1: imports ----------
MODULES = [
    ("core.marduk", "Marduk"), ("core.gilgamesh", "Gilgamesh"),
    ("input.heimdall", "Heimdall"), ("input.horus", "Horus"), ("input.aether", "Aether"),
    ("input.narada", "Narada"), ("input.hugin", "Hugin"),
    ("memory.thoth", "Thoth"), ("memory.hermes", "Hermes"), ("memory.nabu", "Nabu"),
    ("intelligence.athena", "Athena"), ("intelligence.prometheus", "Prometheus"),
    ("intelligence.merlin", "Merlin"), ("intelligence.saraswati", "Saraswati"),
    ("intelligence.akasha", "Akasha"), ("intelligence.chitra", "Chitra"),
    ("intelligence.tyr", "Tyr"), ("intelligence.mimir", "Mimir"), ("intelligence.wayland", "Wayland"),
    ("output.iris", "Iris"), ("output.thor", "Thor"), ("output.mercury", "Mercury"),
    ("output.argus", "Argus"), ("output.asgard", "Asgard"), ("output.hermod", "Hermod"),
    ("personality.loki", "Loki"), ("personality.psyche", "Psyche"), ("personality.apollo", "Apollo"),
    ("management.chronos", "Chronos"), ("management.midas", "Midas"), ("management.saint", "Saint"),
    ("management.arjun", "Arjun"), ("management.aurora", "Aurora"),
    ("environment.fujin", "Fujin"), ("environment.sindbad", "Sindbad"),
    ("protection.karn", "Karn"), ("protection.enkidu", "Enkidu"), ("protection.osiris", "Osiris"),
    ("utility.hephaestus", "Hephaestus"), ("utility.cassandra", "Cassandra"),
    ("utility.ogma", "Ogma"), ("utility.sherlock", "Sherlock"), ("utility.janus", "Janus"),
    ("utility.vesta", "Vesta"), ("utility.ganesh", "Ganesh"), ("utility.stirling", "Stirling"),
    ("idle.selene", "Selene"), ("idle.vyasa", "Vyasa"),
]

classes = {}
print("=== Stage 1: imports ===")
for mod_path, cls_name in MODULES:
    try:
        mod = __import__(mod_path, fromlist=[cls_name])
        classes[cls_name] = getattr(mod, cls_name)
        print(f"  [OK] {mod_path}.{cls_name}")
    except Exception as e:
        failures.append(f"IMPORT {mod_path}.{cls_name}: {type(e).__name__}: {e}")
        print(f"  [FAIL] {mod_path}.{cls_name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)

# ---------- Stage 2: instantiate + register ----------
print("\n=== Stage 2: instantiate + register ===")
from core.marduk import Marduk
marduk = Marduk(config)

# Same set main.py registers (everything except Heimdall/Narada/Hugin/Gilgamesh)
REGULAR = ["Thoth", "Hermes", "Nabu", "Iris",
           "Athena", "Prometheus", "Merlin", "Saraswati", "Akasha", "Chitra", "Tyr", "Mimir", "Wayland",
           "Thor", "Mercury", "Argus", "Asgard", "Hermod",
           "Loki", "Psyche", "Apollo",
           "Chronos", "Midas", "Saint", "Arjun", "Aurora",
           "Fujin", "Sindbad",
           "Karn", "Enkidu", "Osiris",
           "Hephaestus", "Cassandra", "Ogma", "Sherlock", "Janus", "Vesta", "Ganesh", "Stirling",
           "Selene", "Vyasa", "Horus", "Aether"]

instances = {}
for name in REGULAR:
    cls = classes.get(name)
    if cls is None:
        failures.append(f"INSTANTIATE {name}: class missing (import failed)")
        continue
    try:
        inst = cls(config)
        instances[name] = inst
        marduk.register(inst)
        print(f"  [OK] {name} -> MODULE_NAME={inst.MODULE_NAME} LAYER={getattr(inst, 'LAYER', '?')}")
    except Exception as e:
        failures.append(f"INSTANTIATE {name}: {type(e).__name__}: {e}")
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)

# ---------- Stage 3: skill schema validation ----------
print("\n=== Stage 3: skill schemas ===")
seen_skill_names = {}
total_skills = 0
for name, inst in instances.items():
    try:
        skills = inst.skills
    except Exception as e:
        failures.append(f"SKILLS {name}: property raised {type(e).__name__}: {e}")
        continue
    if not isinstance(skills, list):
        failures.append(f"SKILLS {name}: not a list ({type(skills).__name__})")
        continue
    for sk in skills:
        total_skills += 1
        sname = sk.get("name")
        if not sname:
            failures.append(f"SKILLS {name}: skill missing 'name': {sk}")
            continue
        if sname in seen_skill_names:
            failures.append(f"SKILLS duplicate skill name '{sname}' in {name} and {seen_skill_names[sname]}")
        seen_skill_names[sname] = name
        if not sk.get("description"):
            warnings.append(f"SKILLS {name}.{sname}: missing description")
        params = sk.get("parameters", {})
        if not isinstance(params, dict):
            failures.append(f"SKILLS {name}.{sname}: parameters not a dict")
            continue
        if "type" in params and "properties" in params:
            failures.append(f"SKILLS {name}.{sname}: parameters looks like a wrapped JSON schema (should be flat)")
        for pname, pdef in params.items():
            if not isinstance(pdef, dict) or "type" not in pdef:
                failures.append(f"SKILLS {name}.{sname}.{pname}: param def missing 'type': {pdef}")
        req = sk.get("required", [])
        for r in req:
            if r not in params:
                failures.append(f"SKILLS {name}.{sname}: required param '{r}' not in parameters")
print(f"  {total_skills} skills across {len(instances)} modules; {len(seen_skill_names)} unique names")

# ---------- Stage 4: tool list build ----------
print("\n=== Stage 4: Marduk.get_all_tools() ===")
try:
    tools = marduk.get_all_tools()
    json.dumps(tools)  # must be serializable for Ollama
    # internal_only skills are deliberately hidden from the LLM (token diet)
    visible = sum(1 for inst in instances.values() for sk in inst.skills if not sk.get("internal_only"))
    print(f"  [OK] {len(tools)} tools ({total_skills - visible} internal_only hidden), JSON-serializable")
    if len(tools) != visible:
        warnings.append(f"TOOLS count {len(tools)} != non-internal skill count {visible}")
except Exception as e:
    failures.append(f"TOOLS get_all_tools: {type(e).__name__}: {e}")
    traceback.print_exc(limit=3)

# ---------- Stage 5: execute() unknown-skill probe ----------
print("\n=== Stage 5: execute() unknown-skill probe ===")
for name, inst in instances.items():
    try:
        out = inst.execute("__no_such_skill__", {})
        if not isinstance(out, str):
            failures.append(f"EXECUTE {name}: returned {type(out).__name__}, not str")
    except Exception as e:
        failures.append(f"EXECUTE {name}: raised {type(e).__name__}: {e}")
print("  probe complete")

# ---------- Summary ----------
print("\n" + "=" * 60)
print(f"FAILURES: {len(failures)}")
for f_ in failures:
    print(f"  [X] {f_}")
print(f"WARNINGS: {len(warnings)}")
for w in warnings:
    print(f"  [!] {w}")
print("VERDICT:", "HEALTHY" if not failures else "NEEDS FIXES")
