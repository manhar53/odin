# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

ODIN is a personal voice-driven AI assistant (JARVIS/FRIDAY-style), running fully on the user's Windows laptop. Stack: faster-whisper (STT) → Ollama llama3.2:3b (LLM) → piper-tts (TTS) → pygame (audio).

## The three hard constraints

Every dependency, every API call, every design choice must satisfy all three:

1. **Full laptop access** — ODIN should be able to do anything the user can do on the machine. Missing capabilities (file ops, vision, window mgmt, etc.) are bugs, not "features we don't support."
2. **Local-first brain; cloud AI is a *learning tool*, not the runtime voice.** The wake → STT → think → TTS loop runs entirely on local Ollama. Cloud LLMs (Claude API, etc.) are allowed *only* via the SARASWATI module, which calls them for deep research / drafting / summarization workflows whose **results are cached to the Obsidian vault** so future questions on the same topic answer offline. The user must never feel they're talking to a cloud service through a local skin — that's the failure mode this constraint exists to prevent. Free public non-AI HTTP endpoints (wttr.in, DuckDuckGo, Wikipedia REST, ipinfo) are unrestricted. Paid API keys are fine if user-provided. Every cloud-touching skill must degrade gracefully when offline.
3. **Fully offline core** — wake, STT, local LLM thinking, TTS, vault search, every fast-route must work with no network. Cloud-research, weather, maps, email, web search may degrade gracefully but must not break the rest of the system.

## Naming convention (non-negotiable)

Every component — class, file, log tag, config key — is named after a mythological god, goddess, or hero. Never invent generic names. When adding a new module, pick a mythology name that fits its function and prefer underrepresented mythologies (Greek is heavy; Roman, Babylonian, Sumerian, Norse, Hindu are sparse).

| Role | Name | What it is |
|---|---|---|
| Assistant identity | **ODIN** | What the user talks to. Wake word "Hey ODIN". Internal names are never spoken to the user. |
| Brain | **GILGAMESH** (alias **GIL**) | LLM via Ollama, tool-calling loop, streams sentences to TTS |
| Bus | **MARDUK** | Silent orchestrator — all inter-module comms route through it |

The 29 functional modules live in 10 layers (`input/`, `memory/`, `intelligence/`, `output/`, `personality/`, `management/`, `environment/`, `protection/`, `utility/`, `idle/`), one Python file per module. SARASWATI (intelligence layer) is the bridge to cloud AI — the only module permitted to make a request that costs money or hits a non-public API.

## How a single user turn flows

```
HEIMDALL (mic loop) detects "Hey ODIN"
  → records audio until 1.5s silence
  → faster-whisper transcribes the command
  → THOTH stores the user message
  → GIL.think(messages, context, on_sentence=IRIS.speak)
       → streams from Ollama with all registered tools
       → on tool_calls: dispatches via MARDUK.dispatch(skill, args), feeds result back, loops
       → on plain content: flushes complete sentences to on_sentence as they arrive
  → IRIS speak queue plays piper-synthesized audio in a worker thread
  → THOTH stores assistant response
```

Non-obvious points:

- **GIL streams**. `client.chat(..., stream=True)`. As complete sentences land (regex flush in `_flush_sentences`), they go to `on_sentence` immediately so TTS starts before generation finishes. Total latency ≈ max(LLM, TTS), not LLM + TTS. Don't break this by collecting full text before speaking.
- **IRIS is non-blocking**. `iris.speak(text)` enqueues into a `queue.Queue`; a worker thread synthesizes and plays. Use `iris.speak_blocking(text)` only at shutdown when you must wait for audio to finish.
- **GIL pre-warms** Ollama in a daemon thread at startup so the first real query doesn't pay model-load cost. The pre-warm uses `num_predict=1`.
- **HEIMDALL is the only module not registered with MARDUK**. It owns the perception loop and calls `iris.speak`, `thoth.execute`, `gil.think` directly. Don't try to "fix" this by routing through MARDUK; the loop intentionally owns dispatch.

## Module contract

Every regular module subclasses `OdinModule` (in [core/marduk.py](core/marduk.py)) and exposes:

- `MODULE_NAME` (str, uppercase) — the registry key
- `LAYER` (str) — which layer it lives in
- `skills` property → list of skill dicts with `name`, `description`, `parameters`, `required`
- `execute(skill_name: str, args: dict) -> str` — dispatches by skill name, returns a string the LLM will read

`parameters` is a flat dict `{name: {"type": ..., "description": ...}}` — not a wrapped JSON schema. MARDUK wraps it into Ollama's tool format in `Marduk.get_all_tools()`.

To call another module from inside a module, use `self.send(to_module, action, **data)` which routes through MARDUK. Direct cross-module access is reserved for the perception loop and the boot sequence in [main.py](main.py), which cherry-picks IRIS/THOTH/MERLIN/LOKI from the registry to hand to HEIMDALL.

## Bootstrap order matters

[main.py](main.py) constructs MARDUK first, then every module, registers them, *then* constructs GILGAMESH (which needs the populated registry to call `marduk.get_all_tools()`), then resolves the modules HEIMDALL needs and starts the perception loop. Don't reorder.

## Common commands

```powershell
# Install Python deps
python -m pip install -r requirements.txt

# One-time: download the offline piper voice model (default en_US-ryan-high, ~115 MB)
python setup_voice.py
# or pick a different voice from rhasspy/piper-voices:
python setup_voice.py en_US-amy-medium

# Pull the local LLM (Ollama daemon must be running)
ollama pull llama3.2:3b

# Run ODIN
python main.py
```

Ollama must be reachable at `http://localhost:11434` (configurable in [config.yaml](config.yaml) under `gilgamesh.host`). Voice model path is `iris.piper_voice_path`; when missing, IRIS auto-falls-back to Windows SAPI via pyttsx3.

## Where state lives

- `data/memory/` — saved sessions (THOTH `save_session`, called by SELENE on goodnight)
- `data/knowledge/` — facts, preferences, reminders, ledger, alerts, macros, vault, health (one JSON per concern)
- `data/backups/` — OSIRIS snapshots
- `data/logs/odin.log` — MARDUK route + GIL tool-call log
- `data/voices/` — piper `.onnx` + `.json` voice files
- `data/screenshots/` — HORUS captures

The JSON-per-concern pattern is intentional. Don't introduce a database to "consolidate" them.

## Known incomplete work

Persistent project memory at `~/.claude/projects/c--odin/memory/` tracks the current audit. The original seven module gaps (HORUS OCR/llava, CHRONOS scheduler, CASSANDRA toasts, ENKIDU Fernet, ARJUN hosts blocking, HERMES semantic recall, OGMA argos-translate) are all closed and were re-verified live in the 2026-06-11 health review (`_review_health.py` + `_review_functional.py`).

Remaining gaps:

- ARJUN site blocking is implemented but only applies when ODIN runs as administrator (hosts-file edit); it degrades to a clear message otherwise
- SARASWATI code chain is Claude-first by design, but no `ANTHROPIC_API_KEY` is set — code skills currently degrade to Gemini/Groq
- Hindi STT is done (2026-06-11): HEIMDALL `stt_model: "small"` + `stt_languages: ["en", "hi"]`, per-utterance language detection in `_transcribe_command` (Urdu probability folds into Hindi). Hindi TTS is also done (2026-06-11): IRIS loads a second piper voice (`iris.piper_voice_hi_path`, hi_IN-pratham-medium) and routes per sentence — Devanagari → Hindi voice, everything else → default voice (`_voice_for`); GIL's `_SENTENCE_RE` treats danda (।/॥) as a sentence terminator so Hindi streams to TTS. Hindi works only on the piper engine; the SAPI fallback stays English unless a Hindi `sapi_voice` is configured
- OGMA offline packs installed: en↔es, en↔fr, en↔de, en↔hi; other languages fall back to MyMemory online

Fix these by extending the existing module — don't create a parallel module for the same concern.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **odin** (2064 symbols, 4608 relationships, 178 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/odin/context` | Codebase overview, check index freshness |
| `gitnexus://repo/odin/clusters` | All functional areas |
| `gitnexus://repo/odin/processes` | All execution flows |
| `gitnexus://repo/odin/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
