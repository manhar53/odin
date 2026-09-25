# ODIN — wake-dump replay tool
# Usage:
#   python check_dump.py data/debug/wake/<file>.wav
#   python check_dump.py                   (auto-picks the most recent dump)
# Loads a saved wake-buffer, runs the SAME normalize + base.en chain the live
# wake loop uses, and prints what base.en transcribes. If the dump sounds
# clean to your ears but transcribes as garbage here, the issue is the model
# or audio property — not the live capture pipeline.

import os
import sys
import glob
import wave
import numpy as np
import yaml

from input.heimdall import _normalize_audio, _denoise
from faster_whisper import WhisperModel

SR = 16000


def load_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
    if sw != 2 or ch != 1:
        print(f"  warning: expected 16-bit mono, got {sw*8}-bit {ch}ch")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    print(f"  loaded {len(audio)/sr:.2f}s at {sr}Hz, peak={np.max(np.abs(audio)):.3f}, rms={np.sqrt(np.mean(audio**2)):.3f}")
    return audio


def pick_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
    files = sorted(glob.glob("data/debug/wake/*.wav"), key=os.path.getmtime, reverse=True)
    if not files:
        print("No dumps in data/debug/wake/. Pass an explicit path or boot ODIN first.")
        sys.exit(1)
    return files[0]


def main():
    path = pick_path()
    print(f"\nReplaying: {path}")
    audio = load_wav(path)

    config = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    h_cfg = config["heimdall"]

    print(f"\nLoading {h_cfg.get('wake_model', 'base.en')} (this also tests small.en for comparison)...")
    base = WhisperModel(h_cfg.get("wake_model", "base.en"),
                        device=h_cfg.get("device", "cpu"),
                        compute_type=h_cfg.get("compute_type", "int8"))

    print("\n=== Stage A: raw audio (no preprocessing) ===")
    segs, _ = base.transcribe(audio, language="en", beam_size=1)
    print(f"  {h_cfg.get('wake_model','base.en')}: {' '.join(s.text for s in segs).strip()!r}")

    print("\n=== Stage B: normalized (live wake-loop preprocessing) ===")
    norm = _normalize_audio(audio)
    segs, _ = base.transcribe(norm, language="en", beam_size=1)
    print(f"  {h_cfg.get('wake_model','base.en')}: {' '.join(s.text for s in segs).strip()!r}")

    print("\n=== Stage C: denoised + normalized ===")
    den = _normalize_audio(_denoise(audio, SR))
    segs, _ = base.transcribe(den, language="en", beam_size=1)
    print(f"  {h_cfg.get('wake_model','base.en')}: {' '.join(s.text for s in segs).strip()!r}")

    print("\n=== Stage D: try small.en for comparison ===")
    print("  loading small.en (~140MB, downloads first time)...")
    try:
        small = WhisperModel("small.en", device=h_cfg.get("device", "cpu"),
                             compute_type=h_cfg.get("compute_type", "int8"))
        segs, _ = small.transcribe(audio, language="en", beam_size=1)
        small_raw = " ".join(s.text for s in segs).strip()
        print(f"  small.en (raw)  : {small_raw!r}")
        segs, _ = small.transcribe(norm, language="en", beam_size=1)
        small_norm = " ".join(s.text for s in segs).strip()
        print(f"  small.en (norm) : {small_norm!r}")
    except Exception as e:
        print(f"  small.en load failed: {e}")

    print("\n=== Verdict ===")
    print("  If A or B says 'Raven' (or 'Hey Raven'): model + audio are fine,")
    print("  the live wake loop is the bug.")
    print("  If A/B/C are all garbage but small.en gets it: bump wake_model to small.en.")
    print("  If even small.en fails: the audio property (sample rate, channel) is wrong.")


if __name__ == "__main__":
    main()
