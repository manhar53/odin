# ODIN — Full Live-Test Protocol

Run `python main.py`, keep this file open, go top to bottom.
Wake phrase before every spoken command: **"Hey Jarvis"** (wait for the beep).
Legend: ⚡ instant fast-route · 🧠 local LLM (seconds) · ☁ needs cloud key ·
🌐 needs internet · ⚠ real side effect · 🐢 slow on CPU

## 0 · Boot checks (watch the console)
- [ ] ~40 `[MARDUK] Registered` lines, no tracebacks
- [ ] `[IRIS] Voice synthesizer ready (piper: en_GB-alan…)` **and** `Hindi voice ready (hi_IN-pratham…)`
- [ ] `[HEIMDALL] Wake engine: openWakeWord — say "hey jarvis"`
- [ ] World Tree paints over the desktop; HERMOD orb appears when an app is focused
- [ ] Tray icon present (World Tree: Active/Phantom/Hide entries)

## 1 · Wake & speech input
| Say / do | Expect |
|---|---|
| "Hey Jarvis" (nothing else) | beep, listening pulse on the heartwood, then idle |
| Tap **Alt**, speak immediately | push-to-talk works without wake word |
| **Double-clap** | clap-to-wake fires |
| "Hey Jarvis… समय क्या हुआ है?" | Hindi understood; reply spoken in the Hindi voice |
| While ODIN is talking, tap **Alt** and give a new command | old reply cut off, new command wins |

## 2 · Identity & small talk ⚡
"Who are you?" → All-Father intro · "What can you do?" → capability list ·
"Thanks man" / "Good morning" → in-character one-liners · "Tell me a joke"

## 3 · System control (THOR) ⚡
- "What time is it?" · "Uptime"
- "Set volume to 30" → then "Set volume to 70"
- "Set brightness to 60"
- "Take a screenshot" → file lands in data/screenshots
- "System stats" → CPU/RAM figures
- "Open Notepad" → ⚠ opens **and comes to the front** (World Tree yields); HERMOD orb takes over
- "Lock screen" → ⚠ locks Windows (do it last in this section)

## 4 · Perception (HORUS) — NEW
- "What's on my screen?" → OCR of the actual screen, spoken
- **Follow-up:** "What did it say about \<word you saw\>?" → answers from what it just read
- "Describe my screen" 🐢 (local llava; ~20-60s on CPU)
- Watch the **MIND panel** (top-left of the World Tree): it should now show `Screen OCR at …`

## 5 · Working memory & comprehension (SESHAT) — NEW
- Type or say: "learn https://en.wikipedia.org/wiki/Yggdrasil" 🌐☁ → distilled note, spoken gist
- "Summarize that" ⚡ → recap from the note
- "What did it say about Odin?" → targeted answer
- "So what do you think of it?" 🧠 → GIL answers **knowing what's in mind** (no tool call)
- Drop any PDF/Word file onto the World Tree (or HERMOD's ➕) → extracted + learned
- "Summarize report.pdf" (any doc in Desktop/Downloads/Documents, bare filename) → finds it itself

## 6 · One-shot chains — NEW
- "Find the latest \<some file you have\> and summarize it" → found → read → gist, all one command
- "What's the time and set volume to 40" → both execute, one breath
- "What's the weather and then write me a haiku about it" → weather instant, haiku from GIL **using** the weather
- Start a chain, **Alt-interrupt** mid-way, then say "Resume" → parked steps complete

## 7 · Memory & conversation continuity (THOTH/HERMES/PROMETHEUS)
- "Remember that my gym day is Tuesday" → then "What do you remember about me?"
- "Recall my favorite color" (old memory — should still be black)
- "What did we decide about the wake word?" — searches past conversations
- "What were we talking about?" → session summary
- "Where were we?" → last exchange replay

## 8 · Knowledge & web (AKASHA/BRIHASPATI/SARASWATI)
- "What's the weather?" ⚡🌐 · "Weather in Tokyo right now"
- "Bitcoin price" 🌐 · "Convert 100 dollars to rupees"
- "Who was Genghis Khan?" 🌐 → then "Tell me more"
- "What's happening in the world?" 🌐 — NEW, real headlines
- "What's the geopolitical situation in \<region\>?" ☁ — analysis grounded in today's news
- "Debate me: \<any proposition\>" ☁ — both sides + synthesis
- "Search my vault for cipher" ⚡ (offline)
- "Deep research \<topic\>" ☁ → cached to vault; ask about it again later **offline**

## 9 · Reminders & schedule (CHRONOS/CASSANDRA)
- "Remind me to stretch in 2 minutes" → ⚠ Windows toast + voice at T+2
- "List reminders" · "Cancel all reminders"
- "Remind me to drink water every hour" → recurring (then cancel it)

## 10 · Language (OGMA/IRIS)
- "Translate good morning to Spanish" ⚡ (offline pack)
- "Translate how are you to Hindi" → reply should be **spoken in the Hindi voice**

## 11 · Life tracking (MIDAS/SAINT)
- "Log expense 250 rupees for coffee" → "Spending summary" · "Budget status"
- "Log a glass of water" · "Health summary"

## 12 · Protection (KARN/ENKIDU/OSIRIS/ARJUN)
- "Back up my data" → snapshot in data/backups · "List backups"
- "Store a secret called test with value hello" → "Retrieve the secret test" → "Delete the secret test"
- "Check firewall" · "Focus mode on" (site blocking needs admin — expect the clear message otherwise)

## 13 · Messaging (MERCURY) — ⚠ ALL REAL SENDS
Only if you actually want them delivered:
- "Send a message to \<contact\> saying test from ODIN" → defaults to **WhatsApp** (no platform named) — NEW
- "Telegram \<chat\> saying hello" · "Draft an email to \<you\>@gmail.com saying test"
- "Send the same message to \<other contact\>" → resend-last

## 14 · The World Tree UI
- Hover canopy clusters / root island / castle / peaks → cards; click → dossier with **live skill lists**
- Press **/** → type "what time is it" → same pipeline as voice
- Drag a URL from the browser onto the tree → learned; onto **Mars** → focus hint = risks
- Drop a file on the **root island** → vault; a **.py on the high crown** → code explained ☁; **.xlsx on UTILITY** → sheet summary
- **MIND panel** pulses every time ODIN takes something in
- Click empty sky → phantom (click-through); **Ctrl+Alt+O** back; **Ctrl+Alt+H** hide/show
- HERMOD orb → click → chat thread; **➕** → pick a file → learned; ⛯ → World Tree returns

## 15 · Self-awareness (MIMIR/VYASA/SHERLOCK)
- "Run diagnostics" · "Weekly digest" (should report ZERO new failures now)
- "Start self-test" → let it run a few minutes → "Simulation report" → "Stop self-test"

## 16 · Shutdown
- "Goodnight" → SELENE saves the session, ODIN signs off, clean exit

---
**When something misfires:** note the exact phrase + what happened.
`data/logs/odin.log` has every route and dispatch. Bring failures to Fable.
