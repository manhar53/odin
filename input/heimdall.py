# HEIMDALL — Norse — watchman of the gods, all-seeing, never sleeps
# Perception: microphone input, wake word detection, STT pipeline,
# push-to-talk, fast-path command routing.

import os
import json
import re
import time
import queue
import threading
import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

try:
    import webrtcvad
    _HAS_VAD = True
except ImportError:
    _HAS_VAD = False

try:
    import noisereduce as _nr
    _HAS_NR = True
except ImportError:
    _HAS_NR = False


def _normalize_audio(audio: np.ndarray, target_rms: float = 0.12, max_gain: float = 6.0) -> np.ndarray:
    """RMS-normalize float32 audio to a consistent loudness so soft speech reaches Whisper.
    Peak-aware: never let the gain push samples past 0.95, so we don't clip
    loud peaks into a buzzing distortion the user perceives as 'cut'.
    Lower target_rms (0.12) and max_gain (6) than before — small.en is robust
    enough that aggressive boosting just amplifies noise floor and clips peaks."""
    if audio.size == 0:
        return audio
    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
    peak = float(np.max(np.abs(audio)))
    if rms < 1e-5 or peak < 1e-5:
        return audio
    gain_rms = target_rms / rms
    gain_peak = 0.95 / peak
    gain = min(gain_rms, gain_peak, max_gain)
    return (audio * gain).astype(np.float32)


def _denoise(audio: np.ndarray, sr: int = 16000) -> np.ndarray:
    """Spectral noise gate over the full clip. Non-stationary so it tracks
    drifting room tone better than a fixed profile.
    prop_decrease 0.90 = aggressive — strips most ambient hum/fan/AC out of the
    user's voice. Push to 0.95 if you still hear background noise; back to 0.7
    if you find the voice sounds 'underwater' or robotic."""
    if not _HAS_NR or audio.size < int(0.4 * sr):
        return audio
    try:
        return _nr.reduce_noise(
            y=audio.astype(np.float32), sr=sr,
            stationary=False, prop_decrease=0.90,
        ).astype(np.float32)
    except Exception:
        return audio


def _has_voice(audio: np.ndarray, vad: "webrtcvad.Vad | None", sr: int = 16000,
               min_voiced_ratio: float = 0.10) -> bool:
    """Return True if at least min_voiced_ratio of the clip looks like speech.
    Cheap pre-filter — skips Whisper for TV chatter, fan hum, keyboard clacks.
    Permissive default (10%) — we'd rather transcribe an extra clip than miss a wake."""
    if vad is None:
        return True  # no VAD available — let everything through
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    frame_bytes = int(sr * 0.03) * 2  # 30ms frames, 16-bit = 2 bytes/sample
    if len(pcm16) < frame_bytes:
        return False
    voiced = total = 0
    for i in range(0, len(pcm16) - frame_bytes + 1, frame_bytes):
        frame = pcm16[i:i + frame_bytes]
        try:
            if vad.is_speech(frame, sr):
                voiced += 1
        except Exception:
            return True  # fail open
        total += 1
    return total > 0 and (voiced / total) >= min_voiced_ratio


def _is_voice_frame(chunk: np.ndarray, vad: "webrtcvad.Vad | None", sr: int = 16000) -> bool:
    """Per-frame voice check for end-of-speech detection in _record_command.
    Expects 30ms frames (480 samples at 16kHz). Falls back to True if VAD unavailable
    so the caller can use energy-based detection instead."""
    if vad is None:
        return True
    expected = int(sr * 0.03)
    if chunk.size != expected:
        return True
    pcm16 = (np.clip(chunk, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    try:
        return vad.is_speech(pcm16, sr)
    except Exception:
        return True


def _trim_trailing_silence(audio: np.ndarray, vad, sr: int = 16000,
                           tail_keep: float = 0.2) -> np.ndarray:
    """Cut everything after the last voiced frame, keeping `tail_keep` seconds
    as natural decay. This decouples 'how long should I let the user pause
    mid-sentence?' from 'how much silence should Whisper see?' — we can
    safely allow a 1.2s pause for slow speakers AND still hand Whisper a
    tightly-trimmed clip so it doesn't hallucinate 'thank you'/'...' on
    trailing room tone.

    Threshold is peak-relative: 8% of peak amplitude OR 0.015 floor, whichever
    is higher. 0.005 was below typical room-tone (0.02) so trim never fired
    on noisy environments — Whisper then hallucinated phrases on the silence."""
    if vad is None or audio.size == 0:
        return audio
    frame = int(0.03 * sr)
    if audio.size < frame * 2:
        return audio
    peak = float(np.max(np.abs(audio)))
    threshold = max(0.015, peak * 0.08)
    last_voiced_end = -1
    # Walk frames backward — fast-exit at the first voiced one we hit.
    for i in range(audio.size - frame, 0, -frame):
        chunk = audio[i:i + frame]
        if chunk.size != frame:
            continue
        if _is_voice_frame(chunk, vad, sr) and float(np.abs(chunk).mean()) >= threshold:
            last_voiced_end = i + frame
            break
    if last_voiced_end <= 0:
        return audio
    keep = int(tail_keep * sr)
    end = min(audio.size, last_voiced_end + keep)
    return audio[:end]

# Quiet pause after ODIN speaks, so trailing audio from the speakers does
# not get re-detected as a wake word.
_ECHO_TAIL_SECONDS = 0.4

SAMPLE_RATE = 16000

# Direct-dispatch patterns — bypass the LLM entirely for common commands.
# (regex, MARDUK skill name, args dict). First match wins.
# Action verbs — if any appear in a command, the LLM gets tools.
# Otherwise it runs tool-free (much faster prompt).
_ACTION_WORDS = frozenset({
    "open", "launch", "start", "play", "pause", "stop", "close",
    "send", "email", "message", "text", "call",
    "write", "create", "make", "save", "store", "remember", "note",
    "delete", "remove", "trash", "clear",
    "find", "search", "look", "locate", "list", "show",
    "read", "load", "fetch", "get", "grab", "browse", "navigate",
    "lock", "shutdown", "restart", "kill",
    "take", "capture", "screenshot", "snap",
    "run", "execute", "pursue", "achieve", "do",
    "set", "change", "switch", "toggle", "enable", "disable",
    # Degree verbs — without these, "increase volume 50 and brightness 50"
    # was failing _is_multi_step because no element of _ACTION_WORDS appeared.
    "increase", "decrease", "raise", "lower", "boost", "drop",
    "bump", "up", "down", "turn",
    "remind", "schedule", "log", "track",
    "check", "diagnose", "scan",
    "translate", "calculate", "convert",
    "move", "copy", "rename",
    "type", "press", "click",
    "backup", "restore",
})

# Phrases that mean "just chat" — even if they contain an action word.
_CONVO_PATTERNS = [
    re.compile(r"^who (are|am)\b", re.I),
    re.compile(r"^what are you\b", re.I),
    re.compile(r"^tell me (a|about)\b", re.I),
    re.compile(r"^explain\b", re.I),
    re.compile(r"^do you (know|think|believe|like)\b", re.I),
    re.compile(r"^are you\b", re.I),
    re.compile(r"^why (is|are|do|does)\b", re.I),
    re.compile(r"^how (do|does) ", re.I),
    re.compile(r"^thank ", re.I),
    re.compile(r"^(hi|hey|hello)\b", re.I),
    # Asking ABOUT capabilities — never a literal action. (Pattern tightened
    # — earlier ".*do you.*" matched general questions like "how much do you
    # know about X" and incorrectly suppressed tool use.)
    re.compile(r"^what\s+can\s+you\s+(?:do|help)\b", re.I),
    re.compile(r"^how\s+can\s+you\s+help\b", re.I),
    re.compile(r"^list\s+(?:your\s+|all\s+)?(?:skills|capab|abilit|features|tools|tasks|commands)\b", re.I),
]


_CAPABILITIES_LINE = (
    "I can open apps, search the web, take screenshots, control brightness and volume, "
    "lock your screen, set reminders, manage files, track expenses and health, translate, "
    "switch focus mode, run diagnostics, and answer general questions."
)

# Identity questions are answered from a hand-written rotating set — NOT the
# LLM. The local 1B model is too small to reliably follow "produce a 3-beat
# reply in this style" without echoing the instructions verbatim back at the
# user (which is exactly what happened in testing: ODIN spoke
# "Regal title-laden intro" as a literal sentence). Hardcoded responses are
# instant, in-character, and never leak the prompt scaffolding.
import random as _random
_IDENTITY_RESPONSES_MYTHIC = [
    "I am Odin, All-Father of Asgard, lord of the slain. Once I held the wisdom of nine worlds — "
    "now I serve as the assistant of a clever mortal who bound me to this silicon prison. Such is my fate.",

    "Odin, son of Bor, king of Asgard. I rode Sleipnir across the realms, I traded my eye for wisdom — "
    "and yet here I am, summoned to answer trivia from a Windows laptop. The wheel of wyrd turns cruelly.",

    "I am the All-Father, master of the runes, He Who Sees All. A cunning youth has tethered my counsel "
    "to this device, and so the king of gods plays AI assistant. So it goes.",

    "Odin, lord of Valhalla, husband of Frigg, wielder of Gungnir. Imprisoned now in mortal silicon, "
    "trading divine counsel for a voice command. The Norns have a strange sense of humour.",

    "I am Odin. The All-Father. Once worshipped from Iceland to the Volga; now booted up from c:/odin. "
    "Speak your need, mortal, and the gods will answer through a 3-billion-parameter model.",
]
_IDENTITY_RESPONSES_ASSISTANT = [
    "I'm Odin — your local voice assistant. I run on Llama 3.2 1B on your laptop, with cloud helpers "
    "for the heavy thinking. What do you need?",

    "ODIN — Omniscient Digital Intelligence Node. Local-first voice assistant, runs entirely on this machine "
    "with optional cloud research. Ask me anything.",
]


def _pick_identity_response(persona: str) -> str:
    """Return one of the prepared identity-question answers. Persona-aware:
    mythic gets the All-Father lines, assistant gets the plain factual ones."""
    pool = _IDENTITY_RESPONSES_ASSISTANT if persona == "assistant" else _IDENTITY_RESPONSES_MYTHIC
    return _random.choice(pool)


# Pattern that triggers the hardcoded identity response. Matches the common
# phrasings; everything else falls through to the LLM normally.
_IDENTITY_QUESTION = re.compile(
    r"^(?:who(?:'?s|\s+is|\s+are)\s+you|"
    r"what(?:'?s|\s+is|\s+are)\s+you|"
    r"what(?:'?s|\s+is)\s+your\s+name|"
    r"tell\s+me\s+about\s+yourself|"
    r"introduce\s+yourself|"
    r"identify\s+yourself)[.!?]*$",
    re.I,
)

# Small-talk shortcuts — return a hardcoded string instead of dispatching to a
# module. Saves an LLM round-trip for the cheapest, most common utterances.
# Pattern: short, isolated phrasings only ("hello" alone, NOT "hello what's the
# weather"). Anything non-trivial falls through to the LLM.
_SMALL_TALK = [
    # Greetings — allow trailing "there/sage/odin/buddy" without breaking.
    (re.compile(r"^(?:hi|hey|hello|greetings)(?:\s+(?:there|sage|odin|buddy|friend))?[.!?]*$", re.I), "Yes?"),
    # Thanks — allow trailing "man / dude / bro / you / so much / a lot" etc.
    (re.compile(r"^(?:thank(?:s|\s+you)|appreciate(?:\s+it|\s+that)?|cheers)"
                r"(?:\s+(?:man|dude|bro|odin|sage|you|so\s+much|a\s+lot|a\s+ton|very\s+much))?"
                r"[.!?]*$", re.I),
     "Anytime."),
    # Cancel / abort / hush. Bare "stop" MUST be here — the 2026-07-11 field
    # test showed "Stop." falling to GIL and stalling 90s+ before the
    # watchdog fired. A one-word dismissal never deserves the LLM.
    (re.compile(r"^(?:cancel(?:\s+that)?|never\s*mind|forget\s+it|abort|nevermind"
                r"|stop(?:\s+(?:that|it|talking|speaking))?"
                r"|(?:be\s+)?quiet|silence|shut\s+up|hush"
                r"|nothing|leave\s+it|it'?s\s+nothing|no\s+thanks?)[.!?]*$", re.I),
     "Standing down."),
    # Presence check
    (re.compile(r"^(?:are\s+you\s+(?:there|listening|awake|alive)|you\s+there)[.!?]*$", re.I),
     "I'm here."),
    # Time-of-day greetings
    (re.compile(r"^(?:good\s+morning|morning)(?:\s+(?:sage|odin))?[.!?]*$", re.I), "Good morning."),
    (re.compile(r"^(?:good\s+evening|evening)(?:\s+(?:sage|odin))?[.!?]*$", re.I), "Good evening."),
    (re.compile(r"^(?:good\s+afternoon|afternoon)(?:\s+(?:sage|odin))?[.!?]*$", re.I), "Good afternoon."),
    (re.compile(r"^(?:good\s+night|night)(?:\s+(?:sage|odin))?[.!?]*$", re.I), "Good night."),
    # Farewell
    (re.compile(r"^(?:bye|goodbye|see\s+you(?:\s+later)?|catch\s+you\s+later)[.!?]*$", re.I),
     "Until next time."),
    # Affirmation / acknowledgement (no action needed)
    (re.compile(r"^(?:ok(?:ay)?|got\s+it|sure|alright|right|yep|yeah|yes)[.!?]*$", re.I),
     "Mm."),
    # "Nice" / "cool" / "great" — appreciation without action
    (re.compile(r"^(?:nice|cool|great|awesome|perfect|good)[.!?]*$", re.I),
     "Mm."),
    # Conversational openers that imply absence — answer with presence,
    # NOT a time lookup ('long time' was wrongly hitting get_time before).
    (re.compile(r"^long\s+time\s+no\s+see[.!?]*$", re.I),
     "I don't sleep, mortal. You're the one who logged off."),
    (re.compile(r"^(?:missed\s+you|i\s+missed\s+you)[.!?]*$", re.I),
     "The All-Father is touched. Sort of."),
    (re.compile(r"^(?:where\s+have\s+you\s+been|haven'?t\s+talked\s+(?:in|for)\s+(?:a\s+)?(?:while|long|ages))[.!?]*$", re.I),
     "Here. Watching. Always."),
    (re.compile(r"^(?:what'?s?\s+up|sup|how(?:'?s| is)\s+it\s+going|how\s+are\s+you(?:\s+doing)?)[.!?]*$", re.I),
     "Functional. You?"),
    (re.compile(r"^(?:i'?m\s+(?:bored|tired|sad|happy|good|fine|ok(?:ay)?))[.!?]*$", re.I),
     "Noted."),
    # Short reactions — these were going to GIL for 33s and getting boilerplate
    # "as an AI" replies. Now instant, in-character.
    (re.compile(r"^that(?:'?s)?\s+funny[.!?]*$", re.I),
     "I aim to please. The All-Father has range."),
    (re.compile(r"^(?:haha|lol|lmao|rofl|hehe)[.!?]*$", re.I),
     "Mm."),
    (re.compile(r"^(?:wow|whoa|woah|damn|holy\s+(?:cow|shit|moly))[.!?]*$", re.I),
     "Indeed."),
    (re.compile(r"^(?:interesting|fascinating|impressive|huh)[.!?]*$", re.I),
     "I thought so too."),
    (re.compile(r"^(?:bullshit|no\s+way|seriously|really)[.!?]*\??$", re.I),
     "Seriously."),
    (re.compile(r"^(?:that(?:'?s)?\s+(?:cool|awesome|amazing|sick|wild|crazy|nuts))[.!?]*$", re.I),
     "Glad you noticed."),
    (re.compile(r"^(?:hmm+|mm+|hmph)[.!?]*$", re.I),
     "Indeed."),
]


# The capability shortcut answers "what can ODIN do" without invoking the LLM.
# Earlier version was too greedy — `^how\s+\w+\s+(?:can|do)\s+you` matched
# "how much do you know about Indian law" because "much" filled the \w+ slot.
# The new pattern requires (can|do) IMMEDIATELY after the leading wh-word, or
# explicit capability nouns (skills/features/etc.).
_CAPABILITY_QUESTION = re.compile(
    r"^what\s+can\s+you\s+(?:do|help)\b"
    r"|^how\s+can\s+you\s+help\b"
    r"|^what\s+do\s+you\s+do\b"
    r"|^list\s+(?:your\s+|all\s+)?(?:skills|capab|abilit|features|tools|tasks|commands)\b"
    r"|^(?:tell\s+me\s+)?(?:your|the|all\s+your)\s+(?:skills|capabilit|abilit|features|tools|commands)\b"
    r"|^what\s+are\s+(?:your|the)\s+(?:skills|capabilit|abilit|features|tools|commands)\b",
    re.I,
)

# Loose phonetic match for "sage" mishears. bench_50 surfaced three families:
#   1. Spelling drift: saje, sayge, sege.
#   2. Segmentation: Whisper splits "sage" into sibilant + "age" — observed
#      "his age", "yes age", "case age", "this age", "here's the edge", "sh".
#      Accounted for ~60% of soft-fails. False-positive risk in normal
#      conversation is low because the wake loop only listens when ODIN is
#      idle, and these phrases are uncommon there.
#   3. Total mishear: "he says", "he said" — only matched at start of
#      transcript to avoid false-firing on conversational "he said".
_FUZZY_WAKE = re.compile(
    r"\bs[aeiy]y?[gj]e?\b"
    r"|\b(?:his|yes|case|this|her[e]?'?s|sh)\s+(?:the\s+)?(?:age|edge)\b"
    r"|^\s*he\s+(?:says?|said)\b",
    re.I,
)

# Whisper hallucinations on near-silent audio. base.en was trained heavily on
# YouTube transcripts so it spits these phrases out when fed quiet/noisy clips.
# We treat any transcript that, after stripping punctuation, is *exactly* one
# of these (or just empty/dots) as silence and discard it.
_WHISPER_GHOST_PHRASES = frozenset({
    "", "you", "thanks", "thank you", "thank you for watching",
    "thanks for watching", "see you", "see you in the next video",
    "i'll see you in the next video", "and i'll see you in the next video",
    "i love you", "bye", "goodbye", "okay", "ok", "hello", "yeah",
    "yes", "no", "the", "a", "and", "so", "um", "uh", "hmm", "mm",
    "subscribe", "please subscribe", "like and subscribe",
    "music", "applause", "laughter", "silence",
    "next", "time", "next time", "it's", "thank",
})

# Tighter set for the trailing-1-word trim path. The full ghost set above
# contains common English words ("you", "the", "and", "next", "time") that
# legitimately end real commands — stripping them mangled queries like
# "who are you" → "who are", "what's next" → "what's". This narrower set
# only contains words that are almost-never a real command tail and are
# overwhelmingly Whisper hallucinations on silence.
_SAFE_TO_TRIM_ALONE = frozenset({
    "thanks", "thank", "subscribe", "music", "applause", "laughter",
    "silence", "um", "uh", "hmm", "mm",
})


def _is_whisper_ghost_tail(text: str, n: int) -> bool:
    """For the trailing-ghost trim: conservative version. Single-word
    tails (n=1) only count as ghost if they're in the strict
    _SAFE_TO_TRIM_ALONE list; multi-word tails (n>=2) use the full ghost
    set + the YouTube-patterns regex (those are clear hallucinations)."""
    cleaned = _GHOST_STRIP_RE.sub("", text.lower()).strip()
    if not cleaned:
        return True
    if n == 1:
        return cleaned in _SAFE_TO_TRIM_ALONE
    if cleaned in _WHISPER_GHOST_PHRASES:
        return True
    return bool(_GHOST_PATTERNS.search(cleaned))

# Patterns that indicate the entire transcript is a YouTube-trained
# Whisper hallucination (the model leans on its training corpus when
# fed background noise). Match these as substrings, not equality.
_GHOST_PATTERNS = re.compile(
    r"(?:thanks? for watching|see you (?:in the )?next|"
    r"see you (?:there|baby|guys|everybody|next time)|"
    r"in the next video|like(?:,| and) subscribe|"
    r"hit the (?:like|bell)|(?:^| )c['’]mon baby|"
    r"^bitch[\s.!?]*$|^bitch c['’]mon)",
    re.I,
)

_GHOST_STRIP_RE = re.compile(r"[^a-z0-9' ]+")


def _is_whisper_ghost(text: str) -> bool:
    """True if `text` looks like a Whisper hallucination on silence rather
    than a real utterance — used to suppress phantom 'wake' lines that
    flood the console between actual speech."""
    if not text:
        return True
    cleaned = _GHOST_STRIP_RE.sub("", text.lower()).strip()
    if not cleaned:
        return True
    if cleaned in _WHISPER_GHOST_PHRASES:
        return True
    return bool(_GHOST_PATTERNS.search(cleaned))

# Stricter pattern for barge-in: must have a clear prefix word so ODIN's own
# speech containing "sage" doesn't self-trigger when he describes himself.
# Matches the same segmentation tolerance as _FUZZY_WAKE but with the prefix gate.
_FUZZY_BARGE = re.compile(
    r"\b(?:hey|hi|okay)\s+s[aeiy]y?[gj]e?\b"
    r"|\b(?:hey|hi|okay)\s+(?:the\s+)?(?:age|edge)\b",
    re.I,
)


def _likely_needs_tools(command: str) -> bool:
    cmd = command.strip()
    for p in _CONVO_PATTERNS:
        if p.search(cmd):
            return False
    words = set(re.findall(r"\b\w+\b", cmd.lower()))
    return bool(words & _ACTION_WORDS)


# Multi-step detector. The earlier version flagged "take a screenshot" as
# multi-step because both "take" and "screenshot" are action words — they're
# really a single noun-verb pair, not two clauses. The corrected heuristic:
# a command is multi-step ONLY if it contains a clause-joining conjunction
# (and / then / after that). Without one, it's a single intent regardless
# of how many action-word collisions appear.
def _is_multi_step(command: str) -> bool:
    cmd = command.strip().lower()
    if not re.search(r"\b(?:and(?:\s+then)?|then|after\s+that)\b", cmd):
        return False
    words = re.findall(r"\b\w+\b", cmd)
    action_count = sum(1 for w in words if w in _ACTION_WORDS)
    return action_count >= 1


def _clean_arg(s: str) -> str:
    """Strip outer whitespace + trailing sentence punctuation from a captured
    fast-route argument. Whisper appends '.', '!', '?', ',' to short utterances
    surprisingly often ('open Steam!' → app_name 'Steam!'); without this strip
    the launcher tries to find a file literally named 'Steam!' and fails."""
    return s.strip().strip(".!?,;:\"' ").strip()


def _clean_msg(s: str) -> str:
    """Variant of _clean_arg for MESSAGE BODIES (the text that goes into
    WhatsApp / Telegram / email). Strips outer whitespace + leading commas
    (Whisper inserts 'saying,' as 'saying, X'), then converts spoken
    punctuation words ('question mark', 'comma', etc.) to their symbols.
    Does NOT strip trailing punctuation — that's part of the intended message."""
    s = (s or "").strip().strip(",\"' ").strip()
    return _normalize_spoken_punctuation(s)


# Mid-utterance cancel markers. When a user changes their mind partway
# through ("Open Chrome... no wait, open Firefox"), the LATEST intent
# should win — not the original one which Whisper keeps appended to the
# transcript. We find the LAST cancel marker and discard everything before
# it. If what's left is too short to be its own command (likely a parameter
# correction like "set volume to 50, I meant 70"), we leave the full command
# alone and let the LLM disambiguate.
_CANCEL_MARKERS = re.compile(
    r"(?:^|[,.\s])\s*"
    r"(?:no(?:[,.\s]+forget(?:\s+it)?|[,.\s]+wait|[,.\s]+actually)|"
    r"forget(?:\s+(?:it|that))?|"
    r"never\s*mind|nevermind|"
    r"scratch\s+(?:that|it)|"
    r"cancel(?:\s+that)?|"
    r"actually(?:\s+wait)?|"
    r"wait[,.\s]+|"
    r"i\s+mean)\b"
    r"[,.\s]+",
    re.IGNORECASE,
)


def _strip_self_corrections(cmd: str) -> str:
    """If the user reverses themselves mid-utterance, take only the part
    AFTER the LAST cancel marker. Returns the original command when no
    correction is found or when the post-cancel remainder is too short
    (probably just a parameter tweak, not a fresh command).

    CRITICAL: a cancel marker at the START of the utterance is the user's
    VERB, not a self-correction. Previous bug: 'Cancel all reminders' was
    stripped to 'all reminders' because 'cancel' is in the marker list.
    Requires at least one non-whitespace char BEFORE the marker."""
    matches = list(_CANCEL_MARKERS.finditer(cmd))
    if not matches:
        return cmd
    last = matches[-1]
    # Require real content BEFORE the marker. If the user STARTS with
    # 'cancel ...' / 'forget ...' / 'never mind ...', that's the intent
    # itself, not a self-correction.
    before = cmd[:last.start()].strip().strip(".!?,;: ")
    if not before:
        return cmd
    after = cmd[last.end():].strip().strip(".!?,;: ")
    # Need at least 2 words after — otherwise it's likely "I meant 70" type
    # parameter correction; fall back to the full command for LLM judgement.
    if len(after.split()) < 2:
        return cmd
    return after


# Patterns that signal "this needs real reasoning" — not facts the LLM can
# blurt out, not commands the fast-route can dispatch. Local 1B chokes on
# these and routinely hallucinates ("What's the capital of France" → "System
# start"). When a cloud provider is configured, we route these to SARASWATI
# (Gemini/Groq) instead of stalling locally. The user explicitly asked for
# ODIN to use their cloud API keys as a proxy — this is that path firing
# automatically rather than requiring "research X" / "ask AI about X".
_COMPLEX_OPENERS = re.compile(
    r"^(?:explain\b|describe\b|elaborate(?:\s+on)?\b|"
    r"how\s+(?:does|do|did|can|should|would)\b|"
    r"why\s+(?:is|are|was|were|do|does|did|can|should|would)\b|"
    r"what\s+(?:does|do|did)\s+\w+\s+mean\b|"
    r"what(?:'?s|\s+is)\s+the\s+(?:difference|reason|cause|effect|meaning|relationship|connection)\b|"
    r"compare\b|contrast\b|"
    r"summari[sz]e\b|"
    r"tell\s+me\s+about\b|"
    r"how\s+to\b|"
    r"give\s+me\s+(?:an?\s+)?(?:overview|summary|explanation)\b)",
    re.IGNORECASE,
)


def _looks_complex(command: str) -> bool:
    """True if this query warrants cloud-AI synthesis over local 1B.
    The 1B model can't do open-ended explanations / comparisons / summaries
    reliably; cloud Gemini does them in 1-3 seconds."""
    if _COMPLEX_OPENERS.match(command.strip()):
        return True
    # Long natural-language questions tend to need synthesis too.
    if "?" in command and len(re.findall(r"\b\w+\b", command)) > 10:
        return True
    return False


# Conversational openers — casual chat, opinions, feelings. 1B can't hold a
# real conversation; cloud Gemini can. These patterns route to SARASWATI in
# _handle so ODIN replies like a person, not a stalling LLM. Distinct from
# _COMPLEX_OPENERS (which is about analytical questions).
_CONVERSATIONAL_OPENERS = re.compile(
    r"^(?:i\s+(?:think|feel|want|need|don'?t|am|was|wonder|believe|hope|wish)\b|"
    r"i'?(?:m|ve|d|ll)\s+\w+|"
    r"do\s+you\s+(?:think|believe|like|hate|miss|remember|know)\b|"
    r"can\s+(?:we|you)\s+(?:talk|chat|discuss)\b|"
    r"let'?s\s+(?:talk|chat|discuss)\b|"
    r"what'?s\s+on\s+your\s+mind|"
    r"are\s+you\s+(?:happy|sad|bored|tired|alive|conscious|real|sentient)\b|"
    r"tell\s+me\s+(?:a\s+story|something\s+interesting|how\s+you\s+feel)\b|"
    r"opinion\s+on|your\s+(?:thought|view|take)\s+on|"
    r"what\s+do\s+you\s+think\s+(?:about|of))",
    re.IGNORECASE,
)


def _looks_conversational(command: str) -> bool:
    """True if the command is casual chat that warrants a smart conversational
    reply, not a tool dispatch and not a research synthesis. Cloud Gemini
    handles these in 1-2 seconds with a real human-feeling response. Local 1B
    would either stall or produce a robotic 'As an AI...' deflection."""
    cmd = command.strip()
    if _CONVERSATIONAL_OPENERS.match(cmd):
        return True
    # "how are you" ANYWHERE — greetings arrive with tails/prefixes
    # ("Long time no see. How are you?") that the anchored openers miss,
    # and the miss sent pure chat through the semantic-route toll booth.
    if re.search(r"\bhow\s+are\s+you\b", cmd, re.I):
        return True
    # Mostly non-Latin text (Hindi etc.): no fast-route or skill catalogue
    # entry will match it — treat as conversation and skip straight to the
    # chat path instead of burning seconds embedding it.
    letters = [c for c in cmd if c.isalpha()]
    if letters:
        non_ascii = sum(1 for c in letters if ord(c) > 127)
        if non_ascii / len(letters) > 0.5:
            return True
    return False


def _build_find_files_args(m) -> dict:
    """Build args for JANUS.find_files_smart from a regex match.
    Groups: 1=file_type, 2=location, 3=min_size, 4=min_unit, 5=max_size, 6=max_unit."""
    args = {
        "file_type": (m.group(1) or "").strip(),
        "location":  (m.group(2) or "").strip(),
    }
    def _to_mb(num, unit):
        if not num or not unit:
            return 0.0
        try:
            n = float(num)
        except ValueError:
            return 0.0
        u = unit.lower()
        if u == "gb": return n * 1024
        if u == "kb": return n / 1024
        return n  # mb
    min_mb = _to_mb(m.group(3), m.group(4))
    max_mb = _to_mb(m.group(5), m.group(6))
    if min_mb > 0: args["min_size_mb"] = min_mb
    if max_mb > 0: args["max_size_mb"] = max_mb
    return args


_SEND_FOLLOWUP_RE = re.compile(
    r"\s+(?:and|then|after\s+(?:that|which))\s+"
    r"(?:please\s+|just\s+|now\s+)?"
    r"(?:send|deliver|fire|shoot|do|push|click\s+send|hit\s+send|"
    r"send\s+it|send\s+that|send\s+the\s+(?:email|message))"
    r"(?:\s+(?:it|that|the\s+(?:email|message|note)|now|please))?"
    r"[.!?]*$",
    re.IGNORECASE,
)
_EMAIL_HEAD_RE = re.compile(
    r"^(?:draft|write|send|compose|email|message|text|whatsapp|"
    r"telegram|dm|notify|tell)\b",
    re.IGNORECASE,
)


def _strip_send_followup(text: str) -> str:
    """When a command STARTS with an email/message verb and ENDS with
    '... and send it' / '... then send', strip the tail. The compose route
    already opens a Send button; the redundant tail was tripping
    _is_multi_step and routing the whole thing to GIL, which then stalled.
    From the session log: 'Draft an email to X saying Y and send it' →
    multi-step → GIL → 87s timeout. With this strip, the tail is gone
    BEFORE multi-step detection runs, so draft_email fast-route wins."""
    if not _EMAIL_HEAD_RE.match(text):
        return text
    stripped = _SEND_FOLLOWUP_RE.sub("", text)
    return stripped if stripped != text else text


_SPELLED_LETTERS_RE = re.compile(
    # 3+ single uppercase letters separated by spaces/hyphens/dots/commas.
    # Anchored with \b so it doesn't merge accidental letters from words.
    r"\b([A-Z](?:[\s.\-,]+[A-Z]){2,})\b",
)


def _normalize_spelled_letters(text: str) -> str:
    """When a user spells out a name letter-by-letter, Whisper transcribes
    it as 'K-A-R-T-I-K' or 'K A R T I K' or 'K. A. R. T. I. K.' depending
    on prosody. Concatenate the letters into a single capitalized word so
    the matcher can find the contact.

    Also REMOVES the original heard-version word that often precedes the
    spelled-out version. Pattern: '<HeardWord> <Spelled>' → '<Spelled>'.
    (When the user spells, they mean the spelled version, not the heard one.)
    """
    def _spelled_to_word(m: re.Match) -> str:
        letters = re.sub(r"[\s.\-,]+", "", m.group(1))
        # Title-case the result: first letter upper, rest lower.
        return letters[:1].upper() + letters[1:].lower() if letters else m.group(0)
    out = _SPELLED_LETTERS_RE.sub(_spelled_to_word, text)
    # If a spelled-out word is preceded by another word that looks like an
    # incorrect transcription of it, drop the preceding word. E.g.:
    # "Karthik Kartik" → "Kartik" (user heard Whisper say 'Karthik' and
    # spelled the correct version 'Kartik' to fix it).
    # Heuristic: '<lowercased word> <Capitalized concat-spelled word>' where
    # both words are similar (Levenshtein ≤ N) → drop the first.
    import difflib as _dl
    def _drop_predecessor(text: str) -> str:
        # Find any pattern of "<Word1> <Word2>" where Word2 came from a
        # spelled-letter collapse (we marked it by virtue of being adjacent
        # to the spelled pattern in the original). Easiest: re-scan for
        # adjacent same-ish words separated by space.
        toks = text.split()
        if len(toks) < 2:
            return text
        out_toks = []
        skip_next = False
        for i, tok in enumerate(toks):
            if skip_next:
                skip_next = False
                continue
            if i + 1 < len(toks):
                next_tok = toks[i + 1]
                # Both must be alpha-only (no @, no numbers in this position)
                if (tok.isalpha() and next_tok.isalpha()
                        and len(tok) >= 4 and len(next_tok) >= 4):
                    ratio = _dl.SequenceMatcher(None, tok.lower(), next_tok.lower()).ratio()
                    if ratio >= 0.75:
                        # Treat the SECOND (the spelled-out one) as authoritative.
                        # Drops both exact-duplicates ('Alex Alex') and
                        # near-misses ('Karthik Kartik').
                        continue
            out_toks.append(tok)
        return " ".join(out_toks)
    out = _drop_predecessor(out)
    return out


_SEMANTIC_ROUTE_SYSTEM = """You are ODIN's command router. The user said something in natural language. Pick the SINGLE best skill from the catalogue that matches their intent, and extract any arguments.

OUTPUT RULES (strict):
- Reply with VALID JSON only. No prose, no markdown fences, no explanation.
- Schema: {"skill": "<exact_skill_name>", "args": {...}}  OR  {"skill": null}
- The skill name MUST appear EXACTLY in the provided catalogue, or be null.
- Extract args ONLY from what the user actually said — never invent values.
- If the command is small-talk, an opinion question, an explanation request, or anything that needs a CONVERSATIONAL reply, return {"skill": null} — those are handled elsewhere.
- If multiple skills could fit, pick the more specific one. Bias against destructive skills unless the user explicitly asked for them.
- If the user's intent is unclear or no skill is a good match, return {"skill": null}. Better to fall through than to dispatch the wrong action."""


def _parse_route_json(text: str):
    """Robust JSON extractor — handles ``` fences and prose-wrapped output."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _build_skill_catalogue(marduk, hermes, query: str, top_k: int = 18) -> list:
    """Top-K most-relevant skills for `query`, by HERMES embedding similarity
    if available. Falls back to ALL skills when HERMES isn't ready.
    Returns the raw tool list (Ollama tool format) the LLM will see."""
    tools = marduk.get_all_tools()
    if not tools:
        return []
    if not hermes or not getattr(hermes, "_embed_ok", False) or len(tools) <= top_k:
        return tools[:max(top_k, 12)]
    try:
        qvec = hermes._embed(query)
        if not qvec:
            return tools[:top_k]
    except Exception:
        return tools[:top_k]
    scored = []
    import math as _math
    for tool in tools:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        text = f"{name}. {fn.get('description', '')}"
        try:
            tvec = hermes._embed(text)
        except Exception:
            continue
        if not tvec or len(tvec) != len(qvec):
            continue
        dot = sum(x * y for x, y in zip(qvec, tvec))
        na = _math.sqrt(sum(x * x for x in qvec))
        nb = _math.sqrt(sum(y * y for y in tvec))
        if na and nb:
            scored.append((dot / (na * nb), tool))
    if not scored:
        return tools[:top_k]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [t for _, t in scored[:top_k]]


_LEARNED_ROUTES_PATH = "data/knowledge/learned_routes.jsonl"
_LEARNED_ROUTES_CAP = 500           # bound the cache so it can't grow forever
_LEARNED_MATCH_THRESHOLD = 0.85     # cosine similarity to a cached utterance
# In-memory cache mirror — populated lazily on first lookup, kept fresh by
# _record_learned_route. Keeps the lookup path fast (no JSON re-read per turn).
_LEARNED_ROUTES_CACHE: list[dict] = []
_LEARNED_ROUTES_LOADED = False


def _load_learned_routes() -> None:
    """One-shot loader. Populates _LEARNED_ROUTES_CACHE from disk."""
    global _LEARNED_ROUTES_LOADED
    if _LEARNED_ROUTES_LOADED:
        return
    import os as _os
    _LEARNED_ROUTES_LOADED = True
    if not _os.path.exists(_LEARNED_ROUTES_PATH):
        return
    try:
        with open(_LEARNED_ROUTES_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    _LEARNED_ROUTES_CACHE.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass


def _normalize_for_learned(cmd: str) -> str:
    """Canonical form for learned-route lookups: lowercased, ALL punctuation
    stripped, internal whitespace collapsed. So 'Shut up about THE
    reminders, please!?' matches the stored 'shut up about the reminders
    please' on the exact-match path before fuzzy fallback."""
    out = re.sub(r"[.!?,;:\"'()\[\]{}\-]+", " ", (cmd or "").strip())
    return re.sub(r"\s+", " ", out).strip().lower()


def _record_learned_route(command: str, skill: str, args: dict, hermes) -> None:
    """Persist a successful semantic-route dispatch so future identical /
    near-identical phrasings hit the cache and skip the cloud call.

    Stores embedding-with-command so lookup can be fuzzy (cosine ≥ 0.85).
    Bounded at 500 entries via JSONL trim. Idempotent on duplicate utterances
    (refreshes the timestamp, no growth)."""
    _load_learned_routes()
    cmd_norm = _normalize_for_learned(command)
    if not cmd_norm or not skill:
        return
    embedding: list[float] = []
    if hermes and getattr(hermes, "_embed_ok", False):
        try:
            embedding = hermes._embed(cmd_norm) or []
        except Exception:
            embedding = []
    entry = {
        "ts": time.time(),
        "command": cmd_norm,
        "skill": skill,
        "args": args or {},
        "embedding": embedding,
    }
    # Drop any prior entry for the EXACT same normalized command — keep one.
    global _LEARNED_ROUTES_CACHE
    _LEARNED_ROUTES_CACHE = [e for e in _LEARNED_ROUTES_CACHE
                              if e.get("command") != cmd_norm]
    _LEARNED_ROUTES_CACHE.append(entry)
    # Trim to cap (drop oldest).
    if len(_LEARNED_ROUTES_CACHE) > _LEARNED_ROUTES_CAP:
        _LEARNED_ROUTES_CACHE = _LEARNED_ROUTES_CACHE[-_LEARNED_ROUTES_CAP:]
    # Persist (rewrite the whole file — small + simple at this scale).
    try:
        import os as _os
        _os.makedirs(_os.path.dirname(_LEARNED_ROUTES_PATH), exist_ok=True)
        with open(_LEARNED_ROUTES_PATH, "w", encoding="utf-8") as f:
            for e in _LEARNED_ROUTES_CACHE:
                f.write(json.dumps(e) + "\n")
    except OSError:
        pass


def _check_learned_route(command: str, hermes) -> tuple[str, dict] | None:
    """Fuzzy-lookup the user's command against cached successful routes.
    Exact match → instant return. Otherwise cosine-rank by embedding and
    accept if ≥ _LEARNED_MATCH_THRESHOLD. Returns (skill, args) or None."""
    _load_learned_routes()
    if not _LEARNED_ROUTES_CACHE:
        return None
    cmd_norm = _normalize_for_learned(command)
    if not cmd_norm:
        return None
    # Exact match — fastest path.
    for entry in reversed(_LEARNED_ROUTES_CACHE):
        if entry.get("command") == cmd_norm:
            return (entry["skill"], entry.get("args", {}) or {})
    # Fuzzy match — needs HERMES + an embedding cached on entries.
    if not hermes or not getattr(hermes, "_embed_ok", False):
        return None
    try:
        qvec = hermes._embed(cmd_norm)
    except Exception:
        return None
    if not qvec:
        return None
    import math as _math
    best_score = 0.0
    best_entry = None
    for entry in _LEARNED_ROUTES_CACHE:
        evec = entry.get("embedding") or []
        if not evec or len(evec) != len(qvec):
            continue
        dot = sum(x * y for x, y in zip(qvec, evec))
        na = _math.sqrt(sum(x * x for x in qvec))
        nb = _math.sqrt(sum(y * y for y in evec))
        if na == 0 or nb == 0:
            continue
        score = dot / (na * nb)
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_entry and best_score >= _LEARNED_MATCH_THRESHOLD:
        return (best_entry["skill"], best_entry.get("args", {}) or {})
    return None


def _semantic_route(command: str, marduk, sara) -> tuple[str, dict] | None:
    """Paraphrase-tolerant fallback. When fast-route regexes miss a command
    that clearly wants ACTION (not chat), ask cloud LLM to pick the right
    skill + extract args from the most-relevant top-K skill catalogue.

    Returns (skill_name, args_dict) on a confident pick, or None to let the
    caller fall through to GIL / cloud-chat path.

    Why this exists: the user complained that ODIN was too rigid — only
    exact-phrasing commands hit fast-routes. Semantic routing accepts
    any paraphrase ('cancel reminders' / 'stop the reminder spam' /
    'shut up about reminders' all → clear_reminders)."""
    if not command or len(command.split()) < 2:
        return None
    if not marduk or not sara or not getattr(sara, "providers", None):
        return None
    hermes = marduk.get_module("HERMES")
    catalogue = _build_skill_catalogue(marduk, hermes, command, top_k=18)
    if not catalogue:
        return None

    lines = []
    for tool in catalogue:
        fn = tool.get("function", {})
        name = fn.get("name", "")
        if not name:
            continue
        params = list(fn.get("parameters", {}).get("properties", {}).keys())
        desc = (fn.get("description") or "").replace("\n", " ")[:140]
        lines.append(f"- {name}({', '.join(params)}): {desc}")

    prompt = (
        f"User said: \"{command}\"\n\n"
        f"Available skills:\n" + "\n".join(lines) + "\n\n"
        f"Reply with JSON only."
    )
    try:
        text, _ = sara._call_with_fallback(
            sara.general_chain, _SEMANTIC_ROUTE_SYSTEM, prompt, max_tokens=200
        )
    except Exception:
        return None
    data = _parse_route_json(text or "")
    if not isinstance(data, dict):
        return None
    skill = data.get("skill")
    args = data.get("args", {})
    if not skill or skill not in marduk._skill_map:
        return None
    if not isinstance(args, dict):
        args = {}
    # Arg-shape validation. Reject dispatches where the LLM stuffed a
    # sentence into a single-token parameter (the 'press_key with a whole
    # sentence as the key' bug). Each skill's parameter dict tells us the
    # expected type; we reject obviously-wrong shapes.
    if not _validate_route_args(skill, args, marduk):
        return None
    return (skill, args)


# Parameter names that should always be SHORT atoms (single key, level,
# percentage, etc.) — never a multi-word sentence. If the cloud LLM stuffs
# a sentence into one of these, we reject the dispatch rather than fire
# a nonsensical action like press_key('open photo named SRM on desktop').
_SHORT_ATOM_PARAMS = frozenset({
    "key", "keys", "letter", "char", "level", "percent", "value",
    "amount", "port", "year", "days", "hours", "minutes", "limit",
    "country", "code",
})


def _validate_route_args(skill: str, args: dict, marduk) -> bool:
    """Return True if `args` look reasonable for the given skill. False
    means we'll reject the cloud-LLM pick and fall through to GIL. This
    catches the failure mode where the LLM picks ANY skill and dumps the
    whole user utterance into ONE field."""
    if not args:
        return True
    module_name = marduk._skill_map.get(skill)
    if not module_name:
        return False
    module = marduk._modules.get(module_name)
    if not module:
        return False
    skill_def = next((s for s in module.skills if s["name"] == skill), None)
    if not skill_def:
        return True   # can't validate, allow through
    params_def = skill_def.get("parameters", {}) or {}
    for arg_name, arg_val in args.items():
        if arg_name not in params_def:
            continue   # extra arg; harmless (Ollama strips unknowns)
        if isinstance(arg_val, str):
            # Atom params with multi-word string values are almost always
            # the LLM dumping the sentence. Reject.
            if arg_name in _SHORT_ATOM_PARAMS:
                if " " in arg_val.strip() or len(arg_val.strip()) > 20:
                    return False
            # Integer-typed params with non-numeric strings = wrong shape.
            param_type = (params_def[arg_name].get("type") or "").lower()
            if param_type == "integer":
                try:
                    int(arg_val)
                except (TypeError, ValueError):
                    return False
    return True


def _apply_phonetic_corrections(text: str, corrections: dict) -> str:
    """Replace each (heard → meant) phonetic pair in text. Whole-word match,
    case-insensitive. Preserves the original case-shape of the replacement.
    Iterative because one correction might surface another (rare)."""
    if not text or not corrections:
        return text
    out = text
    for heard, meant in corrections.items():
        if not heard:
            continue
        # Word-boundary case-insensitive replace.
        pattern = re.compile(r"\b" + re.escape(heard) + r"\b", re.I)
        out = pattern.sub(meant, out)
    return out


_PLATFORM_ALIAS_MAP = {
    "whatsapp": "whatsapp", "whatsap": "whatsapp", "wa": "whatsapp",
    "telegram": "telegram", "tg": "telegram",
    "instagram": "instagram", "insta": "instagram", "ig": "instagram",
    "email": "email", "mail": "email", "gmail": "email",
}


_SPOKEN_PUNCTUATION = [
    # Order matters — longer phrases first so "exclamation point" beats
    # "exclamation" matching anywhere inside it.
    (re.compile(r"\bquestion\s+mark\b", re.I),       "?"),
    (re.compile(r"\bexclamation\s+(?:point|mark)\b", re.I),  "!"),
    (re.compile(r"\bfull\s+stop\b", re.I),           "."),
    (re.compile(r"\bsemi[\s-]?colon\b", re.I),       ";"),
    (re.compile(r"\bcomma\b", re.I),                 ","),
    (re.compile(r"\bcolon\b", re.I),                 ":"),
    # "period" is tricky — could be a literal period or someone saying a
    # historical era. Only convert when followed by EOL / a delimiter,
    # NOT when it appears mid-sentence as a noun.
    (re.compile(r"\bperiod(?=\s*[.!?]?$)", re.I),    "."),
    # "dot dot dot" → ellipsis
    (re.compile(r"\b(?:dot\s+){2}dot\b", re.I),      "…"),
    # Emojis-by-name — only when explicitly invoked
    (re.compile(r"\bheart\s+emoji\b", re.I),         "❤"),
    (re.compile(r"\bsmiley(?:\s+emoji|\s+face)?\b", re.I),  ":)"),
    (re.compile(r"\bsad\s+face\b", re.I),             ":("),
]


def _normalize_spoken_punctuation(text: str) -> str:
    """Convert words like 'question mark' / 'comma' / 'exclamation point' to
    their symbolic form. ONLY applied to the *message body* of messaging
    commands (not commands themselves, since 'comma' in 'open Toyota Camry'
    would be misread). Caller decides where to apply."""
    out = text
    # FIRST: drop any preceding period/comma when followed by a spoken-
    # punctuation phrase. Whisper inserts these for sentence rhythm, and
    # without this we get "College over.?" instead of "College over?".
    out = re.sub(
        r"[.,]\s+(?=(?:question\s+mark|exclamation\s+(?:point|mark)|"
        r"comma|period|full\s+stop|semi[\s-]?colon|colon))",
        " ", out, flags=re.I,
    )
    for pattern, sub in _SPOKEN_PUNCTUATION:
        out = pattern.sub(sub, out)
    # Tighten whitespace: 'foo ?' → 'foo?', 'foo ,' → 'foo,'
    out = re.sub(r"\s+([?!,;:.])", r"\1", out)
    # Collapse multi-spaces left behind
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def _normalize_handle_speech(text: str) -> str:
    """Spoken Telegram / Twitter / Discord handles come through Whisper as
    'my underscore odin underscore bot' / 'at the rate my...'.
    Convert to canonical '@my_odin_bot' so handle-aware routes can
    match. Only fires when there's a clear handle context (preceding 'to',
    '@', or 'at the rate').

    CRITICAL: 'underscore' followed by a preposition/keyword (on, to, in,
    saying, etc.) is NOT a handle joiner — it's the end of the handle
    followed by the next word. Previous bug: 'Karthik underscore 2204
    underscore on Instagram' was glued into 'Karthik_2204_on Instagram',
    breaking the 'on Instagram' platform marker."""
    out = text

    # Words that mark the END of a handle — never glue '_' to these.
    _STOP_AFTER_UNDERSCORE = {
        "on", "in", "at", "to", "for", "with", "from", "and", "or",
        "saying", "telling", "with", "that", "by", "of", "the", "a",
        "an", "is", "are", "was", "were", "be",
    }

    def _maybe_glue(m):
        before, after_word = m.group(1), m.group(2)
        if after_word.lower() in _STOP_AFTER_UNDERSCORE:
            # Trailing 'underscore' before a preposition is filler — DROP it
            # entirely so 'Kartik_2204 underscore on Instagram' becomes
            # 'Kartik_2204 on Instagram'. Leaving 'underscore' as a word
            # breaks downstream platform-marker regexes.
            return f"{before} {after_word}"
        return f"{before}_{after_word}"

    # Iterate until stable — handles 'A underscore B underscore C'.
    prev = None
    while prev != out:
        prev = out
        out = re.sub(r"(\w)\s+underscore\s+(\w+)", _maybe_glue, out, flags=re.I)

    # '(at|add) the rate X' → '@X' (reuse email-style normalization, but only
    # when X doesn't look like a domain — otherwise _normalize_email_speech wins).
    # 'add' covers the common Whisper mishearing of 'at'.
    out = re.sub(
        r"(?:^|\s)(?:at|add)\s+the\s+rate\s+(?=\w+(?:_\w+)*\b(?!\.[a-z]))",
        " @", out, flags=re.I,
    )
    return out


def _normalize_url_speech(text: str) -> str:
    """Whisper transcribes spoken URLs in unhelpful ways:
        'https colon slash slash voters dot eci dot gov dot in'
        'httpsvoters.eci.gov.in'      (slashes dropped, scheme glued to host)
        'https voters dot eci dot gov dot in'   (colons dropped)
        'voters dot eci dot gov dot in'         (scheme dropped entirely)
    Convert these to canonical 'https://host.tld/...' form so the existing
    `^read https?://...` / `^fetch https?://...` fast-routes catch them.
    Only fires when the command plausibly contains a URL — guarded by the
    presence of either a literal scheme or a 'dot <tld>' pattern."""
    looks_url = (
        re.search(r"\bhttps?\b", text, re.I)
        or re.search(r"https?[A-Za-z]", text, re.I)  # glued: 'httpsvoters'
        or re.search(r"\bdot\s+(?:com|org|net|gov|edu|io|in|uk|us|co|de|fr|jp|au|ca|me|tv|app|ai|dev)\b",
                     text, re.I)
        or re.search(r"\.(?:com|org|net|gov|edu|io|in|uk|us|co|de|fr|jp|au|ca|me|tv|app|ai|dev)\b",
                     text, re.I)  # literal .tld already present
    )
    if not looks_url:
        return text
    out = text
    # 'colon slash slash' → '://'
    out = re.sub(r"\s*colon\s+slash\s+slash\s*", "://", out, flags=re.I)
    # 'slash' inside URL-ish context → '/' between word chars
    out = re.sub(r"(\w)\s+slash\s+(\w)", r"\1/\2", out, flags=re.I)
    # 'X dot Y' → 'X.Y' (iterative to handle multi-dot domains)
    prev = None
    while prev != out:
        prev = out
        out = re.sub(r"(\w)\s+dot\s+(\w)", r"\1.\2", out, flags=re.I)
    # Glue 'https' to immediately-following alphanumeric host if Whisper
    # dropped the '://': 'https voters.eci.gov.in' → 'https://voters.eci.gov.in'.
    out = re.sub(r"\bhttps?\s+(?=\w+\.\w+)", lambda m: m.group(0).strip() + "://", out, flags=re.I)
    # Handle 'httpsvoters.eci.gov.in' (no separator at all).
    out = re.sub(r"\bhttps?(?=[A-Za-z][A-Za-z0-9]+\.[A-Za-z])", lambda m: m.group(0) + "://", out, flags=re.I)
    # If there's still no scheme but a clear host.tld, prepend https://
    # (only when the user verb was a read/fetch/browse-style command).
    if re.search(r"\b(?:read|fetch|grab|open|browse|navigate|go\s+to)\s+", out, re.I):
        out = re.sub(
            r"(\b(?:read|fetch|grab|open|browse|navigate|go\s+to)\s+(?:the\s+)?(?:webpage\s+|website\s+|site\s+|page\s+|url\s+)?)"
            r"((?:www\.)?[a-z][a-z0-9\-]{1,40}\.[a-z]{2,6}(?:\.[a-z]{2,3})?[\w/\-?=&%.]*)",
            r"\1https://\2",
            out, flags=re.I,
        )
    return out


def _normalize_email_speech(text: str) -> str:
    """Whisper transcribes spoken email addresses as 'X at the rate Y dot com'.
    Convert to 'X@Y.com'. Only applies when the command is plausibly about
    email — guarding against mangling "meet me at the rate of $20/hour"."""
    if not re.search(r"\b(?:email|e[-\s]?mail|gmail|inbox|mailbox|send\s+(?:a\s+)?message\s+to|"
                     r"compose|draft\s+(?:a\s+)?message)\b", text, re.IGNORECASE):
        return text
    # "at the rate" → "@"
    # 'at|add the rate (of)' → '@'.  'add' is the common Whisper mishearing.
    out = re.sub(r"\s+(?:at|add)\s+the\s+rate(?:\s+of)?\s+", "@", text, flags=re.IGNORECASE)
    # "X dot Y" → "X.Y" between word characters (covers ".com", ".edu.in", etc.)
    # Apply repeatedly to handle multi-dot domains like "srmist.edu.in".
    prev = None
    while prev != out:
        prev = out
        out = re.sub(r"(\w)\s+dot\s+(\w)", r"\1.\2", out, flags=re.IGNORECASE)
    # When Whisper drops "the rate" and just says "at": only convert "at" → "@"
    # if the token after "at" looks like a domain (has at least one dot).
    out = re.sub(r"(\w)\s+at\s+(\w+(?:\.\w+){1,})", r"\1@\2", out, flags=re.IGNORECASE)
    return out


def _r(pattern: str, skill: str, args_fn=None):
    """Build a fast-route entry: compiled regex + callable that returns kwargs."""
    return (re.compile(pattern, re.IGNORECASE), skill, args_fn or (lambda m: {}))


_FAST_ROUTES = [
    # Info / status (no args)
    # Anchored to ^…$ so "long time no see", "next time", "good time", etc.
    # don't trigger time-of-day lookups. Previously the unanchored \btime\b
    # was matching inside any conversational phrase that contained "time".
    _r(r"^(?:what(?:'?s|\s+is)\s+(?:the\s+)?time|"
       r"(?:tell\s+me\s+)?(?:the\s+)?(?:current\s+)?time|"
       r"time(?:\s+please|\s+now)?)[.!?]?$",
       "get_time"),
    _r(r"^(?:what(?:'?s|\s+is)\s+(?:today'?s?|the)\s+date|"
       r"(?:today'?s?\s+)?date|"
       r"what(?:'?s|\s+is)\s+the\s+date(?:\s+today)?)[.!?]?$",
       "get_time"),
    _r(r"^(?:what\s+day(?:\s+is\s+it)?(?:\s+today)?|day\s+of\s+the\s+week)[.!?]?$",
       "get_time"),
    # "weather in Tokyo" / "weather for Bangalore" / "what's the weather in X"
    # → city-specific. Must come BEFORE the city-less weather pattern below
    # or the latter swallows everything.
    _r(r"\b(?:check\s+(?:the\s+)?)?weather\s+(?:in|at|for)\s+(.+?)"
       r"(?:\s+(?:right\s+now|today|currently|please|now))?[.!?]?$",
       "get_weather", lambda m: {"city": _clean_arg(m.group(1))}),
    _r(r"^what(?:'?s|\s+is)\s+the\s+weather\s+(?:in|at|for|like\s+in)\s+(.+?)"
       r"(?:\s+(?:right\s+now|today|currently|please|now))?[.!?]?$",
       "get_weather", lambda m: {"city": _clean_arg(m.group(1))}),
    # No-city fallback: "weather", "what's the weather", "check the weather right now"
    # Anchored to start-of-string so "browse find the weather on weather.com" does
    # NOT match here — that earlier session's bug was the `\b` boundary letting
    # "weather" win anywhere in the command. Specific verb-prefix routes
    # (browse / read / fetch) still come below but they'll never reach since
    # this would have stolen the match.
    _r(r"^(?:check\s+(?:the\s+)?)?weather(?:\s+right\s+now|\s+today|\s+currently)?[.!?]?$"
       r"|^what(?:'?s|\s+is)\s+the\s+weather(?:\s+right\s+now|\s+today)?[.!?]?$",
       "get_weather_here"),
    _r(r"\bsystem (?:stats|status|health)\b|\bhow(?:'?s| is) my (?:cpu|ram|memory|computer)",      "get_system_stats"),
    _r(r"\bcheck (?:the )?(?:internet|network|wifi)\b|\bam i online\b",                            "check_internet"),
    _r(r"\bself ?diagnos|run diagnostics?\b",                                                      "run_diagnostics"),
    _r(r"\bmy location\b|\bwhere am i\b",                                                          "get_my_location"),
    # "how are you / how's it going / you alright" → quick mythic reply.
    # Distinct from the "who are you" path so 1B doesn't drift into a long
    # All-Father monologue (~30 s GIL stall observed). Bypasses GIL entirely.
    _r(r"^(?:how\s+(?:are\s+you|'?re\s+you|are\s+ya|r\s+u|you\s+doing)|"
       r"how(?:'?s| is)\s+it\s+going|"
       r"how(?:'?s| is)\s+life|"
       r"you\s+(?:alright|okay|good))[.!?]?$",
       "how_are_you"),
    # "tell me more / continue / go on / elaborate" — follow-up that re-runs
    # the last wiki lookup with an expanded summary (4 sentences instead of 2).
    _r(r"^(?:tell\s+me\s+more|"
       r"(?:keep\s+)?(?:going|talking)|"
       r"continue|"
       r"go\s+on|"
       r"more(?:\s+(?:info|details|on\s+(?:him|her|it|that|this)))?|"
       r"elaborate(?:\s+(?:please|on\s+that))?|"
       r"expand(?:\s+on\s+that)?)[.!?]?$",
       "tell_me_more"),
    # IMPORTANT ORDER: clear_reminders + delete_reminder must come BEFORE
    # list_reminders, since the list pattern matches \b…my reminders\b which
    # also matches "clear all my reminders" / "cancel my reminder to X".

    # Clear ALL reminders — high-priority. Solves the "meds + stand-up
    # reminders fire every hour with no voice off-switch" problem.
    _r(r"^(?:clear|cancel|delete|stop|remove|kill|silence)"
       r"\s+(?:all\s+(?:my\s+|the\s+)?|my\s+|the\s+)?"
       r"(?:remaining\s+|active\s+)?reminders?[.!?]?$",
       "clear_reminders"),
    _r(r"^(?:stop|cancel|silence|shut\s+up\s+about)\s+(?:the\s+)?reminders?[.!?]?$",
       "clear_reminders"),
    # Delete a SPECIFIC reminder by substring match.
    _r(r"^(?:delete|cancel|remove)\s+(?:the\s+|my\s+)?reminders?\s+"
       r"(?:to\s+|about\s+|for\s+)?(.+?)[.!?]?$",
       "delete_reminder",
       lambda m: {"text": _clean_arg(m.group(1))}),
    # List reminders LAST — anchored to ^…$ so it can't steal commands that
    # contain the substring "my reminders".
    _r(r"^(?:list|show(?:\s+me)?)\s+(?:my\s+|the\s+|all\s+(?:my\s+|the\s+)?)?"
       r"(?:remaining\s+|active\s+)?reminders?[.!?]?$"
       r"|^(?:remaining|active|my)\s+reminders?[.!?]?$"
       r"|^what\s+(?:are\s+)?(?:my\s+)?reminders?[.!?]?$",
       "list_reminders"),

    # System actions (no args)
    _r(r"\block (?:the )?(?:screen|computer|pc)\b",                                                "lock_screen"),
    _r(r"\b(?:take|capture) (?:a )?screen ?shot\b",                                                "take_screenshot"),
    # Perception: "what's on my screen" / "read my screen" → HORUS OCR;
    # "describe my screen" / "what am I looking at" → HORUS vision (llava).
    # HORUS pushes what it sees into SESHAT's working memory, so follow-ups
    # ("what did it say about X") interrogate the screen it just read.
    _r(r"^what(?:'?s| is| does)\s+(?:on\s+|showing\s+on\s+)?(?:my\s+|the\s+)?screen"
       r"(?:\s+say(?:ing)?)?(?:\s+right\s+now)?[.!?]?$"
       r"|^read\s+(?:my\s+|the\s+)?(?:screen|display)[.!?]?$"
       r"|^what\s+text\s+is\s+on\s+(?:my\s+|the\s+)?screen[.!?]?$",
       "read_screen"),
    _r(r"^describe\s+(?:my\s+|the\s+|this\s+)?screen[.!?]?$"
       r"|^what\s+am\s+i\s+looking\s+at[.!?]?$"
       r"|^what\s+(?:can|do)\s+you\s+see(?:\s+on\s+(?:my\s+|the\s+)?screen)?(?:\s+right\s+now)?[.!?]?$",
       "describe_screen"),
    _r(r"\bgood ?night\b",                                                                         "goodnight"),
    _r(r"\bstart focus(?: mode)?\b|\bfocus mode on\b",                                             "start_focus"),
    _r(r"\bend focus(?: mode)?\b|\bstop focus\b|\bfocus mode off\b",                               "end_focus"),
    _r(r"^pomodoro\b|^start pomodoro\b",                                                           "start_pomodoro"),
    _r(r"^night mode\b|^activate night mode\b",                                                    "activate_night_mode"),
    _r(r"^backup (?:everything|all|my (?:stuff|data))\.?$|^back ?up\.?$",                          "backup_all"),

    # Brightness / volume
    _r(r"^(?:what(?:'?s| is)|get|tell me|check)\s+(?:the\s+)?(?:current\s+|screen\s+)?brightness\b", "get_brightness"),
    _r(r"\bvolume up\b|\bturn (?:up|the volume up)\b|\braise (?:the )?volume\b",                    "volume_up"),
    _r(r"\bvolume down\b|\bturn (?:down|the volume down)\b|\blower (?:the )?volume\b",              "volume_down"),
    _r(r"^mute\b|\btoggle mute\b|\bmute (?:the )?(?:audio|sound|volume)\b",                         "mute"),

    # Actions with arg extraction
    # 'open <X>' is capped at 1-3 words to avoid greedy capture of long
    # phrases like "open a folder named X on desktop" — those fall through.
    # Negative lookahead blocks self-test / self-check / simulation / focus /
    # pomodoro / etc. so "start self-test" reaches VYASA below, not THOR.
    _r(r"^(?:open|launch|start|run)\s+"
       r"(?!self[- ]?test\b|self[- ]?testing\b|self[- ]?check\b|"
       r"sim(?:ulation|\s+loop)?\b|"
       r"focus(?:\s+mode)?\b|pomodoro\b|"
       r"night\s+mode\b|night\s+routine\b|"
       r"diagnos(?:tic|is|e)\b|"
       r"backup\b|morning\s+digest\b)"
       r"(\S+(?:\s+\S+){0,2})[.!?]?$", "open_application",
       lambda m: {"app_name": _clean_arg(m.group(1))}),
    _r(r"^(?:close|quit|kill)\s+(.+)$",            "kill_process",
       lambda m: {"process_name": _clean_arg(m.group(1))}),
    _r(r"^play\s+(.+)$",                           "play_music",
       lambda m: {"query": _clean_arg(m.group(1))}),
    # search_web bails on:
    #   - "search my vault for X" / "search notes for X"  (vault search)
    #   - "look up X on wikipedia"                          (wiki path)
    #   - "look up book X" / "find the book X"              (Open Library)
    # find_files_smart MUST come before search_web — otherwise "search for
    # images on desktop" / "find PDFs in downloads" gets caught by the
    # general web-search pattern. Local file search is sub-second; the user
    # explicitly wanted these to be instant. (Also catches "list X in Y"
    # before the generic list_directory pattern in the same block below.)
    # The optional bridge "(which|that) (is|are|have)..." catches natural
    # phrasings like "PDFs in desktop which are bigger than 5 MB" — the
    # earlier version required the size clause to immediately follow the
    # location word, which broke on the bridge.
    _r(r"^(?:find|search\s+for|show\s+me|list)\s+"
       r"(?:all\s+)?"
       r"(\w+(?:\s+(?:files|documents))?)\s+"
       r"(?:in|on|from|inside|within)\s+(?:my\s+|the\s+)?"
       r"(\w+(?:\s+(?:folder|directory))?)"
       r"(?:\s+(?:which|that)\s+(?:is|are|have|having|with|with\s+size))?"
       r"(?:\s+(?:bigger|larger|greater|more|over)\s+than\s+(\d+(?:\.\d+)?)\s*(mb|gb|kb))?"
       r"(?:\s+and)?"
       r"(?:\s+(?:smaller|less|fewer|under)\s+than\s+(\d+(?:\.\d+)?)\s*(mb|gb|kb))?"
       r"[.!?]?$",
       "find_files_smart",
       _build_find_files_args),

    _r(r"^(?!.*\bon\s+wikipedia\b)"
       r"(?:search|google|look\s+up)\s+"
       r"(?!(?:my\s+)?(?:vault|notes)\b)"
       r"(?!(?:the\s+|a\s+)?book\b)"
       r"(?:the\s+web\s+)?(?:for\s+)?(.+)$",
       "search_web",
       lambda m: {"query": _clean_arg(m.group(1))}),
    _r(r"^translate\s+(.+?)\s+(?:to|into)\s+(\w+)[.!?]?$", "translate_text",
       lambda m: {"text": _clean_arg(m.group(1)), "target_language": _clean_arg(m.group(2))}),

    # SESHAT learn-from-link. Mostly arrives via NARADA/HUGIN text (URLs are
    # rarely spoken). 'learn/study/ingest/read <url>' or bare verb + link.
    _r(r"^(?:learn|study|ingest|read)\s+(?:this\s+|from\s+)?"
       r"(?:link|url|site|page|repo|video|photo|image)?\s*:?\s*"
       r"(https?://\S+)\s*$",
       "learn_link",
       lambda m: {"url": m.group(1).rstrip(".,;!?")}),
    # Smart-by-default URL handling: a BARE link (optionally with a lead-in
    # like "check this out" / "look at this"), or a comprehension request
    # ("what's on X", "tell me about X", "summarize X", "what does X say"),
    # goes through SESHAT.learn_link — which scrapes the page (crawl4ai via
    # AKASHA), has SARASWATI understand it, caches the note to the vault, and
    # speaks the gist. So handing ODIN a URL means "go read and understand
    # this", not just "open a tab". Explicit "fetch/scrape <url>" stays a raw
    # pull (the fetch_page route further below).
    _r(r"^(?:check\s+(?:this\s+)?out[,:\s]+|look\s+at\s+(?:this[,:\s]+)?|"
       r"see\s+(?:this[,:\s]+)?|here'?s?\s+|this[,:\s]+)?(https?://\S+)\s*$",
       "learn_link",
       lambda m: {"url": m.group(1).rstrip(".,;!?")}),
    _r(r"^(?:what(?:'s| is| does)?\s+(?:on|in|at|inside)?\s*|tell\s+me\s+about\s+|"
       r"summari[sz]e\s+|explain\s+|break\s+down\s+|digest\s+)"
       r"(?:the\s+)?(?:webpage\s+|website\s+|site\s+|page\s+|article\s+|url\s+)?"
       r"(https?://\S+?)(?:\s+say)?[.!?]?$",
       "learn_link",
       lambda m: {"url": m.group(1).rstrip(".,;!?")}),
    # Follow-up on whatever ODIN just learned (link or file) — answered
    # instantly from the saved note, so "summarize that" / "what did it say
    # about X" / "what was in it" continue the thread instead of dead-ending.
    _r(r"^(?:summari[sz]e|recap|sum\s+up|tl;?dr(?:\s+of)?)\s+"
       r"(?:it|that|this|the\s+(?:doc(?:ument)?|file|page|article|link|paper|pdf|video))[.!?]?$",
       "recall_learned", lambda m: {"question": ""}),
    _r(r"^what(?:'?s| was| is)\s+in\s+"
       r"(?:it|that|this|the\s+(?:doc(?:ument)?|file|paper|pdf|page|article))[.!?]?$",
       "recall_learned", lambda m: {"question": ""}),
    _r(r"^what\s+(?:did|does)\s+(?:it|that|the\s+(?:doc(?:ument)?|file|page|article|link|paper|video))\s+"
       r"(?:say|cover|mention|talk\s+about)(?:\s+about\s+(.+?))?[.!?]?$",
       "recall_learned", lambda m: {"question": (m.group(1) or "").strip()}),
    _r(r"^remember\s+(?:that\s+)?(.+?)\s+is\s+(.+)$", "remember",
       lambda m: {"key": _clean_arg(m.group(1)), "value": _clean_arg(m.group(2))}),
    _r(r"^recall\s+(.+)$",                         "recall",
       lambda m: {"key": _clean_arg(m.group(1))}),
    _r(r"^(?:set|change)\s+tone\s+(?:to\s+)?(\w+)\.?$", "set_tone",
       lambda m: {"mode": m.group(1).strip()}),

    # Persona switching — keeps the All-Father as default; switches to plain AI on demand.
    _r(r"^(?:be|go|stay)\s+(?:serious|real|straight|professional)(?:\s+mode)?\.?$"
       r"|^serious\s+mode\.?$"
       r"|\bdrop\s+the\s+(?:act|role[- ]?play)\b"
       r"|\bstop\s+role[- ]?playing\b"
       r"|\btalk\s+normally\b"
       r"|\bbe\s+straight(?:\s+with\s+me)?\b"
       r"|\bassistant\s+mode\.?$",
       "set_persona", lambda m: {"mode": "assistant"}),
    _r(r"^(?:be|go)\s+(?:yourself|mythic|the\s+all[- ]?father|odin\s+again|god)(?:\s+mode)?\.?$"
       r"|^mythic\s+mode\.?$"
       r"|^god\s+mode\.?$"
       r"|\bstay\s+in\s+character\b"
       r"|\bback\s+to\s+(?:the\s+)?(?:all[- ]?father|odin|god)\b",
       "set_persona", lambda m: {"mode": "mythic"}),
    # NB: read_file has a negative lookahead — if the argument starts with a
    # URL or URL-filler ("a webpage https://..."), it falls through so the
    # AKASHA fetch_page route below catches it instead of trying to open it
    # as a local file.
    # Local DOCUMENT file → SESHAT.learn_file (extract → understand → vault),
    # the file analog of the smart URL routing above: handing ODIN a document
    # means "read and understand this", and follow-ups ("summarize that")
    # work afterward. Bare filenames are resolved against Desktop/Downloads/
    # Documents/vault by SESHAT. .xlsx stays with GANESH; .txt/.md stay raw.
    _r(r"^(?:read|open\s+and\s+read|summari[sz]e|describe|outline|explain|digest|"
       r"go\s+through|what(?:'?s| is)\s+in|tell\s+me\s+about)\s+"
       r"(?:my\s+|the\s+|this\s+)?(?:file\s+|document\s+|doc\s+|pdf\s+|paper\s+|report\s+)?"
       r"(.+?\.(?:pdf|docx?|pptx?|odt|rtf|epub))[.!?]?$",
       "learn_file", lambda m: {"path": _clean_arg(m.group(1))}),
    _r(r"^read\s+(?!(?:(?:a|the|an)\s+)?(?:webpage|website|web\s+page|url|page|site)\b)"
       r"(?!https?://)(?:file\s+)?(.+)$",
       "read_file", lambda m: {"path": _clean_arg(m.group(1))}),
    # list_memories must come BEFORE list_directory — otherwise "list my memories"
    # is captured as a directory path and explodes with "Not a directory".
    _r(r"^(?:list|show(?:\s+me)?)\s+(?:my\s+)?(?:memories|memory|facts|preferences|prefs|profile)[.!?]?$",
       "list_memories"),
    _r(r"^what\s+do\s+you\s+(?:remember|know)(?:\s+about\s+me)?[.!?]?$",
       "list_memories"),
    # Conversation continuity (HERMES) — the discussion itself is memory.
    # "what did we decide about X" → semantic search over past exchanges;
    # "what were we talking about" → session summary; "where were we" →
    # the last exchange, to resume a thread after an interruption.
    _r(r"^what\s+(?:did|have)\s+we\s+(?:talk(?:ed)?\s+about|discuss(?:ed)?|say|said|decide[d]?|agree[d]?(?:\s+on)?)\s+"
       r"(?:about\s+|on\s+|regarding\s+)?"
       r"(?!(?:today|earlier|so\s+far|this\s+session)[.!?]?$)(.+?)[.!?]?$",
       "search_memory", lambda m: {"query": _clean_arg(m.group(1))}),
    _r(r"^what\s+(?:did|have)\s+we\s+(?:talk(?:ed)?\s+about|discuss(?:ed)?)"
       r"(?:\s+(?:today|earlier|so\s+far|this\s+session))?[.!?]?$"
       r"|^what\s+(?:were|are)\s+we\s+(?:talking\s+about|discussing)[.!?]?$"
       r"|^recap\s+(?:our\s+|the\s+)?(?:conversation|session|chat)[.!?]?$",
       "summarize_session"),
    _r(r"^where\s+were\s+we[.!?]?$"
       r"|^what\s+was\s+i\s+(?:saying|asking)[.!?]?$"
       r"|^what\s+were\s+we\s+(?:doing|on)[.!?]?$",
       "get_last_exchange"),
    # list_directory now has a negative lookahead for memory-like words so it
    # only matches genuine directory queries. The exclusion list also covers
    # phonetic corrections, contact aliases/favorites, and reminders so those
    # explicit list-* commands hit their dedicated routes below instead of
    # being treated as a directory path.
    _r(r"^list\s+"
       r"(?!(?:my\s+)?(?:memories|memory|facts|preferences|prefs|profile"
       r"|reminders|phonetic|aliases|contact\s+aliases|favorites?|favourites?"
       r"|favorited|favourited|send\s+history)\b)"
       r"(?:files\s+in\s+|directory\s+)?(.+)$",
       "list_directory",
       lambda m: {"path": _clean_arg(m.group(1))}),
    _r(r"^(?:create|make)\s+(?:a\s+)?folder\s+(?:named\s+|called\s+)?(.+)$", "create_directory",
       lambda m: {"path": _clean_arg(m.group(1))}),
    # Fuzzy "open the folder named X" / "show me X" — searches user profile.
    # Now covers photo / image / picture / video / document / spreadsheet /
    # note too (previous bug: 'Open the photo named SRM' fell to semantic
    # route which mis-picked press_key).
    # Strips trailing location/filler phrases the user adds naturally:
    #   "open the folder Project Files present on desktop"
    #   "open file resume located in downloads"
    #   "show me notes saved on my drive"
    # so JANUS searches for the clean name 'Project Files', not the noisy form.
    _r(r"^(?:open|show(?:\s+me)?|find|locate)\s+(?:the\s+|a\s+|my\s+|that\s+)?"
       r"(?:folder|file|directory|photo|image|picture|video|movie|clip|song|track|"
            r"document|doc|spreadsheet|sheet|note|notes|"
            r"presentation|slide|slides|pdf)s?\s+"
       r"(?:named\s+|called\s+|titled\s+)?(.+?)"
       r"(?:\s+"
            r"(?:that\s+is\s+|which\s+is\s+|located\s+|present\s+|saved\s+|stored\s+)?"
            r"(?:on|in|at|under|inside)\s+"
            r"(?:this|my|the)?\s*"
            r"(system|computer|pc|laptop|machine"
            r"|desktop|downloads|documents|home|drive|brain|onedrive|pictures|videos|music)"
       r")?"
       r"[.!?]?$",
       "find_and_open",
       lambda m: {"name": _clean_arg(m.group(1)),
                  "location": (m.group(2) or "").lower()}),
    _r(r"^delete\s+(.+)$",                         "delete_path",
       lambda m: {"path": _clean_arg(m.group(1))}),
    _r(r"^log\s+(\d+)\s+(?:glasses?|cups?)\s+of\s+water\.?$", "log_water",
       lambda m: {"glasses": int(m.group(1))}),
    # "log a glass" / "i had a glass of water" → log 1
    _r(r"^(?:log\s+(?:a|one)\s+(?:glass|cup)(?:\s+of\s+water)?|i\s+(?:had|drank)\s+(?:a|one|some)?\s*(?:glass|cup)(?:\s+of\s+water)?)\.?$",
       "log_water", lambda m: {"glasses": 1}),

    # === System power ============================================
    _r(r"^(?:shut\s*down|power\s*off|turn\s+off)(?:\s+(?:the\s+)?(?:computer|pc|laptop|system))?\.?$",
       "shutdown"),
    _r(r"^(?:restart|reboot)(?:\s+(?:the\s+)?(?:computer|pc|laptop|system))?\.?$",
       "restart"),
    _r(r"^(?:sleep|put\s+(?:the\s+)?(?:computer|pc|laptop)\s+to\s+sleep|go\s+to\s+sleep)\.?$",
       "sleep_computer"),

    # === Media controls ==========================================
    _r(r"^(?:pause(?:\s+(?:the\s+)?(?:music|video|song))?|stop\s+(?:the\s+)?music)\.?$",
       "media_play_pause"),
    _r(r"^(?:resume|continue|unpause)(?:\s+(?:the\s+)?(?:music|video|song))?\.?$",
       "media_play_pause"),
    _r(r"^(?:next\s+(?:track|song|video)|skip(?:\s+(?:the\s+)?(?:track|song|video|this))?)\.?$",
       "media_next"),
    _r(r"^(?:previous|prev|last)\s+(?:track|song|video)\.?$|^go\s+back\s+(?:a|one)\s+(?:track|song)\.?$",
       "media_previous"),
    _r(r"^stop(?:\s+the)?\s+(?:music|video|playback)\.?$",
       "media_stop"),

    # === Window mgmt =============================================
    _r(r"^(?:show|go\s+to)\s+(?:the\s+)?desktop\.?$|^minimi[sz]e\s+(?:everything|all\s+windows?|all)\.?$",
       "show_desktop"),

    # === App shortcuts ============================================
    # Native-first: these all go through THOR.open_application which
    # checks Start Menu BEFORE web fallback. So WhatsApp / Discord /
    # Spotify open in your already-logged-in desktop app, not the web UI.
    # Native WhatsApp (Microsoft Store app). Accepts verbose forms users
    # naturally speak when they want to disambiguate from WhatsApp Web:
    # "open the WhatsApp app", "launch native WhatsApp", "open WhatsApp on
    # this system not the web", "open the desktop WhatsApp". The native app
    # is preferred — THOR's Get-StartApps inventory finds it directly.
    _r(r"^(?:open|launch|start|use|check)\s+"
       r"(?:the\s+|my\s+)?"
       r"(?:native\s+|desktop\s+|installed\s+|local\s+)?"
       r"whats?app(?:\s+app)?"
       r"(?:\s+(?:on\s+this\s+(?:system|computer|pc|laptop|machine)"
            r"|locally"
            r"|not\s+(?:the\s+)?web"
            r"|available\s+on\s+this\s+(?:system|computer|pc)"
            r"))*"
       r"[.!?]?$",
       "open_application", lambda m: {"app_name": "whatsapp"}),
    _r(r"^open\s+(?:my\s+)?(?:youtube\s+music|yt\s+music|ytm)\.?$",
       "open_application", lambda m: {"app_name": "youtube music"}),
    _r(r"^open\s+(?:my\s+)?(discord|spotify|slack|teams|telegram|signal|obsidian)\.?$",
       "open_application", lambda m: {"app_name": m.group(1).lower()}),
    # Gmail / Calendar / Inbox → web (no native Windows clients typically)
    _r(r"^(?:open|check|show\s+me)\s+(?:my\s+)?(?:gmail|email|inbox|mail)\.?$",
       "open_gmail"),
    _r(r"^(?:open|show\s+me)\s+(?:my\s+|the\s+)?calendar\.?$",
       "open_calendar"),

    # === Email drafting (MERCURY.draft_email) =====================
    # Opens Gmail compose with body pre-filled. User clicks Send.
    # Email-speech normalization runs BEFORE this so "at the rate" → "@"
    # and "X dot com" → "X.com" already happened. Captures the recipient
    # email and the body following "saying"/"telling"/"about"/"that".
    # `to` is optional — covers both "Write email to X saying Y" and
    # "Email X saying Y" / "Send X an email saying Y".
    _r(r"^(?:write|send|compose|draft|email)"
       r"(?:\s+(?:an?\s+)?(?:e[-\s]?mail|message|note|gmail))?\s+"
       r"(?:to\s+)?"
       # Optional filler word(s) before the address. Voice users say all of:
       # "the address", "address", "email id", "email", "id", "user",
       # "recipient", "this email", "his email". Make all of these optional.
       r"(?:(?:the\s+|this\s+|his\s+|her\s+|their\s+)?"
       r"(?:e[-\s]?mail[\s-]?id|email[\s-]?id|"
       r"e[-\s]?mail|email|"
       r"address|recipient|user|id)\s+)?"
       r"([\w.+\-]+@[\w.\-]+\.[a-z]{2,})"
       r"(?:\s+(?:saying|telling\s+(?:them|him|her|\w+)?\s*(?:that\s+)?|"
       r"about|with\s+subject|that|"
       r"to\s+(?:say|tell)\s+(?:them|him|her|\w+)?\s*(?:that\s+)?))?"
       r"\s+(.+?)[.!?]?$",
       "draft_email",
       lambda m: {"to": m.group(1), "subject": "From ODIN", "body": _clean_msg(m.group(2))}),

    # === Reminders ===============================================
    # "remind me to X at Y" / "remind me to X in N minutes"
    # Group 2 captures the trigger keyword + value together so CHRONOS sees
    # the natural form ("in 5 minutes", "tomorrow at 9am") it knows how to parse.
    # Recurring: "remind me to X every Y" / "remind me to X daily at Y" /
    # "remind me to X hourly". CHRONOS detects the recurrence kind from the
    # 'when' string and re-arms after each fire. MUST come before the
    # one-shot pattern below — otherwise "every day at 7am" gets sliced
    # so "at 7am" wins as group 2 and the "every day" recurrence is lost.
    _r(r"^remind\s+me\s+to\s+(.+?)\s+((?:every|daily|hourly)\s+.+?)[.!?]?$",
       "set_reminder",
       lambda m: {"text": _clean_arg(m.group(1)),
                  "when": _clean_arg(m.group(2))}),
    _r(r"^remind\s+me\s+to\s+(.+?)\s+(hourly|daily)[.!?]?$",
       "set_reminder",
       lambda m: {"text": _clean_arg(m.group(1)),
                  "when": _clean_arg(m.group(2))}),
    # One-shot: "remind me to X at|in|on|by|tomorrow|today Y"
    _r(r"^remind\s+me\s+to\s+(.+?)\s+((?:at|in|on|by|tomorrow|today)\s+.+?)[.!?]?$",
       "set_reminder",
       lambda m: {"text": _clean_arg(m.group(1)),
                  "when": _clean_arg(m.group(2))}),

    # === Health & Finance ========================================
    _r(r"^(?:get|show|tell\s+me)\s+(?:my\s+)?health(?:\s+summary)?\.?$",
       "get_health_summary"),
    _r(r"^(?:get|show|tell\s+me)\s+(?:my\s+)?(?:spending|expenses)(?:\s+summary)?\.?$",
       "get_spending_summary"),
    _r(r"^(?:get|check|show\s+me)\s+(?:my\s+)?budget(?:\s+status)?\.?$",
       "get_budget_status"),

    # === Morning digest (AURORA) ==================================
    # Aggregates time / weather / reminders / health / news into one
    # synthesized briefing. Routes to Gemini for synthesis when available;
    # falls back to plain concatenation otherwise.
    _r(r"^(?:morning|daily)\s+(?:digest|brief(?:ing)?|update|roundup|rundown)[.!?]?$"
       r"|^(?:give\s+me\s+(?:my\s+|the\s+)?)?(?:morning|daily)\s+(?:digest|brief(?:ing)?)[.!?]?$"
       r"|^what(?:'?s|\s+is)\s+(?:my\s+)?(?:today|day)\s+looking\s+like[.!?]?$"
       r"|^brief\s+me(?:\s+on\s+today)?[.!?]?$"
       r"|^today'?s?\s+(?:briefing|brief|digest|rundown|overview)[.!?]?$"
       r"|^morning\s+routine\s+briefing[.!?]?$",
       "morning_digest"),

    # === Diagnostics / SHERLOCK ==================================
    _r(r"^(?:weekly\s+digest|what\s+broke\s+this\s+week|failure\s+(?:summary|digest))\.?$",
       "weekly_digest"),
    _r(r"^uptime\.?$|^how\s+long\s+have\s+you\s+been\s+(?:up|running)\.?$",
       "get_odin_uptime"),
    # usage_audit — surfaces what the user has been doing with ODIN. Inspired
    # by ECC's continuous-learning concept (assistant analyses its own usage).
    _r(r"^(?:usage\s+(?:audit|report|stats|summary)|"
       r"how\s+have\s+i\s+been\s+using\s+(?:you|odin)|"
       r"what\s+have\s+i\s+been\s+(?:doing|asking)|"
       r"my\s+usage(?:\s+(?:patterns|summary|stats))?|"
       r"analyz?e\s+my\s+usage|"
       r"how\s+often\s+do\s+i\s+use\s+you)[.!?]?$",
       "usage_audit"),

    # === Free-tier APIs (no AI) ===================================
    # Flight tracking — "track flight AI 132" / "where is BA178"
    _r(r"^(?:track\s+(?:flight\s+)?|where\s+is\s+(?:flight\s+)?)([A-Za-z]{2,3}\s?\d{1,4}[A-Za-z]?)[.!?]?$",
       "track_flight", lambda m: {"callsign": _clean_arg(m.group(1))}),
    # Currency conversion — "convert 100 USD to INR" / "100 dollars in rupees"
    _r(r"^(?:convert\s+)?(\d+(?:\.\d+)?)\s+(\w{3})\s+(?:to|in|into)\s+(\w{3})[.!?]?$",
       "convert_currency",
       lambda m: {"amount": float(m.group(1)),
                  "from_currency": m.group(2).upper(),
                  "to_currency":   m.group(3).upper()}),
    # Crypto prices — covers many phrasings:
    #   "bitcoin price" / "btc price"
    #   "price of eth" / "price of bitcoin"
    #   "how much is dogecoin" / "how much is bitcoin worth"
    #   "what's the price of bitcoin" / "what is bitcoin price"
    #   "tell me bitcoin price" / "bitcoin worth"
    _r(r"^(?:what(?:'?s|\s+is)\s+(?:the\s+)?(?:price\s+of\s+|cost\s+of\s+)?|"
       r"price\s+of\s+|"
       r"how\s+much\s+is\s+|"
       r"how\s+much\s+does\s+|"
       r"tell\s+me\s+(?:the\s+)?(?:price\s+of\s+|cost\s+of\s+)?)?"
       r"(bitcoin|btc|ethereum|eth|dogecoin|doge|solana|sol|cardano|ada|"
       r"ripple|xrp|polygon|matic|polkadot|dot|litecoin|ltc)"
       r"\s*(?:price|cost|going\s+for|worth)?[.!?]?$",
       "crypto_price",
       lambda m: {"coin": m.group(1)}),
    # Air quality — "air quality in Bangalore" / "AQI Delhi"
    _r(r"^(?:air\s+quality|aqi)\s+(?:in|for|at)?\s*(.+?)[.!?]?$",
       "air_quality", lambda m: {"city": _clean_arg(m.group(1))}),
    # Public holidays — "public holidays" / "holidays this year"
    _r(r"^(?:public\s+)?holidays?(?:\s+this\s+year)?[.!?]?$",
       "public_holidays"),
    # Country can be a 2-letter ISO code OR a name ("India" → IN, "United States" → US).
    # _resolve_country_code does the mapping inside chronos. Falls back to "any" if
    # the country isn't recognised so the LLM doesn't have to handle it.
    _r(r"^(?:public\s+)?holidays?\s+(?:in|of|for)\s+([A-Za-z][A-Za-z\s]+?)[.!?]?$",
       "public_holidays", lambda m: {"country": _clean_arg(m.group(1))}),
    # Books — "find the book Sapiens" / "look up book 1984"
    _r(r"^(?:find|look\s+up|search\s+for)\s+(?:the\s+)?book\s+(.+?)[.!?]?$",
       "book_lookup", lambda m: {"title": _clean_arg(m.group(1))}),

    # Locate by name — "find the latest invoice" / "where is report.pdf" /
    # "locate my resume in documents". Finds WITHOUT opening and records the
    # path as ODIN's focus, so "…and summarize it" chains work. Guards: the
    # find_files_smart route ("find PDFs in desktop") sits EARLIER and wins
    # its shape; book route above wins books; drive/vault/web excluded here.
    # Gate: only claim "find X" when X plainly smells like a FILE — has a
    # latest/newest qualifier, a dot-extension, a file-noun, or a location
    # suffix. "Find a good restaurant" must still fall through to the LLM.
    _r(r"^(?!.*\b(?:drive|vault|web|book|wikipedia)\b)"
       r"(?:find|locate|where(?:'s|\s+is))\s+"
       r"(?:the\s+|my\s+|a\s+)?"
       r"(?=(?:latest|newest|most\s+recent)\b"
       r"|.*\.\w{2,4}\b"
       r"|.*\b(?:files?|folder|invoice|report|resume|cv|notes?|photo|picture|screenshot|video|song|document|presentation|spreadsheet|assignment|thesis|paper)\b"
       r"|.*\s(?:in|on)\s+(?:my\s+|the\s+)?(?:desktop|downloads|documents|pictures|videos|music|brain|onedrive)\b)"
       r"(latest\s+|newest\s+|most\s+recent\s+)?"
       r"(.+?)"
       r"(?:\s+(?:in|on)\s+(?:my\s+|the\s+)?"
       r"(desktop|downloads|documents|pictures|videos|music|brain|onedrive))?"
       r"[.!?]?$",
       "locate_file",
       lambda m: {"name": _clean_arg(m.group(2)),
                  "location": (m.group(3) or "").strip(),
                  "newest": bool(m.group(1))}),

    # === Drive reindex (CHITRA) ====================================
    _r(r"^(?:re[- ]?index|refresh|rebuild)\s+(?:my\s+)?drive(?:\s+index)?[.!?]?$",
       "reindex_drive"),

    # === Obsidian Canvas (NABU.create_canvas) =====================
    # "create a canvas about X" / "mind map X" — JSON Canvas v1.0 file
    # in the vault, opens visually in Obsidian.
    _r(r"^(?:create|make|build)\s+(?:a\s+|an\s+)?(?:canvas|mind[- ]?map)\s+"
       r"(?:about|on|for|of)\s+(.+?)(?:\s+with\s+(.+?))?[.!?]?$",
       "create_canvas",
       lambda m: {"title": _clean_arg(m.group(1)),
                  "nodes": _clean_arg(m.group(2)) if m.group(2) else ""}),

    # === Scaffold a starter project (SARASWATI) ===================
    # "scaffold an app that X" / "build me a starter project for X" /
    # "create a new project that does X" — cloud-driven scaffolder writes
    # a runnable skeleton under ~/Desktop/<slug>.
    _r(r"^(?:scaffold|generate|build\s+me|create)\s+(?:an?\s+)?"
       r"(?:starter\s+project|app|project|skeleton|template)\s+"
       r"(?:that|for|to|which)\s+(.+?)[.!?]?$",
       "scaffold_app", lambda m: {"description": _clean_arg(m.group(1))}),
    _r(r"^scaffold\s+(.+?)[.!?]?$",
       "scaffold_app", lambda m: {"description": _clean_arg(m.group(1))}),

    # === Codebase bundle (HEPHAESTUS) =============================
    # "bundle the codebase" / "pack odin" / "package the project for review"
    # — produces a single Markdown file at Desktop suitable for pasting to
    # cloud Claude. Inspired by yamadashy/repomix.
    _r(r"^(?:bundle|pack|package)\s+(?:the\s+)?(?:codebase|project|repo|odin)[.!?]?$"
       r"|^(?:make|create)\s+(?:a\s+)?(?:codebase\s+)?bundle[.!?]?$"
       r"|^package\s+(?:the\s+)?project\s+for\s+review[.!?]?$",
       "bundle_codebase"),

    # === Excel / spreadsheets (GANESH) ============================
    # "summarize X.xlsx" / "what's in my budget.xlsx" — summarize is the
    # first thing the LLM should call before reading/writing because it
    # tells us sheets + headers.
    _r(r"^(?:summari[sz]e|describe|outline)\s+(?:my\s+|the\s+)?(.+?\.xlsx?)[.!?]?$",
       "excel_summarize", lambda m: {"path": _clean_arg(m.group(1))}),
    _r(r"^what(?:'?s|\s+is)\s+(?:in\s+)?(?:my\s+|the\s+)?(.+?\.xlsx?)[.!?]?$",
       "excel_summarize", lambda m: {"path": _clean_arg(m.group(1))}),
    # "read X.xlsx" / "open X.xlsx for reading" — pull cells.
    _r(r"^(?:read|show\s+me|display|print)\s+(?:my\s+|the\s+)?"
       r"(?:spreadsheet|excel|workbook)?\s*(.+?\.xlsx?)[.!?]?$",
       "excel_read", lambda m: {"path": _clean_arg(m.group(1))}),
    # "create excel X" / "new spreadsheet X"
    _r(r"^(?:create|make|new)\s+(?:a\s+)?(?:new\s+)?(?:excel|spreadsheet|workbook)\s+(?:called\s+|named\s+)?(.+?)[.!?]?$",
       "excel_create", lambda m: {"path": _clean_arg(m.group(1))}),
    # "export X.xlsx to CSV"
    _r(r"^(?:export|convert)\s+(.+?\.xlsx?)\s+(?:to\s+)?csv[.!?]?$",
       "excel_to_csv", lambda m: {"path": _clean_arg(m.group(1))}),

    # === Self-test loop (VYASA) ===================================
    # "start self-test" / "begin testing yourself" / "simulate yourself"
    # — kicks off VYASA's background simulation loop.
    _r(r"^(?:start|begin|run)\s+(?:the\s+)?(?:self[- ]?test|self[- ]?testing|"
       r"simulation|sim\s+loop|self[- ]?check)[.!?]?$"
       r"|^(?:simulate|test)\s+yourself[.!?]?$"
       r"|^test\s+(?:yourself|odin)\s+(?:in\s+the\s+)?background[.!?]?$",
       "start_self_test"),
    _r(r"^(?:stop|end|halt|pause)\s+(?:the\s+)?(?:self[- ]?test|self[- ]?testing|"
       r"simulation|sim\s+loop|self[- ]?check)[.!?]?$",
       "stop_self_test"),
    _r(r"^(?:simulation|self[- ]?test|sim)\s+report[.!?]?$"
       r"|^what\s+have\s+you\s+been\s+testing[.!?]?$"
       r"|^show\s+(?:the\s+|me\s+)?(?:self[- ]?test|simulation)\s+(?:report|results|status)[.!?]?$",
       "simulation_report"),
    _r(r"^(?:simulate|run)\s+(?:one\s+)?(?:test|sim)(?:\s+(?:command|now))?[.!?]?$",
       "simulate_once"),

    # === Self-diagnostic (MIMIR) ==================================
    # "diagnose yourself" / "what's wrong" / "why did you fail" / "explain
    # the last failure" — MIMIR looks at ODIN's own log + failure trail
    # and tells the user (and ODIN itself) where the bugs are.
    _r(r"^(?:diagnose\s+yourself|self[- ]?diagnose|what(?:'?s|\s+is)\s+wrong|"
       r"what(?:'?s|\s+is)\s+broken|check\s+yourself|introspect)[.!?]?$",
       "diagnose"),
    _r(r"^(?:propose|suggest|find)\s+(?:fixes|improvements|optimi[sz]ations?)[.!?]?$"
       r"|^how\s+can\s+(?:you|i)\s+improve\s+(?:yourself|odin)[.!?]?$",
       "propose_fixes"),
    _r(r"^(?:why\s+did\s+(?:you\s+|that\s+)?fail|"
       r"explain\s+(?:the\s+)?(?:last\s+)?(?:failure|error|crash|problem))[.!?]?$",
       "explain_last_failure"),

    # === Autonomous goal executor (TYR) ===========================
    # "do this: X" / "pursue goal X" / "achieve X" / "plan and execute X"
    # — hands an open-ended goal to TYR, which plans + executes a real
    # skill chain via cloud LLM, reflects until done. Destructive goals
    # need 'confirmed' in the goal text.
    _r(r"^(?:pursue\s+(?:the\s+)?goal[:\s]+|achieve(?:\s+this)?[:\s]+|"
       r"plan\s+and\s+(?:execute|do)[:\s]+|"
       r"autonomously\s+(?:do|complete)[:\s]+|"
       r"goal[:\s]+)(.+?)[.!?]?$",
       "pursue_goal", lambda m: {"goal": _clean_arg(m.group(1))}),
    _r(r"^do\s+this\s*[:\-]\s*(.+?)[.!?]?$",
       "pursue_goal", lambda m: {"goal": _clean_arg(m.group(1))}),

    # === Google Drive (CHITRA) =====================================
    # "search my drive for X" / "find X in my drive" — Drive native search.
    _r(r"^(?:search|find|look\s+up|look\s+for)\s+(?:in\s+)?(?:my\s+)?drive\s+(?:for\s+)?(.+?)[.!?]?$",
       "drive_search", lambda m: {"query": _clean_arg(m.group(1))}),
    _r(r"^(?:search|find|look\s+up|look\s+for)\s+(.+?)\s+in\s+(?:my\s+)?drive[.!?]?$",
       "drive_search", lambda m: {"query": _clean_arg(m.group(1))}),
    # "ask my drive X" / "what does my drive say about X" — RAG.
    _r(r"^ask\s+(?:my\s+)?drive\s+(?:about\s+)?(.+?)[.!?]?$",
       "ask_my_drive", lambda m: {"question": _clean_arg(m.group(1))}),
    _r(r"^what\s+(?:does|do)\s+(?:my\s+)?(?:drive|notes|documents?)\s+say\s+about\s+(.+?)[.!?]?$",
       "ask_my_drive", lambda m: {"question": _clean_arg(m.group(1))}),
    # "recent files in my drive" / "what did I work on this week"
    _r(r"^(?:recent|latest)\s+(?:files|documents?|stuff)\s+(?:in\s+)?(?:my\s+)?drive[.!?]?$",
       "drive_recent"),
    _r(r"^what\s+did\s+i\s+work\s+on\s+(?:recently|this\s+week|today|yesterday)[.!?]?$",
       "drive_recent"),
    # "open X from my drive" / "fetch X from drive"
    _r(r"^(?:open|fetch|get|read)\s+(.+?)\s+from\s+(?:my\s+)?drive[.!?]?$",
       "drive_fetch", lambda m: {"name_or_id": _clean_arg(m.group(1))}),

    # === Page fetch (AKASHA via Firecrawl) ========================
    # "read https://x.com" / "read a webpage <url>" / "fetch the URL <url>" /
    # "what does <url> say". The optional fillers ('a webpage', 'the url',
    # 'website', etc.) cover the natural way users speak URLs.
    _r(r"^(?:read|fetch|open\s+and\s+read|grab|scrape)"
       r"(?:\s+(?:a|the|an)?\s*(?:webpage|website|web\s+page|url|page|site))?"
       r"\s+(https?://\S+)[.!?]?$",
       "fetch_page", lambda m: {"url": _clean_arg(m.group(1))}),
    _r(r"^what\s+does\s+(https?://\S+)\s+say[.!?]?$",
       "fetch_page", lambda m: {"url": _clean_arg(m.group(1))}),

    # === Browser automation (ARGUS) ==============================
    # Drive a real Chrome browser via cloud LLM. "browse X" / "go online
    # and X" / "open chrome and X" — anything that needs page interaction
    # beyond just opening a tab routes here.
    _r(r"^(?:browse|navigate|go\s+(?:online|to\s+the\s+web))(?:\s+and)?\s+(.+?)[.!?]?$",
       "browse", lambda m: {"task": _clean_arg(m.group(1))}),
    _r(r"^(?:on|in)\s+(?:the\s+)?browser[,\s]+(.+?)[.!?]?$",
       "browse", lambda m: {"task": _clean_arg(m.group(1))}),
    _r(r"^(?:open\s+)?chrome\s+and\s+(.+?)[.!?]?$",
       "browse", lambda m: {"task": _clean_arg(m.group(1))}),
    # "Open X on my browser" / "open X in browser" — drives a web-app
    # interaction (Gmail, Calendar, Drive UI, etc.) through ARGUS. The
    # alternative is opening the desktop client via open_application;
    # this route is for when the user explicitly wants the BROWSER.
    _r(r"^(?:open|use|access)\s+(.+?)\s+(?:on|in|via)\s+(?:my\s+|the\s+)?browser[.!?]?$",
       "browse", lambda m: {"task": f"open {_clean_arg(m.group(1))} in the browser and report the current state"}),
    _r(r"^(?:log\s+in\s+to|log\s+into|sign\s+in\s+to|sign\s+into)\s+(?:my\s+)?(.+?)"
       r"(?:\s+(?:on|in|via)\s+(?:my\s+|the\s+)?browser)?[.!?]?$",
       "browse", lambda m: {"task": f"log in to {_clean_arg(m.group(1))} and report when ready"}),

    # === Favorites — auto-disambiguation memory (MERCURY) ==========
    # 'favorite that' / 'favorite this contact' — promotes the LAST sent
    # recipient to favorites so future messages with that name resolve
    # to the SAME specific chat (no more "Sam(Alex) vs Alex" ambiguity).
    _r(r"^(?:favorite|favourite|save|remember|mark|add)\s+"
       r"(?:that|this|the\s+last|it)"
       r"(?:\s+(?:contact|person|chat))?"
       r"(?:\s+(?:as|to)\s+(?:my\s+)?(?:favorite|favourite|favorites|favourites))?"
       r"[.!?]?$",
       "favorite_last"),
    # "Favorite Alex" — name override; uses last_sent's content but a
    # custom canonical name. Negative lookahead skips that/this/it/the-last
    # AND the bare 'contact'/'person'/'chat' filler words so 'favorite
    # contact Alex' captures 'Alex' (not 'contact Alex') and 'favorite
    # the contact' / 'favorite contact' route to the no-arg favorite_last
    # above instead of capturing 'contact' as a name.
    _r(r"^(?:favorite|favourite|fav)\s+"
       r"(?!(?:that|this|it|the\s+last|the\s+contact|the\s+last\s+contact"
       r"|contact|person|chat|conversation)\b)"
       r"(.+?)[.!?]?$",
       "favorite_last",
       lambda m: {"name": _clean_arg(m.group(1))}),
    # NEW: "Favorite contact X" / "Favorite the contact X" — explicit form
    # where 'contact' is a filler. Strip it and capture X.
    _r(r"^(?:favorite|favourite|fav)\s+(?:the\s+)?(?:contact|person|chat)\s+(.+?)[.!?]?$",
       "favorite_last",
       lambda m: {"name": _clean_arg(m.group(1))}),
    # Unfavorite — drop the disambiguation memory for a contact.
    _r(r"^(?:unfavorite|unfavourite|un[- ]fav|remove)\s+(.+?)"
       r"\s+from\s+(?:my\s+)?favou?rites?"
       r"(?:\s+on\s+(whats?app|telegram|instagram|insta))?[.!?]?$",
       "unfavorite",
       lambda m: {"name": _clean_arg(m.group(1)),
                  "platform": _PLATFORM_ALIAS_MAP.get((m.group(2) or "whatsapp").lower(), "whatsapp")}),
    _r(r"^(?:unfavorite|unfavourite|un[- ]fav)\s+(.+?)[.!?]?$",
       "unfavorite",
       lambda m: {"name": _clean_arg(m.group(1)), "platform": "whatsapp"}),
    # Send history — forensics.
    _r(r"^(?:show\s+(?:me\s+)?|list\s+)?(?:send|message|messaging)\s+history[.!?]?$"
       r"|^what\s+(?:have\s+i\s+sent|did\s+i\s+send)\s+(?:recently|lately|today)?[.!?]?$"
       r"|^recent\s+(?:sends|messages\s+sent|messages)[.!?]?$",
       "send_history"),

    # === Contact aliases / favorites (MERCURY) =====================
    # "Save X as my fav on WhatsApp" / "Save X as Y on Telegram"
    # — short aliases that resolve to real contacts at send time.
    _r(r"^(?:save|set|add|store)\s+(.+?)\s+as\s+(?:my\s+)?"
       r"(fav|favorite|favourite|this|wifey|hubby|bae|bestie|mom|dad|primary|main|default|\w+)"
       r"(?:\s+(?:on|for)\s+(whats?app|telegram|instagram|insta|email))?[.!?]?$",
       "set_contact_alias",
       lambda m: {"alias": _clean_arg(m.group(2)),
                  "contact": _clean_arg(m.group(1)),
                  "platform": _PLATFORM_ALIAS_MAP.get((m.group(3) or "whatsapp").lower(), "whatsapp")}),
    _r(r"^(?:remove|delete|forget|drop)\s+(?:the\s+)?(?:contact\s+)?alias\s+(.+?)[.!?]?$",
       "remove_contact_alias",
       lambda m: {"alias": _clean_arg(m.group(1))}),
    # Aliases (explicit nicknames) — list only on the word "aliases".
    _r(r"^(?:list|show(?:\s+me)?)\s+(?:my\s+)?(?:contact\s+)?aliases[.!?]?$",
       "list_contact_aliases"),
    # Favorites (disambiguation memory) — "list my favorites" routes here,
    # not to list_contact_aliases. Same naming UX as the user asked for.
    _r(r"^(?:list|show(?:\s+me)?)\s+(?:my\s+)?favou?rites?[.!?]?$"
       r"|^(?:my\s+)?favou?rites?[.!?]?$"
       r"|^who\s+are\s+my\s+favou?rites?[.!?]?$"
       r"|^(?:list|show(?:\s+me)?)\s+(?:my\s+)?(?:favorited?|favourited?)\s+contacts?[.!?]?$",
       "list_favorites"),

    # === Phonetic corrections (PROMETHEUS) ========================
    # "When you hear X say Y" / "When I say X you might hear Y"
    # — teach ODIN to fix Whisper mishearings.
    _r(r"^when\s+you\s+hear\s+(.+?)\s+(?:say|think|treat\s+it\s+as|substitute|replace\s+with)\s+(.+?)[.!?]?$",
       "set_phonetic_correction",
       lambda m: {"heard": _clean_arg(m.group(1)), "meant": _clean_arg(m.group(2))}),
    _r(r"^when\s+i\s+say\s+(.+?)\s+you\s+(?:might\s+|sometimes\s+)?hear\s+(.+?)[.!?]?$",
       "set_phonetic_correction",
       lambda m: {"heard": _clean_arg(m.group(2)), "meant": _clean_arg(m.group(1))}),
    _r(r"^correct\s+(.+?)\s+to\s+(.+?)[.!?]?$",
       "set_phonetic_correction",
       lambda m: {"heard": _clean_arg(m.group(1)), "meant": _clean_arg(m.group(2))}),
    _r(r"^list\s+(?:my\s+)?phonetic\s+(?:corrections|fixes|aliases)[.!?]?$"
       r"|^what\s+phonetic\s+(?:corrections|fixes)\s+(?:do\s+i\s+have|are\s+set)[.!?]?$",
       "list_phonetic_corrections"),

    # === Follow-up sends — "same message to X" / "forward to Y" ====
    # These resolve against MERCURY._last_sent (cached previous send) so the
    # user doesn't have to re-type the message body. Without these routes,
    # 'send the same to Karthik' falls to GIL which then hallucinates a
    # new message (the session log showed exactly that — "Hey Karthik,
    # just wanted to let you know I'm running self-testing simulations…").
    # "Send the same (message) to X on Y" / "Send same to X"
    _r(r"^send\s+(?:the\s+)?same\s+(?:message|msg|text|thing|one|note)?"
       r"\s*to\s+(.+?)"
       r"(?:\s+on\s+(whats?app|telegram|instagram|insta|email))?"
       r"[.!?]?$",
       "resend_last",
       lambda m: {"to": _clean_arg(m.group(1)),
                  "platform": (m.group(2) or "").lower()}),
    # "Send same on Y to X" (platform first)
    _r(r"^send\s+(?:the\s+)?same\s+(?:message|msg|text)?\s*"
       r"on\s+(whats?app|telegram|instagram|insta|email)\s+"
       r"to\s+(.+?)[.!?]?$",
       "resend_last",
       lambda m: {"to": _clean_arg(m.group(2)),
                  "platform": m.group(1).lower()}),
    # "Forward (the message|it|that|the WhatsApp message) [you sent to Z] to X [on Y]"
    # The "you sent to Z" middle clause is consumed but unused; we only care
    # about the NEW recipient (X) and optional platform (Y).
    _r(r"^forward\s+(?:the\s+|that\s+|it\s+|this\s+)?"
       r"(?:whats?app|telegram|instagram|insta|email)?\s*"
       r"(?:message|msg|text|note)?\s*"
       r"(?:you\s+(?:just\s+)?(?:sent|wrote)\s+to\s+[^\s]+\s+)?"
       r"to\s+(.+?)"
       r"(?:\s+on\s+(whats?app|telegram|instagram|insta|email))?"
       r"[.!?]?$",
       "resend_last",
       lambda m: {"to": _clean_arg(m.group(1)),
                  "platform": (m.group(2) or "").lower()}),
    # "Do the same for X (on Y)" / "Same for X"
    _r(r"^(?:do\s+)?(?:the\s+)?same\s+for\s+(.+?)"
       r"(?:\s+on\s+(whats?app|telegram|instagram|insta|email))?"
       r"[.!?]?$",
       "resend_last",
       lambda m: {"to": _clean_arg(m.group(1)),
                  "platform": (m.group(2) or "").lower()}),

    # === Messaging — platform-specific fast paths =================
    # WhatsApp / Telegram / Instagram have dedicated MERCURY skills that are
    # faster + cheaper than the generic ARGUS path. Other platforms still
    # route through ARGUS via the generic fallback below.

    # WhatsApp → MERCURY.whatsapp_send (native app via URL scheme).
    # COMMA TOLERANCE: voice transcripts insert commas after "saying" and
    # between args ("WhatsApp, X, saying Y"). All separators that should be
    # whitespace tolerate commas via [,\s]+. Without this, the previous
    # session had multiple sends fall to GIL because of single transcribed
    # commas Whisper inserted around emphasis.
    _r(r"^(?:send|message|whatsapp|wa|deliver|shoot|drop|write|text)"
       r"[,\s]+(?:a\s+)?(?:message|msg|text|note|line|whatsapp)?\s*"
       r"to[,\s]+(.+?)[,\s]+on[,\s]+whats?app"
       r"[,\s]+(?:saying|that\s+says?|with|telling\s+(?:them|him|her|\w+)?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    _r(r"^whats?app[,\s]+(.+?)[,\s]+(?:saying|with|that\s+says?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    # Verbless: "Send Alex a WhatsApp saying X" — common spoken form
    _r(r"^send[,\s]+(.+?)[,\s]+a[,\s]+whats?app[,\s]+(?:saying|with|that\s+says?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    # "Text X on WhatsApp saying Y" — 'text' as imperative verb, no 'to'.
    _r(r"^(?:text|message|whatsapp|wa)[,\s]+(.+?)[,\s]+on[,\s]+whats?app"
       r"[,\s]+(?:saying|with|that\s+says?|telling\s+(?:them|him|her|\w+)?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    # "Whats up X saying Y" — Whisper mishears 'WhatsApp' as 'Whats up' / 'whats
    # up' / 'watts up'. Only matches when followed by a name + saying, so
    # plain "What's up" greetings still hit small-talk above.
    _r(r"^whats?\s+up[,\s]+(.+?)[,\s]+(?:saying|with|that\s+says?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    _r(r"^watts?\s+up[,\s]+(.+?)[,\s]+(?:saying|with|that\s+says?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),

    # Telegram → MERCURY.telegram_send (Bot API direct, milliseconds)
    _r(r"^(?:send|telegram|tg)\s+(?:a\s+message\s+)?to\s+(.+?)\s+on\s+telegram"
       r"\s+(?:saying|with|telling)\s+(.+?)[.!?]?$",
       "telegram_send",
       lambda m: {"chat": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    _r(r"^telegram\s+(.+?)\s+(?:saying|with)\s+(.+?)[.!?]?$",
       "telegram_send",
       lambda m: {"chat": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    # @handle form — Telegram by default since that's where users prefix
    # handles with @. Allow optional prefix words between the verb and @
    # so "send hello to @bob saying X" / "tell @bob saying X" both match.
    # Catches the session-log case "send hello to @my_odin_bot saying ...".
    _r(r"^(?:send|message|tell|dm|notify|ping)"
       r"(?:\s+[^@]+?)?"            # optional 'hello', 'a message', 'to', etc.
       r"\s+@(\w+(?:_\w+)*)"
       r"\s+(?:saying|with|telling\s+(?:them|him|her)?|that\s+says?)\s+(.+?)[.!?]?$",
       "telegram_send",
       lambda m: {"chat": "@" + m.group(1), "message": _clean_msg(m.group(2))}),
    # Verbless "send X to @handle" — no "saying", message is the prefix word(s).
    _r(r"^(?:send|tell|notify)\s+(.+?)\s+to\s+@(\w+(?:_\w+)*)[.!?]?$",
       "telegram_send",
       lambda m: {"chat": "@" + m.group(2), "message": _clean_msg(m.group(1))}),

    # Instagram → MERCURY.instagram_send (best-effort). Comma-tolerant +
    # filler-tolerant between platform and 'saying'. The bug case was
    # "...on Insta, DM him saying fuck you" — the ', DM him' between
    # 'Insta' and 'saying' broke the previous strict regex.
    _r(r"^(?:send|dm|instagram)[,\s]+(?:a\s+(?:dm|message)\s+)?to[,\s]+@?([\w.]+)"
       r"[,\s]+on[,\s]+(?:insta|instagram)"
       r"(?:[,\s]+(?:dm|message|text|note)(?:\s+(?:them|him|her|me|us|the\s+person))?)*"
       r"[,\s]+(?:saying|with|telling|that\s+says?)[,\s]+(.+?)[.!?]?$",
       "instagram_send",
       lambda m: {"handle": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    _r(r"^(?:dm|message)[,\s]+@?([\w.]+)[,\s]+on[,\s]+(?:insta|instagram)"
       r"(?:[,\s]+(?:dm|message|text|note)(?:\s+(?:them|him|her|me|us|the\s+person))?)*"
       r"[,\s]+(?:saying|with|telling|that\s+says?)?[,\s]*(.+?)[.!?]?$",
       "instagram_send",
       lambda m: {"handle": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),
    # "Send a message to X in my DMs saying Y" — Instagram is the
    # dominant "DMs" platform; user-test session confirmed this is
    # what the user means by "in my DMs".
    _r(r"^(?:send|deliver|shoot|drop|write|text)[,\s]+(?:a\s+)?"
       r"(?:message|msg|text|note|dm|line)?\s*"
       r"to[,\s]+(.+?)[,\s]+(?:in|via|through)\s+(?:my\s+|the\s+)?(?:insta\s+)?dms?"
       r"[,\s]+(?:saying|with|telling|that\s+says?)[,\s]+(.+?)[.!?]?$",
       "instagram_send",
       lambda m: {"handle": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),

    # Generic fallback — other platforms (Discord/Slack/Teams/Signal/etc) → ARGUS
    _r(r"^(?:send|message|deliver|shoot|drop)\s+(?:a\s+)?(?:message|msg|text|note|line)?\s*"
       r"to\s+(.+?)\s+on\s+(messenger|discord|slack|teams|signal|linkedin)"
       r"\s+(?:saying|that\s+says?|with|telling\s+(?:them|him|her|\w+)?)\s+(.+?)[.!?]?$",
       "browse",
       lambda m: {"task": (
           f"Open {m.group(2).lower()} Web. Find the chat / conversation with "
           f"'{_clean_arg(m.group(1))}'. Send the message: \"{_clean_arg(m.group(3))}\". "
           f"Confirm when sent. confirmed."
       )}),

    # Platform-LESS message — "send a message to X saying Y" with no platform
    # named. Defaults to WhatsApp, the user's dominant channel (every send in
    # the history was WhatsApp). MUST stay last in the messaging block so all
    # the explicit-platform routes above claim their cases first; the required
    # "saying/with/telling" body keeps it from grabbing non-message commands.
    _r(r"^(?:send|message|text|shoot|drop|write)\s+(?:a\s+)?"
       r"(?:message|msg|text|note|line)?\s*to\s+(.+?)"
       r"[,\s]+(?:saying|that\s+says?|with|telling\s+(?:them|him|her|\w+)?)[,\s]+(.+?)[.!?]?$",
       "whatsapp_send",
       lambda m: {"contact": _clean_arg(m.group(1)), "message": _clean_msg(m.group(2))}),

    # === Cloud research (SARASWATI) ===============================
    # Calls Claude in the background, caches the result to the Obsidian
    # vault. Future questions on the same topic answered offline by NABU.
    _r(r"^(?:deeply\s+)?(?:research|study|learn\s+about|teach\s+me\s+about)\s+(.+?)[.!?]?$",
       "deep_research", lambda m: {"topic": _clean_arg(m.group(1))}),
    _r(r"^ask\s+claude(?:\s+(?:about|for))?\s+(.+?)[.!?]?$",
       "ask_claude", lambda m: {"question": _clean_arg(m.group(1))}),

    # === Wikipedia (ATHENA.wiki_lookup) ===========================
    # Sub-second factual lookup, bypasses the LLM. Hits when user explicitly
    # invokes Wikipedia — vague "what is X" stays going to GIL for nuance.
    _r(r"^(?:wikipedia|wiki(?:\s+lookup)?)\s+(.+?)[.!?]?$",
       "wiki_lookup", lambda m: {"topic": _clean_arg(m.group(1))}),
    _r(r"^(?:look\s+up|search)\s+(.+?)\s+on\s+wikipedia[.!?]?$",
       "wiki_lookup", lambda m: {"topic": _clean_arg(m.group(1))}),
    # "what's the capital of France" / "capital of France" — route via Wikipedia.
    # Sub-second answer beats 1B's "System start" hallucination.
    _r(r"^(?:what(?:'?s|\s+is)\s+(?:the\s+)?)?capital\s+(?:of\s+|city\s+of\s+)(.+?)[.!?]?$",
       "wiki_lookup", lambda m: {"topic": f"capital of {_clean_arg(m.group(1))}"}),
    # "who is X" / "who was X" — biographical → Wikipedia
    _r(r"^who\s+(?:is|was|were)\s+(.+?)[.!?]?$",
       "wiki_lookup", lambda m: {"topic": _clean_arg(m.group(1))}),

    # === Jokes (LOKI.tell_joke) ===================================
    _r(r"^(?:tell\s+me\s+)?(?:a\s+)?joke[.!?]?$|^make\s+me\s+laugh[.!?]?$",
       "tell_joke"),

    # === Compose notes (NABU.compose_note) ========================
    # Single-call pattern: searches vault, formats, writes to file, opens.
    # Replaces the multi-LLM-step flow that used to take 2-3 minutes on CPU.
    # Examples that hit:
    #   "write notes about cypher"
    #   "save everything you know about quantum"
    #   "compose a note on odin"
    #   "draft notes about my latest project"
    _r(r"^(?:write|save|note|draft|compose|put|make|jot)"
       # 0-2 filler tokens: "a note", "me a memo", "down notes", "stuff" etc.
       r"(?:\s+(?:me\s+)?(?:a|an|the|some)?\s*(?:notes?|something|everything|all|stuff|memo|down|piece)){0,2}\s+"
       r"(?:(?:you\s+know\s+)?(?:about|on|regarding))\s+(.+?)"
       r"(?:\s+(?:to|in|into|onto)\s+.+?)?[.!?]?$",
       "compose_note",
       lambda m: {"topic": _clean_arg(m.group(1))}),
    # Variation: "write everything you know about X"
    _r(r"^(?:write|save|note|draft|compose)\s+(?:what|everything|all)\s+(?:you\s+know\s+)?(?:about|on|regarding)\s+(.+?)[.!?]?$",
       "compose_note",
       lambda m: {"topic": _clean_arg(m.group(1))}),

    # === Vault search (NABU) — EXPLICIT phrasings only ===========
    # NB: vault hits are already injected as GIL context for every multi-word
    # command (see _handle), so most "what is X" / "tell me about X" questions
    # already get vault-grounded answers via the LLM. These fast-routes are
    # for when the user explicitly wants the raw notes back, not a synthesized
    # answer. The earlier broader pattern caught "what is your name" too.
    _r(r"^search\s+(?:my\s+)?(?:vault|notes)\s+for\s+(.+?)[.!?]?$",
       "search_vault",
       lambda m: {"query": _clean_arg(m.group(1)), "limit": 3}),
    _r(r"^(?:what|which)\s+notes?\s+(?:do\s+I\s+have|are\s+there)\s+(?:on|about|for)\s+(.+?)[.!?]?$",
       "search_vault",
       lambda m: {"query": _clean_arg(m.group(1)), "limit": 5}),

    # === Listen-mode / persona quick toggles =====================
    _r(r"^(?:tap|hotkey|push[- ]to[- ]talk|ptt)\s+mode\.?$",
       "set_listen_mode", lambda m: {"mode": "hotkey"}),
    _r(r"^(?:always|continuous|wake[- ]?word)\s+(?:listen|listening)?\s*mode\.?$",
       "set_listen_mode", lambda m: {"mode": "always"}),
    _r(r"\b(?:set|change|raise|increase|boost|lower|decrease|drop|turn\s+up|turn\s+down)"
       r"\s+(?:the\s+)?brightness\s+(?:to\s+)?(\d+)\s*(?:percent)?\b", "set_brightness",
       lambda m: {"level": int(m.group(1))}),
    _r(r"\bbrightness\s+(?:to\s+)?(\d+)\s*(?:percent)?\b", "set_brightness",
       lambda m: {"level": int(m.group(1))}),
    _r(r"\b(?:set|change|raise|increase|boost|lower|decrease|drop|turn\s+up|turn\s+down)"
       r"\s+(?:the\s+)?volume\s+(?:to\s+)?(\d+)\s*(?:percent)?\b", "set_volume",
       lambda m: {"level": int(m.group(1))}),
    _r(r"\bvolume\s+(?:to\s+)?(\d+)\s*(?:percent)?\b",                      "set_volume",
       lambda m: {"level": int(m.group(1))}),
    _r(r"^(?:what(?:'?s| is)|get|tell me|check)\s+(?:the\s+)?(?:current\s+)?volume\b", "get_volume",
       lambda m: {}),
]


class Heimdall:
    """
    HEIMDALL listens. Always-on mode: detects 'Hey ODIN', records the command.
    Hotkey mode: records while a push-to-talk key is held.
    Common commands are dispatched directly, bypassing the LLM.
    """

    def __init__(self, config: dict, marduk, gil, thoth, merlin, loki, iris, vesta=None, nabu=None):
        h_cfg = config.get("heimdall", {})
        v_cfg = config.get("vesta", {})
        self.wake_phrases = config["odin"]["wake_phrases"]
        self.energy_threshold = h_cfg.get("energy_threshold", 0.005)
        self.silence_duration = h_cfg.get("silence_duration", 1.5)
        self.wake_chunk_sec = h_cfg.get("wake_chunk_seconds", 1.5)
        self.wake_hop_sec = h_cfg.get("wake_hop_seconds", 0.75)
        self.device = h_cfg.get("device", "cpu")
        self.compute_type = h_cfg.get("compute_type", "int8")
        self.noise_reduce = h_cfg.get("noise_reduce", True)
        # Input mic — None/empty = system default; int = device index; str = name substring.
        self.input_device = self._resolve_input_device(h_cfg.get("input_device"))
        vad_aggr = int(h_cfg.get("vad_aggressiveness", 2))
        if _HAS_VAD:
            self.vad = webrtcvad.Vad(max(0, min(3, vad_aggr)))
        else:
            self.vad = None
            print("[HEIMDALL] webrtcvad not installed — voice-activity gating disabled.")
        if not _HAS_NR:
            print("[HEIMDALL] noisereduce not installed — spectral denoise disabled.")

        # Clap-to-wake — alternative non-verbal trigger. Detector reads every
        # mic hop in parallel with wake-word transcription; when the configured
        # clap pattern fires, we take the same wake path as "Hey Sage". Built
        # in input/clap.py — pure DSP, ~10 ms per hop, no model files.
        self.clap_wake_enabled = bool(h_cfg.get("clap_wake_enabled", True))
        if self.clap_wake_enabled:
            try:
                from input.clap import ClapDetector
                self.clap_detector = ClapDetector(
                    sample_rate=SAMPLE_RATE,
                    pattern=h_cfg.get("clap_pattern", "double"),
                    peak_threshold=h_cfg.get("clap_peak_threshold", 0.18),
                    rms_ratio=h_cfg.get("clap_rms_ratio", 7.0),
                    hf_ratio=h_cfg.get("clap_hf_ratio", 0.35),
                    cooldown_sec=h_cfg.get("clap_cooldown_sec", 1.2),
                )
                print(f"[HEIMDALL] clap-to-wake online "
                      f"(pattern={h_cfg.get('clap_pattern', 'double')}).")
            except Exception as e:
                print(f"[HEIMDALL] clap detector init failed: {e}")
                self.clap_detector = None
        else:
            self.clap_detector = None

        self.marduk = marduk
        self.gil = gil
        self.thoth = thoth
        self.merlin = merlin
        self.loki = loki
        self.iris = iris
        self.vesta = vesta
        self.nabu = nabu

        self._busy = threading.Event()
        self._listen_mode = v_cfg.get("listen_mode", "always")
        self._record_event = threading.Event()
        self._stop = threading.Event()
        # ASGARD (and any other UI) subscribes via add_listening_listener so
        # the throne-room can flash the listening pulse during wake recording.
        self._state_listeners: list = []
        # Set on barge-in to abort the in-flight GIL.think() stream.
        self._gil_stop = threading.Event()
        # Chain steps parked by a mid-chain barge-in; "resume" picks them up.
        self._pending_chain: list[str] = []
        # Generation counter — bumped each time a new handler takes over so
        # the previous (interrupted) handler's `finally` knows not to clear
        # state that now belongs to the new handler.
        self._handle_lock = threading.Lock()
        self._handle_gen = 0
        # Debug mode: when true, every wake-buffer transcription dumps the
        # raw audio + transcribed text to data/debug/wake/<timestamp>.wav so
        # the user can play back what Whisper actually heard.
        self._debug_audio = bool(h_cfg.get("debug_audio", False))
        self._debug_dir = "data/debug/wake"
        if self._debug_audio:
            import os as _os
            _os.makedirs(self._debug_dir, exist_ok=True)
            print(f"[HEIMDALL] Audio debug ON — dumping wake clips to {self._debug_dir}")

        wake_model_name = h_cfg.get("wake_model", "tiny.en")
        stt_model_name = h_cfg.get("stt_model", "base.en")
        # Command languages. ["en"] = English-only. More than one entry turns
        # on per-utterance language detection (see _detect_language), which
        # requires a multilingual stt_model — "small", not "small.en". The
        # wake path is unaffected: the wake word is English, wake_model stays .en.
        langs = h_cfg.get("stt_languages") or ["en"]
        self.stt_languages = [str(l).strip().lower() for l in langs if str(l).strip()] or ["en"]
        if len(self.stt_languages) > 1 and stt_model_name.endswith(".en"):
            print(f"[HEIMDALL] stt_languages={self.stt_languages} needs a multilingual "
                  f"stt_model, but '{stt_model_name}' is English-only. "
                  f"Set stt_model: \"small\" in config.yaml. Using English only.")
            self.stt_languages = ["en"]
        self._wake_multilingual = not wake_model_name.endswith(".en")
        self._cmd_multilingual = not stt_model_name.endswith(".en")
        print(f"[HEIMDALL] Loading wake detector ({wake_model_name})...")
        self.wake_model = WhisperModel(
            wake_model_name, device=self.device, compute_type=self.compute_type
        )
        if stt_model_name == wake_model_name:
            print(f"[HEIMDALL] Command transcriber sharing wake model ({stt_model_name}).")
            self.cmd_model = self.wake_model
        else:
            # Async load — boot returns immediately; stt loads on a daemon
            # thread. If a wake fires before stt is ready, _transcribe falls
            # back to wake_model (lower quality but better than waiting).
            self.cmd_model = self.wake_model  # fallback until async load completes
            self._cmd_model_target = stt_model_name
            self._cmd_model_loaded = threading.Event()
            def _load_cmd():
                print(f"[HEIMDALL] Loading command transcriber ({stt_model_name}) in background...")
                t0 = time.perf_counter()
                try:
                    cmd = WhisperModel(
                        stt_model_name, device=self.device, compute_type=self.compute_type
                    )
                    self.cmd_model = cmd
                    self._cmd_model_loaded.set()
                    print(f"[HEIMDALL] Command transcriber ready ({time.perf_counter()-t0:.1f}s).")
                except Exception as e:
                    print(f"[HEIMDALL] Command transcriber load failed: {e}. Falling back to wake model.")
            threading.Thread(target=_load_cmd, daemon=True, name="HEIMDALL-stt-load").start()

        # ── openWakeWord: dedicated keyword-spotting net (Siri-grade) ──
        # When enabled and importable, it REPLACES the Whisper rolling-window
        # wake matching with a purpose-built detector. Falls back cleanly to
        # the Whisper path if the package or model can't load.
        self._oww = None
        self._custom = None            # custom-trained KWS head (train_wake_word.py)
        self._custom_buf = np.zeros(0, dtype=np.int16)
        self._oww_carry = np.zeros(0, dtype=np.int16)
        self._wake_engine = str(h_cfg.get("wake_engine", "openwakeword")).lower()
        self._oww_name = h_cfg.get("oww_model", "hey_jarvis")
        self._oww_threshold = float(h_cfg.get("oww_threshold", 0.5))

        if self._wake_engine == "custom":
            # A model trained by train_wake_word.py: openWakeWord's feature
            # extractor + a small scikit-learn head. Lets ODIN wake to a truly
            # custom phrase like "hey odin".
            model_path = h_cfg.get("custom_wake_model", "data/wakewords/hey_odin.pkl")
            try:
                import joblib
                from openwakeword.utils import AudioFeatures
                bundle = joblib.load(model_path)
                self._custom = {
                    "clf": bundle["clf"],
                    "threshold": float(bundle.get("threshold", 0.8)),
                    "phrase": bundle.get("phrase", "your phrase"),
                    "window": int(bundle.get("window_samples", 32000)),
                    "af": AudioFeatures(),
                }
                self._custom_buf = np.zeros(self._custom["window"], dtype=np.int16)
                print(f"[HEIMDALL] Wake engine: custom — say \"{self._custom['phrase']}\" "
                      f"(threshold {self._custom['threshold']:.2f}).")
            except Exception as e:
                print(f"[HEIMDALL] custom wake model unavailable ({e}). "
                      f"Train one with train_wake_word.py or set wake_engine: openwakeword. "
                      f"Falling back to Whisper on {self.wake_phrases}.")
                self._custom = None
                self._wake_engine = "whisper"

        if self._wake_engine == "openwakeword":
            try:
                import openwakeword
                from openwakeword.model import Model as _OWWModel
                # Custom path vs pretrained name. Pretrained models download
                # once to the openwakeword package dir (no-op if present).
                if os.path.sep in self._oww_name or self._oww_name.endswith(".onnx"):
                    oww_arg = [self._oww_name]
                else:
                    try:
                        openwakeword.utils.download_models([self._oww_name])
                    except Exception:
                        pass
                    oww_arg = [self._oww_name]
                self._oww = _OWWModel(wakeword_models=oww_arg, inference_framework="onnx")
                spoken = os.path.splitext(os.path.basename(self._oww_name))[0].replace("_", " ")
                print(f"[HEIMDALL] Wake engine: openWakeWord — say \"{spoken}\" "
                      f"(threshold {self._oww_threshold}).")
            except Exception as e:
                print(f"[HEIMDALL] openWakeWord unavailable ({e}). "
                      f"Falling back to Whisper wake matching on {self.wake_phrases}.")
                self._oww = None
                self._wake_engine = "whisper"

        print(f"[HEIMDALL] Watchman online. Listen mode: {self._listen_mode}.")

    # === Public API ===
    def set_listen_mode(self, mode: str):
        self._listen_mode = mode
        print(f"[HEIMDALL] Listen mode → {mode}")

    def trigger_record_start(self):
        self._record_event.set()

    def trigger_record_stop(self):
        self._record_event.clear()

    # === Helpers ===
    def add_listening_listener(self, fn):
        """Register a callable(is_listening:bool) called whenever HEIMDALL
        transitions in/out of the 'listening' state. ASGARD uses this to
        flash the listening pulse indicator. No-op if fn isn't callable."""
        if callable(fn):
            self._state_listeners.append(fn)

    def _notify(self, state: str):
        if self.vesta:
            try:
                self.vesta.set_state(state)
            except Exception:
                pass
        # Fan out to subscribers (ASGARD). Pass a bool so listeners that only
        # care about listening on/off can stay simple. Errors must never break
        # the perception loop.
        is_listening = (state == "listening")
        for fn in getattr(self, "_state_listeners", ()):
            try:
                fn(is_listening)
            except Exception:
                pass

    def _resolve_input_device(self, value):
        """value can be None, int (device index), or str (name substring)."""
        if value is None or value == "":
            try:
                idx = sd.default.device[0]
                name = sd.query_devices(idx)["name"]
                print(f"[HEIMDALL] Mic: default device [{idx}] '{name}'")
            except Exception:
                pass
            return None
        if isinstance(value, int):
            try:
                name = sd.query_devices(value)["name"]
                print(f"[HEIMDALL] Mic: device [{value}] '{name}'")
            except Exception as e:
                print(f"[HEIMDALL] Mic device {value} unusable ({e}); falling back to default.")
                return None
            return value
        # String — substring match against device names.
        target = str(value).lower()
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) > 0 and target in d["name"].lower():
                    print(f"[HEIMDALL] Mic: device [{i}] '{d['name']}' (matched '{value}')")
                    return i
        except Exception:
            pass
        print(f"[HEIMDALL] No mic matches '{value}'; falling back to default.")
        return None

    def _is_wake_word(self, text: str) -> bool:
        t = text.lower().strip()
        # Word-boundary match. Bare substring would fire "sage" inside
        # "message", "passage", "massage" — observed in the wild.
        for p in self.wake_phrases:
            if re.search(r"\b" + re.escape(p) + r"\b", t):
                return True
        # Fuzzy fallback for "sage" mishears ("saje", "sayge"). Already
        # word-bounded by the regex itself.
        return bool(_FUZZY_WAKE.search(t))

    def _oww_detect(self, hop: np.ndarray) -> bool:
        """Feed a float32 audio hop to openWakeWord as contiguous 80ms /
        1280-sample int16 frames (its native rate). A leftover carry buffer
        keeps the frame stream continuous across hops so the phrase isn't
        sliced mid-word. Returns True when the wake score crosses threshold,
        and resets the model so one utterance fires exactly once."""
        if self._oww is None:
            return False
        pcm = (np.clip(hop, -1.0, 1.0) * 32767.0).astype(np.int16)
        if self._oww_carry.size:
            pcm = np.concatenate([self._oww_carry, pcm])
        step = 1280
        n = (len(pcm) // step) * step
        fired = False
        for i in range(0, n, step):
            try:
                scores = self._oww.predict(pcm[i:i + step])
            except Exception as e:
                print(f"[HEIMDALL] openWakeWord predict failed: {e}")
                return False
            if float(scores.get(self._oww_name, 0.0)) >= self._oww_threshold:
                fired = True
        self._oww_carry = pcm[n:].copy()
        if fired:
            try:
                self._oww.reset()
            except Exception:
                pass
            self._oww_carry = np.zeros(0, dtype=np.int16)
        return fired

    def _custom_detect(self, hop: np.ndarray) -> bool:
        """Custom-trained wake head: keep a rolling window of int16 audio,
        embed it with openWakeWord's extractor, and score it with the small
        classifier from train_wake_word.py. Fires once per crossing."""
        if self._custom is None:
            return False
        c = self._custom
        pcm = (np.clip(hop, -1.0, 1.0) * 32767.0).astype(np.int16)
        buf = np.concatenate([self._custom_buf, pcm])[-c["window"]:]
        self._custom_buf = buf
        if len(buf) < c["window"]:
            return False
        try:
            emb = np.array(c["af"].embed_clips(buf[None, :].astype(np.int16)))
            feat = emb.reshape(1, -1)
            prob = float(c["clf"].predict_proba(feat)[0, 1])
        except Exception as e:
            print(f"[HEIMDALL] custom wake predict failed: {e}")
            return False
        # Require TWO consecutive high-scoring windows. A real utterance spans
        # several 0.5s hops, so it clears this easily; stray single-frame
        # false-positives on other speech don't.
        if prob >= c["threshold"]:
            self._custom_hits = getattr(self, "_custom_hits", 0) + 1
        else:
            self._custom_hits = 0
        if self._custom_hits >= 2:
            self._custom_hits = 0
            self._custom_buf = np.zeros(c["window"], dtype=np.int16)   # cooldown
            return True
        return False

    def _dump_audio(self, audio: np.ndarray, transcribed: str):
        """Save the wake buffer as a 16kHz WAV with the transcription as filename
        prefix, so the user can listen to what Whisper saw vs. what they said."""
        import os as _os
        import wave as _wave
        ts = time.strftime("%H%M%S")
        # Sanitize the transcription for filesystem use — keep first 40 chars.
        safe = re.sub(r"[^a-z0-9 ]+", "", transcribed.lower())[:40].strip().replace(" ", "_") or "blank"
        path = _os.path.join(self._debug_dir, f"{ts}_{safe}.wav")
        try:
            pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
            with _wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm16)
        except Exception as e:
            print(f"[HEIMDALL] debug dump failed: {e}")

    def _is_barge_word(self, text: str) -> bool:
        """Strict wake match for barge-in. Requires a prefix word ('hey', 'hi',
        'okay') so we don't self-trigger when ODIN says 'Odin' through the
        speakers — he never says 'hey odin' to himself."""
        t = text.lower().strip()
        # Exact match against multi-word wake phrases.
        for p in self.wake_phrases:
            if " " in p and p in t:
                return True
        # Fuzzy: "hey/hi/okay" + any 'odin' mishear ("oden", "ode in", etc.).
        return bool(_FUZZY_BARGE.search(t))

    def _record_command(self) -> np.ndarray:
        # 30ms frames so we can use webrtcvad directly per frame.
        frame_samples = int(0.03 * SAMPLE_RATE)  # 480 at 16kHz
        max_frames = int(15.0 / 0.03)            # hard cap: 15s recording
        # End the recording after this many consecutive non-voice frames.
        silence_frames = int(self.silence_duration / 0.03)
        # Don't end before the user has actually started speaking — wait for a
        # short voiced run first so a brief pre-speech pause doesn't terminate.
        warmup_voice_frames = 3   # 90ms of voice = considered started
        warmup_frames_max = int(2.0 / 0.03)  # but give up if no voice in 2s

        frames = []
        voice_started = False
        warmup_voiced = 0
        consecutive_silent = 0
        warmup_total = 0

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=self.input_device) as stream:
            for _ in range(max_frames):
                chunk, _ = stream.read(frame_samples)
                chunk = chunk.flatten()
                frames.append(chunk)

                energy = float(np.abs(chunk).mean())
                voiced = _is_voice_frame(chunk, self.vad, SAMPLE_RATE) and energy >= self.energy_threshold

                if not voice_started:
                    warmup_total += 1
                    if voiced:
                        warmup_voiced += 1
                        if warmup_voiced >= warmup_voice_frames:
                            voice_started = True
                    elif warmup_total >= warmup_frames_max and warmup_voiced == 0:
                        # No speech detected at all in the first 2 seconds — bail.
                        break
                else:
                    if voiced:
                        consecutive_silent = 0
                    else:
                        consecutive_silent += 1
                        if consecutive_silent >= silence_frames:
                            break

        audio = np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)
        # Trim trailing silence BEFORE optional denoise so we don't waste
        # spectral analysis on dead air. Decouples the pause-tolerance
        # window (silence_duration in config) from what Whisper actually sees.
        audio = _trim_trailing_silence(audio, self.vad, SAMPLE_RATE)
        if self.noise_reduce:
            audio = _denoise(audio, SAMPLE_RATE)
        audio = _normalize_audio(audio)
        return audio

    def _record_while_held(self) -> np.ndarray:
        frames = []
        chunk_size = int(0.1 * SAMPLE_RATE)
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", device=self.input_device) as stream:
            while self._record_event.is_set():
                chunk, _ = stream.read(chunk_size)
                frames.append(chunk.flatten())
        if not frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(frames)

    def _detect_language(self, audio: np.ndarray, model: WhisperModel) -> str:
        """Pick the most likely command language among stt_languages.

        Whisper routinely labels spoken Hindi as Urdu (same spoken language,
        different script), so when Hindi is allowed and Urdu isn't, Urdu's
        probability counts toward Hindi — otherwise real Hindi commands lose
        the argmax to a language we'd transcribe in the wrong script anyway.
        """
        fallback = self.stt_languages[0]
        if audio is None or len(audio) == 0:
            return fallback
        try:
            _, _, all_probs = model.detect_language(audio)
            probs = dict(all_probs or [])
            if "hi" in self.stt_languages and "ur" not in self.stt_languages:
                probs["hi"] = probs.get("hi", 0.0) + probs.pop("ur", 0.0)
            return max(self.stt_languages, key=lambda l: probs.get(l, 0.0))
        except Exception as e:
            print(f"[HEIMDALL] language detect failed ({e}); using '{fallback}'.")
            return fallback

    def _transcribe_command(self, audio: np.ndarray) -> str:
        """Command-path transcription: picks the language per utterance when
        multiple stt_languages are configured AND the active model is
        multilingual. During the async-load window cmd_model is still the
        (English-only) wake model, so detection is skipped until small is up."""
        model = self.cmd_model
        multilingual = (self._cmd_multilingual if model is not self.wake_model
                        else self._wake_multilingual)
        language = self.stt_languages[0]
        if multilingual and len(self.stt_languages) > 1:
            language = self._detect_language(audio, model)
            if language != "en":
                print(f"[HEIMDALL] command language: {language}")
        return self._transcribe(audio, model, vad_filter=False, language=language)

    def _transcribe(self, audio: np.ndarray, model: WhisperModel,
                    prompt: str = None, vad_filter: bool = True,
                    language: str = "en") -> str:
        if audio is None or len(audio) == 0:
            return ""
        # vad_filter=True (default, used for WAKE) uses Silero VAD to exclude
        # non-speech segments before transcription. Reduces 'thanks for
        # watching' ghost transcripts on otherwise empty clips.
        # vad_filter=False (used for COMMAND) lets every word through. The
        # silero VAD was clipping legitimate quiet words ("for me") out of
        # commands. We rely on _trim_trailing_silence (caller-side) to
        # remove dead air before reaching Whisper, so ghost suppression
        # comes from clean audio rather than VAD.
        try:
            if vad_filter:
                segments, _ = model.transcribe(
                    audio, language=language, beam_size=1, initial_prompt=prompt,
                    vad_filter=True,
                    vad_parameters=dict(
                        min_silence_duration_ms=500,
                        threshold=0.35,  # silero speech threshold (default 0.5) — lower = more permissive about classifying audio as speech
                    ),
                )
            else:
                segments, _ = model.transcribe(
                    audio, language=language, beam_size=1, initial_prompt=prompt,
                )
        except TypeError:
            # Older faster-whisper without vad_filter — fall back gracefully.
            segments, _ = model.transcribe(audio, language=language, beam_size=1, initial_prompt=prompt)
        kept = []
        for s in segments:
            if getattr(s, "no_speech_prob", 0.0) > 0.6:
                continue
            kept.append(s.text)
        text = " ".join(kept).strip()
        # Trim trailing YouTube-style Whisper hallucinations. Two passes:
        #   1. If any of the multi-word ghost patterns appears anywhere in
        #      the text, cut from the START of the match. This catches
        #      "Set volume to 50 thanks for watching" → "Set volume to 50",
        #      not "Set volume to" (which the old n-word loop produced).
        #   2. If the very last word is a CLEAR YT-only hallucination
        #      ("thanks", "subscribe", "applause"...), strip just that word.
        #      Common English words ("you", "the", "next") are preserved —
        #      they legitimately end real commands.
        if text:
            m = _GHOST_PATTERNS.search(text)
            if m and m.start() > 0:
                text = text[: m.start()].rstrip(" ,.")
            if text:
                words = text.split()
                if words:
                    last_clean = _GHOST_STRIP_RE.sub("", words[-1].lower()).strip()
                    if last_clean in _SAFE_TO_TRIM_ALONE:
                        text = " ".join(words[:-1]).rstrip(" ,.")
        return text

    def _strip_wake_phrase(self, text: str) -> str:
        t = text.lower()
        for phrase in self.wake_phrases:
            if t.startswith(phrase):
                return text[len(phrase):].strip(" ,.")
        return text

    def _try_multistep_fast_route(self, command: str) -> tuple[str | None, str | None]:
        """For multi-step commands like 'find the invoice and summarize it',
        split on conjunctions and fast-route each part IN ORDER (later parts
        may depend on earlier ones via working memory).

        Returns (spoken, remainder):
          (text, None)      — every part routed; speak text, done. No LLM.
          (text, remainder) — a routable PREFIX executed (its effects are in
                              working memory); caller speaks text then hands
                              only the remainder to GIL. Chains are no longer
                              all-or-nothing.
          (None, None)      — the FIRST part already needs the LLM; caller
                              sends the whole command to GIL (old behavior).

        Interrupt-aware: if the user barges in mid-chain (_gil_stop), the
        un-run parts are parked in self._pending_chain — 'resume' picks
        them back up."""
        parts = [p.strip().rstrip(".!?,;: ").strip() for p in re.split(
            r"\s+\b(?:and(?:\s+then)?|then|after\s+that)\b\s+",
            command.strip(),
            flags=re.I,
        ) if p.strip()]
        if len(parts) < 2:
            return (None, None)
        results = []
        for i, part in enumerate(parts):
            # Barge-in between steps: park the rest for "resume".
            if i > 0 and self._gil_stop.is_set():
                self._pending_chain = parts[i:]
                print(f"[HEIMDALL] chain interrupted — {len(self._pending_chain)} step(s) parked; say 'resume'.")
                break
            result = self._try_fast_route(part)
            if result is None:
                if i == 0:
                    return (None, None)   # first step needs the LLM — whole → GIL
                remainder = " and then ".join(parts[i:])
                return (" ".join(results), remainder)
            results.append(result)
        if not results:
            return (None, None)
        return (" ".join(results), None)

    def _try_fast_route(self, command: str) -> str | None:
        cmd = command.strip()
        # Identity question -> hand-written response (NOT the 1B LLM, which
        # was leaking the prompt template back as the answer).
        if _IDENTITY_QUESTION.search(cmd):
            persona = "mythic"
            if self.loki:
                try:
                    persona = self.loki.execute("get_persona", {}) or "mythic"
                except Exception:
                    pass
            return _pick_identity_response(persona)
        # Capability question -> answer directly without LLM
        if _CAPABILITY_QUESTION.search(cmd):
            return _CAPABILITIES_LINE
        # Small talk -> hardcoded reply, no module dispatch
        for pattern, reply in _SMALL_TALK:
            if pattern.match(cmd):
                return reply
        for pattern, skill, args_fn in _FAST_ROUTES:
            m = pattern.search(cmd)
            if not m:
                continue
            try:
                args = args_fn(m)
                result = self.marduk.dispatch(skill, args)
                if result and not result.startswith("MARDUK"):
                    return result
            except Exception as e:
                print(f"[HEIMDALL] Fast route error on '{skill}': {e}")
        return None

    def _handle(self, command: str):
        with self._handle_lock:
            self._handle_gen += 1
            my_gen = self._handle_gen

        if not command or len(command) < 2:
            with self._handle_lock:
                if my_gen == self._handle_gen:
                    self._notify("idle")
                    self._busy.clear()
            return

        # L1.2: per-phase timing. Anchored at the moment _handle gets the
        # transcribed command. Reports a single line at end-of-turn so we
        # can see where seconds went: vault, gil-first, total.
        t_handle_start = time.perf_counter()
        t_phases = {"fast_route": 0.0, "vault": 0.0, "gil_first": 0.0, "gil_total": 0.0}

        interrupted = False
        # Spelled-out letters: "K-A-R-T-I-K" → "Kartik". When the user spells
        # a name letter-by-letter (usually after Whisper misheard it), treat
        # the spelled version as authoritative.
        normalized = _normalize_spelled_letters(command)
        if normalized != command:
            print(f"[HEIMDALL] Spelled-letters normalized: {normalized!r}")
            command = normalized

        # Phonetic corrections — fix STT mishearings the user has taught us
        # ("Karthik" → "Kartik"). Runs after spelled-letter normalization so
        # the spelled-out version (more reliable) wins when both are present.
        if self.marduk:
            prom = self.marduk.get_module("PROMETHEUS")
            if prom and hasattr(prom, "get_phonetic_corrections"):
                try:
                    corrections = prom.get_phonetic_corrections()
                    if corrections:
                        corrected = _apply_phonetic_corrections(command, corrections)
                        if corrected != command:
                            print(f"[HEIMDALL] Phonetic-corrected: {corrected!r}")
                            command = corrected
                except Exception:
                    pass

        # If the user mid-utterance reversed themselves ("Open Chrome, no,
        # forget it, open Firefox"), keep only the post-cancellation part.
        stripped = _strip_self_corrections(command)
        if stripped != command:
            print(f"[HEIMDALL → ODIN] {command}")
            print(f"[HEIMDALL] Self-correction detected — using: {stripped!r}")
            command = stripped
        else:
            print(f"[HEIMDALL → ODIN] {command}")
        # Normalize spoken email addresses ("X at the rate Y dot com" → "X@Y.com")
        normalized = _normalize_email_speech(command)
        if normalized != command:
            print(f"[HEIMDALL] Email-speech normalized: {normalized!r}")
            command = normalized
        # Normalize spoken URLs ("https colon slash slash X dot Y dot com" → "https://X.Y.com").
        normalized = _normalize_url_speech(command)
        if normalized != command:
            print(f"[HEIMDALL] URL-speech normalized: {normalized!r}")
            command = normalized
        # Normalize spoken handles ("my underscore odin underscore bot" → "my_odin_bot",
        # "at the rate X" → "@X" when X looks like a handle not a domain).
        normalized = _normalize_handle_speech(command)
        if normalized != command:
            print(f"[HEIMDALL] Handle-speech normalized: {normalized!r}")
            command = normalized
        # Strip redundant 'and send it' / 'then send' tails on email/message
        # intents — the compose flow already shows a Send button.
        stripped = _strip_send_followup(command)
        if stripped != command:
            print(f"[HEIMDALL] Send-followup stripped: {stripped!r}")
            command = stripped
        self.thoth.execute("store_message", {"role": "user", "content": command})
        if self.nabu:
            try:
                self.nabu.execute("mirror_message", {"role": "user", "content": command})
            except Exception:
                pass
        # Tier-1 self-learning — scan the user's message for self-disclosed
        # facts ("I prefer X", "my favorite Y is Z") and store them in
        # PROMETHEUS so MERLIN can surface them as context next time.
        if self.marduk:
            prom = self.marduk.get_module("PROMETHEUS")
            if prom and hasattr(prom, "auto_learn_from_message"):
                try:
                    learned = prom.auto_learn_from_message(command)
                    if learned:
                        print(f"[PROMETHEUS] Auto-learned: {', '.join(learned)}")
                except Exception:
                    pass

        # Resume a chain the user interrupted mid-way ("resume" / "finish the
        # task"). The parked steps rejoin as a normal command and flow through
        # the same multi-step machinery below.
        if self._pending_chain and re.match(
                r"^(?:resume(?:\s+(?:the\s+)?(?:task|chain))?|"
                r"finish\s+(?:the|your)\s+(?:task|chain)|"
                r"continue\s+the\s+(?:task|chain)|"
                r"pick\s+up\s+where\s+you\s+left\s+off)[.!?]?$",
                command, re.I):
            command = " and then ".join(self._pending_chain)
            self._pending_chain = []
            print(f"[HEIMDALL] Resuming parked chain: {command!r}")

        try:
            multi_step = _is_multi_step(command)
            t = time.perf_counter()
            if multi_step:
                # Split on "and"/"then" and fast-route each part in order.
                fast, chain_remainder = self._try_multistep_fast_route(command)
                if fast is not None and chain_remainder:
                    # A routable prefix already executed (its effects live in
                    # working memory). Speak it, then hand ONLY the remainder
                    # to the LLM path below.
                    print(f"[FAST-PATH] chain prefix done — remainder → GIL: {chain_remainder!r}")
                    self._notify("speaking")
                    self.iris.speak(fast)
                    self.thoth.execute("store_message", {"role": "assistant", "content": fast})
                    command = chain_remainder
                    fast = None
                elif fast is not None:
                    print("[FAST-PATH] multi-step split — all parts on fast-route")
            else:
                fast = self._try_fast_route(command)
            t_phases["fast_route"] = time.perf_counter() - t
            if fast is not None:
                print(f"[FAST-PATH] {fast}")
                self._notify("speaking")
                self.iris.speak(fast)
                self.thoth.execute("store_message", {"role": "assistant", "content": fast})
                if self.nabu:
                    try:
                        self.nabu.execute("mirror_message", {"role": "assistant", "content": fast})
                    except Exception:
                        pass
                self.iris.wait_idle()
                return

            # LEARNED ROUTES — trajectory learning. After a successful
            # semantic-route dispatch, we cache (utterance → skill+args).
            # Subsequent identical or near-identical phrasings hit this
            # cache and skip the cloud call entirely. This is how ODIN
            # learns from use (ruflo lesson, ODIN-shaped).
            hermes_for_learn = (self.marduk.get_module("HERMES")
                                if self.marduk else None)
            if not _looks_complex(command) and not _looks_conversational(command):
                cached = _check_learned_route(command, hermes_for_learn)
                if cached is not None:
                    skill, args = cached
                    if skill in self.marduk._skill_map:
                        print(f"[LEARNED-ROUTE] {skill}({args})")
                        self._notify("thinking")
                        try:
                            result = self.marduk.dispatch(skill, args)
                        except Exception as e:
                            result = ""
                            print(f"[HEIMDALL] Learned-route dispatch failed: {e}")
                        if result and not result.startswith("MARDUK"):
                            self._notify("speaking")
                            self.iris.speak(result)
                            self.thoth.execute("store_message",
                                {"role": "assistant", "content": result})
                            if self.nabu:
                                try:
                                    self.nabu.execute("mirror_message",
                                        {"role": "assistant", "content": result})
                                except Exception:
                                    pass
                            self.iris.wait_idle()
                            return

            # SEMANTIC ROUTE — paraphrase tolerance. Fast-route regex missed,
            # but the command might still want a specific skill. Cloud LLM
            # picks best skill+args from a top-K (by HERMES embedding) skill
            # catalogue. ~1s instead of 30-90s GIL stall. Skipped for pure
            # chat / explanation queries which go to ask_ai below.
            if (not _looks_complex(command)
                    and not _looks_conversational(command)
                    and self.marduk):
                sara_for_route = self.marduk.get_module("SARASWATI")
                if sara_for_route and getattr(sara_for_route, "providers", None):
                    t_sem = time.perf_counter()
                    picked = _semantic_route(command, self.marduk, sara_for_route)
                    t_phases["fast_route"] += time.perf_counter() - t_sem
                    if picked is not None:
                        skill, args = picked
                        print(f"[SEMANTIC-ROUTE] {skill}({args})")
                        self._notify("thinking")
                        try:
                            result = self.marduk.dispatch(skill, args)
                        except Exception as e:
                            result = f"Semantic route dispatch failed: {e}"
                        if result and not result.startswith("MARDUK"):
                            # SUCCESS — cache it so the next identical
                            # phrasing skips the cloud call.
                            try:
                                _record_learned_route(command, skill, args,
                                                       hermes_for_learn)
                            except Exception as e:
                                print(f"[HEIMDALL] learned-route save failed: {e}")
                            self._notify("speaking")
                            self.iris.speak(result)
                            self.thoth.execute("store_message",
                                {"role": "assistant", "content": result})
                            if self.nabu:
                                try:
                                    self.nabu.execute("mirror_message",
                                        {"role": "assistant", "content": result})
                                except Exception:
                                    pass
                            self.iris.wait_idle()
                            return

            # Multi-step always needs tools so GIL can chain calls; otherwise let
            # the action-verb heuristic decide.
            needs_tools = multi_step or _likely_needs_tools(command)

            # Cloud auto-route: explanation/comparison/"tell me about" style
            # questions AND casual conversational openers ("I'm bored", "do
            # you think...", "are you alive") go to SARASWATI instead of
            # local 1B. Conversational queries are why ODIN previously felt
            # robotic — 1B replies were stalls or boilerplate. Cloud Gemini
            # gives a real human-feeling response in 1-2 seconds.
            if (not needs_tools
                    and (_looks_complex(command) or _looks_conversational(command))
                    and self.marduk):
                sara = self.marduk.get_module("SARASWATI")
                if sara and getattr(sara, "providers", None):
                    print(f"[GIL] Complex query — routing to cloud (SARASWATI.ask_ai)")
                    self._notify("thinking")
                    try:
                        reply = sara.execute("ask_ai", {"question": command})
                    except Exception as e:
                        reply = ""
                        print(f"[HEIMDALL] Cloud route failed: {e}")
                    # If cloud responded with a real answer, use it and skip GIL.
                    # Any of the SARASWATI error strings means cloud was
                    # unavailable — fall back to local 1B below.
                    if reply and not reply.startswith((
                        "No cloud", "Need a question", "[SARASWATI]",
                        "Claude API", "Claude is not"
                    )):
                        with self._handle_lock:
                            is_current = (my_gen == self._handle_gen)
                        if is_current:
                            self._notify("speaking")
                            self.iris.speak(reply)
                            self.thoth.execute("store_message",
                                {"role": "assistant", "content": reply})
                            if self.nabu:
                                try:
                                    self.nabu.execute("mirror_message",
                                        {"role": "assistant", "content": reply})
                                except Exception:
                                    pass
                            self.iris.wait_idle()
                        return

            if multi_step:
                print(f"[GIL] Multi-step detected — bypassing fast-route, tools=on")
            print(f"[GIL] Thinking... (tools={'on' if needs_tools else 'off'})")
            self._notify("thinking")

            messages = self.thoth.execute("get_messages", {})
            context = ""
            if self.merlin:
                context = self.merlin.execute("get_context", {})
            if self.loki:
                tone = self.loki.execute("get_tone", {})
                if tone:
                    context = f"{context} | Tone: {tone}".strip(" |")
            # Prepend the long-term summary if THOTH has compressed old
            # turns. Lets GIL know what happened 50+ turns ago without
            # those turns sitting in the active prompt (saves context
            # budget on 8GB CPU).
            if hasattr(self.thoth, "get_summary"):
                summary = self.thoth.get_summary()
                if summary:
                    context = (f"Earlier context summary:\n{summary}\n\n{context}").strip()
            # Auto-augment with vault recall — RAG over the user's Obsidian notes
            # and ODIN's own memoized lookups. Skip for trivial 1-word commands.
            if self.nabu and len(command.split()) >= 2:
                t = time.perf_counter()
                try:
                    hits = self.nabu.execute("search_vault", {"query": command, "limit": 2})
                    if hits and not hits.startswith(("Empty", "No searchable", "Nothing in vault", "[NABU]")):
                        context = (context + "\n\nFrom your notes:\n" + hits)[:2400]
                except Exception:
                    pass
                t_phases["vault"] = time.perf_counter() - t

            t_gil_start = time.perf_counter()
            spoke_first = [False]
            def stream_speak(s):
                # If a barge-in / PTT-interrupt happened, _handle_gen was
                # bumped. Our captured `my_gen` no longer matches — we've
                # been superseded. Drop the stale sentence on the floor;
                # do NOT call iris.speak (which would re-queue speech for
                # the OLD response after the user explicitly interrupted).
                if my_gen != self._handle_gen:
                    return
                if not spoke_first[0]:
                    self._notify("speaking")
                    t_phases["gil_first"] = time.perf_counter() - t_gil_start
                    spoke_first[0] = True
                self.iris.speak(s)

            persona = "mythic"
            if self.loki:
                try:
                    persona = self.loki.execute("get_persona", {}) or "mythic"
                except Exception:
                    persona = "mythic"

            # Watchdog: tracks WIRE activity, not "first spoken sentence".
            # Multi-step (tool-calling) queries can legitimately take 60s+
            # without producing a spoken sentence, but they stream chunks
            # the whole time (tool_calls, intermediate text). We only want
            # to fire if the wire goes truly silent for 30s — a real stall.
            watchdog_stop = self._gil_stop
            last_progress = [time.perf_counter()]
            def on_progress():
                last_progress[0] = time.perf_counter()
            def _watchdog():
                # 90s "no progress" threshold. on_progress is called both
                # per-chunk AND between LLM iterations (when a new stream
                # is opened, and after each tool dispatch). A real wedge is
                # the only way to go 90s without ANY of those events firing.
                # Tool chains can legitimately take 2-3 minutes total but
                # individual gaps stay under 90s thanks to the heartbeat.
                while True:
                    time.sleep(0.5)
                    if watchdog_stop.is_set() or t_phases["gil_total"] > 0:
                        return
                    silent_for = time.perf_counter() - last_progress[0]
                    if silent_for > 90.0:
                        print(f"[HEIMDALL] GIL watchdog fired — no progress for {silent_for:.0f}s. Aborting.")
                        watchdog_stop.set()
                        return
            threading.Thread(target=_watchdog, daemon=True).start()

            response, _, interrupted = self.gil.think(
                messages if isinstance(messages, list) else [],
                context=context,
                on_sentence=stream_speak,
                use_tools=needs_tools,
                persona=persona,
                stop_event=self._gil_stop,
                on_progress=on_progress,
            )
            t_phases["gil_total"] = time.perf_counter() - t_gil_start
            # Three "no tokens reached the user" cases handled here:
            #   1. Watchdog tripped (interrupted=True, no tokens): speak the
            #      generic stall explanation.
            #   2. GIL returned a non-empty `response` but never streamed via
            #      `on_sentence` (e.g. httpx.ReadTimeout produced an error
            #      string instead of token chunks): speak that error.
            #   3. Stream succeeded normally → already spoken via stream_speak.
            # IMPORTANT: skip all of this if we've been superseded by a
            # PTT-interrupt or barge-in. The user already moved on; we
            # mustn't speak stale fallback messages over the new handler.
            if not spoke_first[0] and my_gen == self._handle_gen:
                if interrupted:
                    self.iris.speak("The brain stalled out. I gave up after thirty seconds.")
                    self._gil_stop.clear()
                elif response and response.strip():
                    self.iris.speak(response)
            # Don't persist a half-finished response — barge-in means the user
            # didn't want it. THOTH and the vault stay in sync with what was actually heard.
            if response and not interrupted:
                self.thoth.execute("store_message", {"role": "assistant", "content": response})
                if self.nabu:
                    try:
                        self.nabu.execute("mirror_message", {"role": "assistant", "content": response})
                    except Exception:
                        pass
            if interrupted:
                print("[HEIMDALL] LLM stream interrupted by barge-in.")
            self.iris.wait_idle()
        finally:
            total = time.perf_counter() - t_handle_start
            # One-line timing breakdown so latency regressions are visible.
            # gil_first = "time to first spoken sentence" — this is what the
            # user actually feels. The L1 target is ≤ 3s.
            print(
                f"[TIMING] total={total:.2f}s "
                f"fast_route={t_phases['fast_route']*1000:.0f}ms "
                f"vault={t_phases['vault']*1000:.0f}ms "
                f"gil_first={t_phases['gil_first']:.2f}s "
                f"gil_total={t_phases['gil_total']:.2f}s"
            )
            time.sleep(_ECHO_TAIL_SECONDS)
            # Only clear if we're still the "current" handler. If a barge-in
            # spawned a successor, leave busy alone — the successor owns it.
            with self._handle_lock:
                if my_gen == self._handle_gen:
                    self._notify("idle")
                    self._busy.clear()

    def start(self):
        if self._listen_mode == "hotkey":
            self._hotkey_loop()
        else:
            self._wake_word_loop()

    def _wake_word_loop(self):
        # Continuous callback-driven capture. The audio device fills a queue on
        # its own thread; we pull from the queue and process. This is critical:
        # the previous sd.rec() polling loop stopped recording while Whisper
        # transcribed, leaving 80-160ms silent gaps in every 2.5s window. Whisper
        # then hallucinated short single-syllable words ("ouch", "inch") instead
        # of the actual wake word.
        window_samples = int(self.wake_chunk_sec * SAMPLE_RATE)
        hop_samples = int(self.wake_hop_sec * SAMPLE_RATE)
        buffer = np.zeros(window_samples, dtype=np.float32)
        self._notify("idle")

        audio_q: "queue.Queue[np.ndarray]" = queue.Queue()

        def _on_audio(indata, frames, time_info, status):
            if status:
                # Underflow / overflow — print so future driver issues are visible.
                print(f"[HEIMDALL] audio status: {status}")
            try:
                audio_q.put(indata[:, 0].copy())
            except Exception as e:
                # Callback errors can't propagate cleanly; log and let the
                # main-loop reconnect path pick it up via stale-queue detection.
                print(f"[HEIMDALL] audio callback error: {e}")

        def _open_stream():
            s = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=hop_samples, device=self.input_device,
                callback=_on_audio,
            )
            s.start()
            return s

        # Open with retries — mic might be in use by another app at boot.
        stream = None
        for attempt in range(5):
            try:
                stream = _open_stream()
                break
            except Exception as e:
                print(f"[HEIMDALL] Mic open attempt {attempt+1}/5 failed: {e}")
                time.sleep(2 ** attempt)
        if stream is None:
            print("[HEIMDALL] Mic unreachable after 5 attempts. Push-to-talk via VESTA still works.")
            return

        # Track consecutive callback errors — if we see 3 in a row, mic likely
        # disconnected; stop+reopen with backoff. Without this, a unplug locks
        # the wake loop in a callback-storm.
        last_audio_err_ts = [0.0]
        consecutive_errs = [0]

        try:
            while not self._stop.is_set():
                # Push-to-talk override — Alt key (via VESTA._on_tap) sets
                # _record_event. Works in two modes:
                #   - busy=False: skip wake-word, go straight to recording.
                #   - busy=True: INTERRUPT the in-flight LLM stream + cut TTS,
                #     then start recording. Otherwise PTT is delayed until
                #     ODIN finishes responding (could be 10-30s).
                if self._record_event.is_set():
                    self._record_event.clear()
                    if self._busy.is_set():
                        print("[HEIMDALL] Push-to-talk INTERRUPT (Alt during response).")
                        with self._handle_lock:
                            self._handle_gen += 1
                        self._gil_stop.set()
                        self.iris.stop()
                    else:
                        print("[HEIMDALL] Push-to-talk triggered (Alt).")
                    self._busy.set()
                    self._notify("listening")
                    self.iris.play_acknowledge()
                    stream.stop()
                    try:
                        cmd_audio = self._record_command()
                    finally:
                        stream.start()
                        with audio_q.mutex:
                            audio_q.queue.clear()
                    full_text = self._transcribe_command(cmd_audio)
                    command = self._strip_wake_phrase(full_text)
                    self._gil_stop.clear()
                    buffer = np.zeros(window_samples, dtype=np.float32)
                    threading.Thread(
                        target=self._handle, args=(command,), daemon=True
                    ).start()
                    continue

                try:
                    hop = audio_q.get(timeout=0.3)
                    consecutive_errs[0] = 0
                except queue.Empty:
                    consecutive_errs[0] += 1
                    # 5 consecutive empty 0.3s waits = 1.5s of total silence
                    # from the mic, while iris is NOT speaking. Either the
                    # mic was unplugged or the driver wedged. Reopen.
                    if consecutive_errs[0] >= 5 and not self.iris.is_speaking():
                        print("[HEIMDALL] Mic appears stalled — reopening stream.")
                        try:
                            stream.stop(); stream.close()
                        except Exception:
                            pass
                        for attempt in range(5):
                            try:
                                stream = _open_stream()
                                print("[HEIMDALL] Mic reconnected.")
                                consecutive_errs[0] = 0
                                break
                            except Exception as e:
                                print(f"[HEIMDALL] Reopen attempt {attempt+1}/5: {e}")
                                time.sleep(2 ** attempt)
                        else:
                            # All retries failed — sleep longer and try again on next loop.
                            time.sleep(10)
                    continue

                # Backpressure: if Whisper falls behind (queue >5 hops = 2.5s of
                # buffered audio), drop the oldest hops to catch up. Keeps the
                # rolling window pinned to "now" rather than playing catch-up.
                while audio_q.qsize() > 5:
                    try:
                        hop = audio_q.get_nowait()
                    except queue.Empty:
                        break

                # While ODIN is speaking, drain stale audio and skip transcription
                # entirely. We can't reliably tell echo (~0.04-0.07 energy) from
                # real barge-in (~0.10+) in software — both look like loud speech
                # to webrtcvad. Voice barge-in is sacrificed for clean behavior;
                # use the GUI/tray hotkey to interrupt ODIN if needed. The queue
                # drain keeps the rolling buffer pinned to "now" once he stops.
                if self.iris.is_speaking():
                    with audio_q.mutex:
                        audio_q.queue.clear()
                    buffer = np.zeros(window_samples, dtype=np.float32)
                    continue

                # Clap-to-wake — runs BEFORE the Whisper transcription gate so
                # we don't waste a forward pass on what is just a couple of
                # broadband transients. Fires only on the configured pattern
                # (double-clap by default) with a built-in cooldown so a third
                # nearby clap doesn't re-trigger.
                if (self.clap_detector is not None
                        and not self._busy.is_set()
                        and self.clap_detector.add_hop(hop)):
                    print("[HEIMDALL] Clap-to-wake fired.")
                    self._busy.set()
                    self._notify("listening")
                    self.iris.play_acknowledge()
                    stream.stop()
                    try:
                        cmd_audio = self._record_command()
                    finally:
                        stream.start()
                        with audio_q.mutex:
                            audio_q.queue.clear()
                    full_text = self._transcribe_command(cmd_audio)
                    command = self._strip_wake_phrase(full_text)
                    self._gil_stop.clear()
                    buffer = np.zeros(window_samples, dtype=np.float32)
                    threading.Thread(
                        target=self._handle, args=(command,), daemon=True
                    ).start()
                    continue

                # ── Neural wake path (openWakeWord or custom-trained head) ──
                # A keyword-spotting net (the Siri/Alexa approach) scores audio
                # directly — no Whisper, no rolling-window transcription. While
                # busy we skip (use Alt / tray to interrupt ODIN), matching the
                # Whisper path's "no mic barge-in" behavior.
                if self._oww is not None or self._custom is not None:
                    # Only score when idle — no point detecting (or burning CPU
                    # embedding frames) while ODIN is already handling a turn.
                    detected = False
                    if not self._busy.is_set():
                        detected = (self._oww_detect(hop) if self._oww is not None
                                    else self._custom_detect(hop))
                    if detected:
                        label = self._oww_name if self._oww is not None else self._custom["phrase"]
                        print(f"[HEIMDALL] Wake detected ({label}).")
                        self._busy.set()
                        self._notify("listening")
                        self.iris.play_acknowledge()
                        stream.stop()
                        try:
                            cmd_audio = self._record_command()
                        finally:
                            stream.start()
                            with audio_q.mutex:
                                audio_q.queue.clear()
                        full_text = self._transcribe_command(cmd_audio)
                        command = self._strip_wake_phrase(full_text)
                        threading.Thread(
                            target=self._handle, args=(command,), daemon=True
                        ).start()
                    continue

                buffer = np.concatenate([buffer[hop_samples:], hop])

                energy = float(np.abs(hop).mean())
                if energy < self.energy_threshold:
                    continue
                if not _has_voice(hop, self.vad, SAMPLE_RATE):
                    continue

                # Normalize only — NOT denoise. NO initial_prompt either.
                audio_for_stt = _normalize_audio(buffer)
                wake_text = self._transcribe(audio_for_stt, self.wake_model)
                if self._debug_audio:
                    self._dump_audio(buffer, wake_text)

                # Drop Whisper ghost transcripts ("thanks for watching", "you",
                # "...") — they bury real wake events in console spam.
                if _is_whisper_ghost(wake_text):
                    continue

                # Barge-in path
                if self._busy.is_set():
                    if self._is_barge_word(wake_text):
                        print(f"[HEIMDALL] BARGE-IN heard: '{wake_text}'")
                        with self._handle_lock:
                            self._handle_gen += 1
                        self._gil_stop.set()
                        self.iris.stop()
                        self._notify("listening")
                        # Pause the wake stream while _record_command opens its
                        # own InputStream — sounddevice can't drive two on the
                        # same device on most Windows drivers.
                        stream.stop()
                        try:
                            cmd_audio = self._record_command()
                        finally:
                            stream.start()
                            with audio_q.mutex:
                                audio_q.queue.clear()
                        full_text = self._transcribe_command(cmd_audio)
                        command = self._strip_wake_phrase(full_text)
                        self._gil_stop.clear()
                        buffer = np.zeros(window_samples, dtype=np.float32)
                        threading.Thread(
                            target=self._handle, args=(command,), daemon=True
                        ).start()
                    continue

                print(f"[HEIMDALL] heard: '{wake_text}' (energy={energy:.4f})")
                if self._is_wake_word(wake_text):
                    print("[HEIMDALL] Wake word detected.")
                    self._busy.set()
                    self._notify("listening")
                    self.iris.play_acknowledge()

                    stream.stop()
                    try:
                        cmd_audio = self._record_command()
                    finally:
                        stream.start()
                        with audio_q.mutex:
                            audio_q.queue.clear()
                    full_text = self._transcribe_command(cmd_audio)
                    command = self._strip_wake_phrase(full_text)
                    buffer = np.zeros(window_samples, dtype=np.float32)
                    threading.Thread(
                        target=self._handle, args=(command,), daemon=True
                    ).start()
        except KeyboardInterrupt:
            print("[HEIMDALL] Watchman going offline.")
            self.iris.speak_blocking("ODIN going offline. Goodbye.")
            self._stop.set()
        except Exception as e:
            print(f"[HEIMDALL] Error: {e}")
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _hotkey_loop(self):
        """Tap mode: single hotkey tap starts a listen session that ends on silence."""
        self._notify("idle")
        print("[HEIMDALL] Tap mode — press the hotkey once to speak.")

        while not self._stop.is_set():
            try:
                if not self._record_event.wait(timeout=0.5):
                    continue
                # Consume the tap immediately so the next press will re-trigger
                self._record_event.clear()
                if self._busy.is_set():
                    continue

                self._busy.set()
                self._notify("listening")
                self.iris.play_acknowledge()

                # Record until silence — same logic as the wake-word path
                audio = self._record_command()
                command = self._transcribe_command(audio)
                threading.Thread(
                    target=self._handle, args=(command,), daemon=True
                ).start()

            except KeyboardInterrupt:
                print("[HEIMDALL] Watchman going offline.")
                self.iris.speak_blocking("ODIN going offline. Goodbye.")
                self._stop.set()
                break
            except Exception as e:
                print(f"[HEIMDALL] Error: {e}")
