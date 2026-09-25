# ODIN — wake-word picker
# Records you saying each candidate wake word, transcribes with base.en,
# shows which ones Whisper handles cleanly for YOUR voice. Pick the one
# that transcribes correctly every time.
#
# Usage:
#   python pick_wake.py
#
# Edit CANDIDATES below if you want to test different words.

import sys
import time
import wave
import numpy as np
import sounddevice as sd
import yaml

from input.heimdall import _normalize_audio
from faster_whisper import WhisperModel

DURATION = 3
SR = 16000
TRIES_PER_WORD = 2

CANDIDATES = [
    "mentor",       # wisdom theme, no leading R
    "allfather",    # Odin's title, Marvel-saturated
    "sage",         # short, wise, persona-fit
    "oracle",       # wisdom + distinct phonemes
    "jarvis",       # 5-star Whisper benchmark
    "computer",     # bulletproof, generic
    "hela",         # Norse + Marvel-saturated
    "valkyrie",     # Norse, distinct phonemes
]


def record(duration=DURATION) -> np.ndarray:
    config = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    device = config.get("heimdall", {}).get("input_device") or None
    audio = sd.rec(int(duration * SR), samplerate=SR, channels=1,
                   dtype="float32", device=device, blocking=True).flatten()
    return audio


def main():
    config = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    h_cfg = config["heimdall"]

    print(f"\nLoading {h_cfg.get('wake_model','base.en')}...")
    model = WhisperModel(h_cfg.get("wake_model", "base.en"),
                         device=h_cfg.get("device", "cpu"),
                         compute_type=h_cfg.get("compute_type", "int8"))

    print(f"\nWill record {len(CANDIDATES)} candidate words, {TRIES_PER_WORD} tries each.")
    print(f"Each recording is {DURATION}s. Say the word clearly when prompted.\n")
    input("Press Enter when ready... ")

    results = {}
    for word in CANDIDATES:
        results[word] = []
        for trial in range(1, TRIES_PER_WORD + 1):
            print(f"\n--- '{word}' (try {trial}/{TRIES_PER_WORD}) ---")
            for i in range(2, 0, -1):
                print(f"  in {i}...", end="\r", flush=True)
                time.sleep(1)
            print(f"  RECORDING — say '{word}' now            ")
            audio = record()
            norm = _normalize_audio(audio)
            segs, _ = model.transcribe(norm, language="en", beam_size=1)
            transcript = " ".join(s.text for s in segs).strip()
            match = word.lower() in transcript.lower()
            mark = "OK  " if match else "MISS"
            print(f"  {mark} -> {transcript!r}")
            results[word].append((match, transcript))

    print("\n" + "=" * 60)
    print("SUMMARY — wake-word reliability for YOUR voice")
    print("=" * 60)
    for word, trials in results.items():
        hits = sum(1 for ok, _ in trials if ok)
        rate = f"{hits}/{TRIES_PER_WORD}"
        transcripts = [t for _, t in trials]
        marker = "★" * hits + "·" * (TRIES_PER_WORD - hits)
        print(f"  {marker:>4s}  {word:12s}  {rate:>4s}  {transcripts}")

    winners = [w for w, trials in results.items() if all(ok for ok, _ in trials)]
    if winners:
        print(f"\nPerfect-score words: {winners}")
        print(f"Pick one and tell me — I'll wire it as the wake word.")
    else:
        almost = sorted(results.items(), key=lambda kv: -sum(1 for ok,_ in kv[1] if ok))[:3]
        print(f"\nNo perfect scores. Top three: {[(w, sum(1 for ok,_ in r if ok)) for w,r in almost]}")
        print("Tell me the best one, or we run again with different candidates.")


if __name__ == "__main__":
    main()
