# ODIN — wake pipeline test rig
# Records a single 5-second clip, runs the SAME normalize + base.en pipeline
# the wake loop uses, and prints what each stage produced.
#
# Usage:
#   python test_wake.py
# Then say the wake word from config.yaml (currently "Sage") cleanly
# during the recording window.

import sys
import time
import wave
import numpy as np
import sounddevice as sd
import yaml

from input.heimdall import _normalize_audio, _denoise, _FUZZY_WAKE
from faster_whisper import WhisperModel


def _wake_match(text: str, phrases: list) -> bool:
    """Same logic as Heimdall._is_wake_word — substring or fuzzy regex."""
    t = text.lower().strip()
    if any(p in t for p in phrases):
        return True
    return bool(_FUZZY_WAKE.search(t))

DURATION = 5
SR = 16000
OUT = "wake_test.wav"


def main():
    config = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    h_cfg = config.get("heimdall", {})
    wake_phrases = config["odin"]["wake_phrases"]
    device = h_cfg.get("input_device") or None

    print(f"Loading wake model ({h_cfg.get('wake_model', 'base.en')})...")
    model = WhisperModel(
        h_cfg.get("wake_model", "base.en"),
        device=h_cfg.get("device", "cpu"),
        compute_type=h_cfg.get("compute_type", "int8"),
    )

    print(f"Recording {DURATION}s. Say '{wake_phrases[0]}' clearly when prompted...")
    for i in range(3, 0, -1):
        print(f"  starting in {i}...", end="\r", flush=True)
        time.sleep(1)
    print("  RECORDING NOW            ")
    audio = sd.rec(
        int(DURATION * SR), samplerate=SR, channels=1,
        dtype="float32", device=device, blocking=True,
    ).flatten()
    print("  done.")

    # Save raw
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    with wave.open(OUT, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
        wf.writeframes(pcm16)
    print(f"Saved raw clip: {OUT}")

    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio ** 2)))
    print(f"\n=== Stage 1: capture ===")
    print(f"  peak={peak:.4f}  rms={rms:.4f}")

    # Stage 2: normalize (what the wake path does)
    norm = _normalize_audio(audio)
    print(f"\n=== Stage 2: normalize ===")
    print(f"  peak={float(np.max(np.abs(norm))):.4f}  rms={float(np.sqrt(np.mean(norm**2))):.4f}")

    # Stage 3a: transcribe RAW (no preprocessing)
    print(f"\n=== Stage 3a: transcribe raw audio ===")
    segs, _ = model.transcribe(audio, language="en", beam_size=1)
    raw_text = " ".join(s.text for s in segs).strip()
    print(f"  -> {raw_text!r}")

    # Stage 3b: transcribe normalized
    print(f"\n=== Stage 3b: transcribe normalized audio ===")
    segs, _ = model.transcribe(norm, language="en", beam_size=1)
    norm_text = " ".join(s.text for s in segs).strip()
    print(f"  -> {norm_text!r}")

    # Stage 3c: transcribe normalized + denoised (what command path does)
    den = _denoise(audio, SR)
    den = _normalize_audio(den)
    segs, _ = model.transcribe(den, language="en", beam_size=1)
    den_text = " ".join(s.text for s in segs).strip()
    print(f"\n=== Stage 3c: transcribe denoise+normalize ===")
    print(f"  -> {den_text!r}")

    # Stage 4: matchers
    print(f"\n=== Stage 4: wake matcher ===")
    for label, text in [("raw", raw_text), ("norm", norm_text), ("denoise+norm", den_text)]:
        wake = _wake_match(text, wake_phrases)
        print(f"  {label:14s}: wake={wake}  text={text!r}")

    print(f"\nVerdict:")
    if any(_wake_match(t, wake_phrases) for t in (raw_text, norm_text, den_text)):
        print(f"  Pipeline DOES detect '{wake_phrases[0]}' on a clean 5s clip.")
        print("  Wake-loop misses are caused by the 1.5s rolling buffer fragmenting")
        print("  your utterance — bumping wake_chunk_seconds will help.")
    else:
        if rms < 0.02:
            print("  Audio too quiet for the model. Bump Windows mic Boost / use a closer mic.")
        else:
            print("  Pipeline FAILS even on a clean 5s clip. Try a different wake_model")
            print("  (try 'small.en' next), or check that wake_test.wav actually contains your voice.")


if __name__ == "__main__":
    main()
