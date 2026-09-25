# ODIN — Omniscient Digital Intelligence Node

A voice-driven, local-first AI assistant for Windows, in the spirit of JARVIS/FRIDAY.
Say the wake word, speak a command, and ODIN hears, thinks and answers out loud —
with the core loop running entirely on your own machine, no cloud required.

```
wake word → faster-whisper (STT) → Ollama llama3.2:3b + tool calls → piper-tts (TTS)
```

## Highlights

- **Fully offline core** — wake word, speech-to-text, LLM reasoning, text-to-speech,
  and notes search all work with no network.
- **Real laptop control** — files and folders, apps and windows, system settings,
  screenshots + OCR, browser automation, reminders, expenses, focus mode and more,
  exposed to the LLM as ~30 tool-calling modules.
- **Streaming speech** — replies are spoken sentence-by-sentence while the model is
  still generating, so latency is roughly max(LLM, TTS) instead of the sum.
- **English + Hindi** — per-utterance language detection for STT and a second piper
  voice for Devanagari output.
- **Messaging & web** — WhatsApp Web, Gmail and Telegram gateways, web search,
  news, weather and maps; all degrade gracefully when offline.
- **Cloud AI as a learning tool, not a crutch** — optional cloud models (Gemini,
  Groq, NVIDIA NIM, OpenAI, Claude) are used only for deep research, and the results
  are cached to an Obsidian vault so the same question later answers offline.
- **Desktop UI** — ASGARD, a pywebview "throne-room" interface alongside the voice loop.

## Architecture

Every component is named after a mythological figure. The 30 modules live in 10
layers (`input/`, `memory/`, `intelligence/`, `output/`, `personality/`,
`management/`, `environment/`, `protection/`, `utility/`, `idle/`), one file each.

| Role | Module | What it does |
|---|---|---|
| Ears | **HEIMDALL** | Mic loop, wake word, recording, transcription |
| Brain | **GILGAMESH** (GIL) | Ollama tool-calling loop, streams sentences to TTS |
| Bus | **MARDUK** | Routes every inter-module call and registers tools |
| Voice | **IRIS** | Queued, non-blocking piper TTS (SAPI fallback) |
| Memory | **THOTH / HERMES / NABU** | Conversation store, semantic recall, Obsidian vault |
| Cloud bridge | **SARASWATI** | The only module allowed to call paid/cloud AI |

A turn flows: HEIMDALL detects the wake word → Whisper transcribes → GIL streams a
reply from Ollama, dispatching tool calls through MARDUK → IRIS speaks each sentence
as it lands. See [CLAUDE.md](CLAUDE.md) for the full design notes.

## Getting started

Requirements: Windows 10/11, Python 3.11, [Ollama](https://ollama.com).

```powershell
python -m pip install -r requirements.txt
ollama pull llama3.2:3b
python setup_voice.py          # downloads the offline piper voice (~115 MB)
python main.py
```

Optional helpers: `setup_ollama.py`, `setup_crawl4ai.py`, `setup_kiwix.py`
(offline Wikipedia), `setup_bitnet.py` (alternative BitNet brain),
`setup_autostart.py`, `train_wake_word.py`.

## Configuration & secrets

All settings live in [config.yaml](config.yaml). Every API key and account field
ships empty — set them as environment variables instead (e.g. `GEMINI_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `GMAIL_SENDER`, `GMAIL_APP_PASSWORD`). Features without a key
simply stay disabled.

Personal state — sessions, knowledge, logs, voice models, OAuth tokens and the
signed-in browser profile used for WhatsApp/Gmail — is written to `data/`, which is
gitignored and never leaves your machine.
