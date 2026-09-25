# IRIS — Greek — goddess of communication and messages
# Voice output: text to speech, tone, delivery — fully offline, queue-driven

import os
import re
import time
import wave
import queue
import tempfile
import threading
import numpy as np
import pygame
from core.marduk import OdinModule


# === TTS sanitization ===============================================
# Piper and SAPI both speak every character literally — including
# **asterisks**, ## hashtags, and `backticks`. We strip Markdown
# decorations (and Obsidian wiki-links) before sending to TTS.
_RE_CODE_BLOCK = re.compile(r"```\w*\n?(.*?)\n?```", re.DOTALL)
_RE_INLINE_CODE = re.compile(r"`([^`]+)`")
_RE_WIKI_LINK = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+))?\]\]")
_RE_MD_LINK = re.compile(r"\[([^\]]+?)\]\([^)]+?\)")
_RE_HEADER = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_RE_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_RE_BOLD = re.compile(r"\*\*([^*]+?)\*\*|__([^_]+?)__")
_RE_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
_RE_NEWLINES = re.compile(r"\n+")
_RE_SPACES = re.compile(r"\s+")

# Devanagari block — any such character routes the sentence to the Hindi
# piper voice (when loaded). Hinglish written in Latin script intentionally
# stays on the default English voice (U+0900–U+097F).
_RE_DEVANAGARI = re.compile("[\\u0900-\\u097F]")


def _sanitize_for_tts(text: str) -> str:
    """Strip Markdown / wiki-link decorations so the synthesizer doesn't
    read them aloud as 'asterisk asterisk bold asterisk asterisk'.

    Order matters: code blocks before inline code; wiki-links/md-links
    before bold/italic so brackets are stripped first.
    """
    if not text:
        return ""
    # Code: keep the content, drop the fences/ticks
    text = _RE_CODE_BLOCK.sub(lambda m: " code: " + m.group(1).strip() + " ", text)
    text = _RE_INLINE_CODE.sub(r"\1", text)
    # Obsidian wiki-links: [[Notes/X.md]] or [[Notes/X|alias]] → "X" or alias
    def _wikilink(m):
        target, alias = m.group(1), m.group(2)
        if alias:
            return alias
        name = target.split("/")[-1]
        if name.lower().endswith(".md"):
            name = name[:-3]
        return name.replace("-", " ").replace("_", " ")
    text = _RE_WIKI_LINK.sub(_wikilink, text)
    # Markdown links [text](url) → "text"
    text = _RE_MD_LINK.sub(r"\1", text)
    # Headers — drop the # prefix
    text = _RE_HEADER.sub("", text)
    # Bullets — drop the marker, keep the content
    text = _RE_BULLET.sub("", text)
    # Emphasis — keep inner text
    text = _RE_BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _RE_ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    # Whitespace cleanup
    text = _RE_NEWLINES.sub(" ", text)
    text = _RE_SPACES.sub(" ", text)
    return text.strip()

try:
    from piper.voice import PiperVoice
    _HAS_PIPER = True
except ImportError:
    try:
        from piper import PiperVoice  # type: ignore
        _HAS_PIPER = True
    except ImportError:
        _HAS_PIPER = False

try:
    import pyttsx3
    _HAS_PYTTSX3 = True
except ImportError:
    _HAS_PYTTSX3 = False


class Iris(OdinModule):
    MODULE_NAME = "IRIS"
    LAYER = "OUTPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("iris", {})
        self.engine_name = "none"
        self.piper_voice = None
        self.piper_voice_hi = None
        self.pyttsx_engine = None
        # indic-nlp Hindi normalizer: None=not yet loaded, False=unavailable,
        # else the normalizer object. Lazy-loaded on first Hindi sentence.
        self._hi_normalizer = None

        voice_path = cfg.get("piper_voice_path", "")
        if _HAS_PIPER and voice_path and os.path.exists(voice_path):
            try:
                self.piper_voice = PiperVoice.load(voice_path)
                self.engine_name = "piper"
                print(f"[IRIS] Voice synthesizer ready (piper: {os.path.basename(voice_path)}).")
            except Exception as e:
                print(f"[IRIS] Piper load failed: {e}. Falling back.")

        # Optional second piper voice for Hindi. Sentences containing
        # Devanagari are routed to it per-sentence (see _voice_for), so a
        # mixed conversation switches voices mid-stream without any config.
        voice_hi_path = cfg.get("piper_voice_hi_path", "")
        if self.engine_name == "piper" and voice_hi_path:
            if os.path.exists(voice_hi_path):
                try:
                    self.piper_voice_hi = PiperVoice.load(voice_hi_path)
                    print(f"[IRIS] Hindi voice ready (piper: {os.path.basename(voice_hi_path)}).")
                except Exception as e:
                    print(f"[IRIS] Hindi piper load failed: {e}. Hindi text will use the default voice.")
            else:
                print(f"[IRIS] Hindi voice missing at {voice_hi_path} — "
                      f"run 'python setup_voice.py hi_IN-pratham-medium'.")

        if self.engine_name == "none" and _HAS_PYTTSX3:
            self.pyttsx_engine = pyttsx3.init()
            self.pyttsx_engine.setProperty("rate", cfg.get("sapi_rate", 185))
            self.pyttsx_engine.setProperty("volume", cfg.get("sapi_volume", 1.0))
            sapi_voice = cfg.get("sapi_voice", "")
            if sapi_voice:
                for v in self.pyttsx_engine.getProperty("voices"):
                    if sapi_voice.lower() in v.name.lower():
                        self.pyttsx_engine.setProperty("voice", v.id)
                        break
            self.engine_name = "pyttsx3"
            print("[IRIS] Voice synthesizer ready (pyttsx3 / Windows SAPI).")
            if not _HAS_PIPER or not voice_path or not os.path.exists(voice_path):
                print("[IRIS] Tip: run 'python setup_voice.py' for higher-quality offline voice.")

        if self.engine_name == "none":
            print("[IRIS] WARNING: No TTS engine available. Install piper-tts or pyttsx3.")

        pygame.mixer.init()
        self._tts_queue: "queue.Queue[str]" = queue.Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._tts_loop, daemon=True)
        self._worker.start()

        # Speak listeners — ASGARD subscribes so the throne-room UI can
        # animate "speaking" + surface the line in its blue answer panel.
        # Each listener is called synchronously with the sanitized line;
        # exceptions in a listener never break TTS.
        self._on_speak_listeners: list = []
        # Mouth-level listeners (speech-energy). Fed amplitude floats in
        # [0.0, 1.0] at ~20 Hz during piper playback — ASGARD uses them to
        # throb the sun's corona with ODIN's voice. See _stream_amplitudes.
        self._mouth_level_listeners: list = []

    def add_speak_listener(self, fn):
        """Register a callable(text:str) to be notified whenever IRIS
        enqueues a sentence. Used by ASGARD to drive UI state."""
        if callable(fn):
            self._on_speak_listeners.append(fn)

    # Speech-energy stream. Subscribers receive a float 0.0..1.0 every ~50 ms
    # during piper playback, normalized so the loudest sample in the utterance
    # == 1.0. A final 0.0 fires when playback ends so the UI settles back to
    # idle. pyttsx3 path cannot expose the audio buffer (Windows SAPI plays
    # directly), so under that engine these listeners receive nothing and the
    # UI's CSS-keyframe fallback drives a generic "speaking" animation.
    def add_mouth_level_listener(self, fn):
        """Register a callable(level: float in [0.0, 1.0]) to receive audio
        amplitude updates during piper playback."""
        if callable(fn):
            self._mouth_level_listeners.append(fn)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "speak_text",
                "description": "Speak a message out loud using text to speech",
                "parameters": {
                    "text": {"type": "string", "description": "Text to speak"}
                },
                "required": ["text"],
                "internal_only": True
            },
            {
                "name": "list_sapi_voices",
                "description": "List available Windows SAPI voices on this system",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        try:
            if skill_name == "speak_text":
                self.speak(args.get("text", ""))
                return "spoken"
            if skill_name == "list_sapi_voices":
                return self._list_sapi_voices()
        except Exception as e:
            return f"[IRIS] Error: {e}"
        return f"[IRIS] Unknown skill: {skill_name}"

    def speak(self, text: str):
        """Non-blocking — enqueues text for the TTS worker.

        Markdown decorations are stripped before queueing so we don't speak
        'asterisk asterisk bold asterisk asterisk' or 'hashtag hashtag'.
        The console print keeps the original (with markdown) for debug visibility.
        """
        if not text or not text.strip():
            return
        print(f"[ODIN] {text}")
        spoken = _sanitize_for_tts(text)
        if not spoken:
            return
        # New speech overrides any prior stop signal — the user wants this said.
        self._stop_event.clear()
        self._tts_queue.put(spoken)
        # Fan out to UI listeners (e.g. ASGARD) so they animate before audio
        # actually plays. A misbehaving listener must never break TTS.
        for fn in self._on_speak_listeners:
            try:
                fn(spoken)
            except Exception as e:
                print(f"[IRIS] speak listener error: {e}")

    def stop(self):
        """Cut off ODIN mid-sentence. Drains the queue, stops current playback,
        and signals the worker to skip anything in flight. Used for barge-in."""
        self._stop_event.set()
        # Drain pending sentences without playing them.
        try:
            while True:
                self._tts_queue.get_nowait()
                self._tts_queue.task_done()
        except queue.Empty:
            pass
        # Stop the currently-playing wave file.
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass

    def speak_blocking(self, text: str):
        """Speak and wait for the queue to drain — use at shutdown."""
        self.speak(text)
        self.wait_idle()

    def wait_idle(self):
        """Block until all queued speech has finished playing."""
        self._tts_queue.join()

    def is_speaking(self) -> bool:
        """True iff something is currently playing OR queued. HEIMDALL uses
        this to mute its wake loop so ODIN doesn't transcribe his own voice
        bleeding back through the speakers."""
        try:
            if pygame.mixer.music.get_busy():
                return True
        except Exception:
            pass
        return not self._tts_queue.empty()

    def _tts_loop(self):
        while True:
            text = self._tts_queue.get()
            try:
                if self._stop_event.is_set():
                    # Barge-in fired between the put and our get — drop this sentence.
                    continue
                if self.engine_name == "piper":
                    self._speak_piper(text)
                elif self.engine_name == "pyttsx3":
                    self._speak_pyttsx3(text)
            except Exception as e:
                print(f"[IRIS] TTS error: {e}")
            finally:
                self._tts_queue.task_done()

    def _voice_for(self, text: str):
        """Pick the piper voice for this sentence — Hindi voice for
        Devanagari text when loaded, default voice otherwise."""
        if self.piper_voice_hi is not None and _RE_DEVANAGARI.search(text):
            return self.piper_voice_hi
        return self.piper_voice

    def _normalize_hi(self, text: str) -> str:
        """Canonicalize Devanagari before synthesis so piper pronounces it
        cleanly (variant nukta forms, stray combining marks, digit/space
        quirks from STT/LLM output). Uses indic-nlp's Hindi normalizer when
        available; a no-op if it isn't installed."""
        if not _RE_DEVANAGARI.search(text):
            return text
        if self._hi_normalizer is None:
            if self._hi_normalizer is False:
                return text
            try:
                from indicnlp.normalize.indic_normalize import IndicNormalizerFactory
                self._hi_normalizer = IndicNormalizerFactory().get_normalizer("hi")
            except Exception as e:
                print(f"[IRIS] indic-nlp normalizer unavailable ({e}); Hindi unnormalized.")
                self._hi_normalizer = False
                return text
        try:
            return self._hi_normalizer.normalize(text)
        except Exception:
            return text

    def _speak_piper(self, text: str):
        voice = self._voice_for(text)
        synth_text = self._normalize_hi(text) if voice is self.piper_voice_hi else text
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp = f.name
        try:
            with wave.open(tmp, "wb") as wf:
                voice.synthesize_wav(synth_text, wf)
            # Build the amplitude envelope BEFORE pygame opens the file —
            # we open it once for analysis, then hand it off for playback.
            envelope = self._compute_amplitude_envelope(tmp) if self._mouth_level_listeners else None

            pygame.mixer.music.load(tmp)
            pygame.mixer.music.play()

            # The amplitude streamer kicks off the moment playback starts so
            # the speech-energy track stays synced with the audio's rhythm.
            if envelope:
                threading.Thread(
                    target=self._stream_amplitudes,
                    args=(envelope,),
                    daemon=True,
                ).start()

            while pygame.mixer.music.get_busy():
                if self._stop_event.is_set():
                    pygame.mixer.music.stop()
                    break
                pygame.time.wait(50)
            try:
                pygame.mixer.music.unload()
            except Exception:
                pass
        finally:
            # Always fire a final 0.0 so subscribers settle back to idle
            # even on crashes / barge-ins mid-stream.
            for fn in self._mouth_level_listeners:
                try: fn(0.0)
                except Exception: pass
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _compute_amplitude_envelope(self, wav_path: str):
        """Read the WAV and return [(offset_seconds, normalized_level), ...]
        at ~20 Hz. Normalized so the loudest 50 ms window in the utterance
        maps to 1.0, so the UI's speech-energy animation is proportional to
        *this* sentence rather than absolute loudness."""
        try:
            with wave.open(wav_path, "rb") as wf:
                n_frames = wf.getnframes()
                sr = wf.getframerate()
                n_channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                raw = wf.readframes(n_frames)
        except Exception as e:
            print(f"[IRIS] envelope read failed: {e}")
            return []

        dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
        dtype = dtype_map.get(sample_width, np.int16)
        try:
            audio = np.frombuffer(raw, dtype=dtype)
        except Exception:
            return []
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels).mean(axis=1)
        max_val = float(np.iinfo(dtype).max) or 1.0
        audio = audio.astype(np.float32) / max_val

        window_sec = 0.05            # 20 Hz update rate
        window_samples = max(1, int(window_sec * sr))
        envelope = []
        for i in range(0, len(audio), window_samples):
            chunk = audio[i:i + window_samples]
            if len(chunk) == 0:
                break
            rms = float(np.sqrt(np.mean(chunk ** 2)))
            envelope.append((i / sr, rms))
        if not envelope:
            return []
        max_rms = max(r for _, r in envelope) or 1e-6
        # Apply a mild gamma so quiet passages still animate visibly.
        return [(t, (r / max_rms) ** 0.7) for t, r in envelope]

    def _stream_amplitudes(self, envelope):
        """Walk the precomputed envelope in playback time, fanning each level
        out to subscribers. Bails on stop_event for barge-in."""
        start_t = time.monotonic()
        listeners = list(self._mouth_level_listeners)   # snapshot
        for offset_sec, level in envelope:
            wait = offset_sec - (time.monotonic() - start_t)
            if wait > 0:
                if self._stop_event.wait(timeout=wait):
                    break   # barge-in
            elif self._stop_event.is_set():
                break
            for fn in listeners:
                try: fn(level)
                except Exception: pass

    def _speak_pyttsx3(self, text: str):
        self.pyttsx_engine.say(text)
        self.pyttsx_engine.runAndWait()

    def _list_sapi_voices(self) -> str:
        if not _HAS_PYTTSX3:
            return "pyttsx3 not installed."
        eng = self.pyttsx_engine or pyttsx3.init()
        names = [v.name for v in eng.getProperty("voices")]
        if not names:
            return "No SAPI voices found."
        return "Available voices: " + ", ".join(names)

    def play_acknowledge(self):
        sample_rate = 44100
        duration = 0.15
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        beep = (np.sin(2 * np.pi * 880 * t) * 0.6 * 32767).astype(np.int16)
        stereo = np.column_stack([beep, beep])
        sound = pygame.sndarray.make_sound(stereo)
        sound.play()
        pygame.time.wait(int(duration * 1000) + 100)
