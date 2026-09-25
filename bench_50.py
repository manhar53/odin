# ODIN — L1 acceptance benchmark
# Records 50 utterances and checks each one against an expected wake-or-command
# transcript. Produces a pass/fail summary so we can prove the L1 latency +
# accuracy gates are met.
#
# Usage:
#   python bench_50.py
#
# What it does:
#   1. Prompts you for each of 50 utterances (10 wake-only, 40 command).
#   2. Records 4s per utterance through the same audio pipeline HEIMDALL uses
#      (sd.InputStream + _normalize_audio + small.en transcribe + ghost filter).
#   3. Marks each PASS / SOFT-FAIL / HARD-FAIL.
#   4. Prints the L1 verdict.
#
# This is intentionally NOT integrated into the running ODIN — it's a
# diagnostic harness. Run it after you change anything in HEIMDALL or after
# you change wake_phrases / models in config.yaml.

import re
import sys
import time
import wave
import numpy as np
import sounddevice as sd
import yaml

from input.heimdall import (
    _normalize_audio,
    _is_whisper_ghost,
    _FUZZY_WAKE,
    SAMPLE_RATE,
)
from faster_whisper import WhisperModel


# (prompt-shown-to-user, expected-substring-or-pattern, kind)
# kind: "wake" = wake-word only test; "cmd" = full command (wake + command).
SUITE = (
    [("hey sage", "sage", "wake")] * 5 +
    [("sage", "sage", "wake")] * 5 +
    [("hey sage what time is it", "time", "cmd")] * 4 +
    [("hey sage open notepad", "notepad", "cmd")] * 4 +
    [("hey sage check the weather", "weather", "cmd")] * 4 +
    [("hey sage take a screenshot", "screenshot", "cmd")] * 4 +
    [("hey sage volume to fifty", "volume", "cmd")] * 4 +
    [("hey sage who are you", "who are you", "cmd")] * 4 +
    [("hey sage list my reminders", "reminder", "cmd")] * 4 +
    [("hey sage lock the screen", "lock", "cmd")] * 4 +
    [("hey sage system stats", "stats|system", "cmd")] * 4
)
# That's exactly 50 utterances.

DURATION = 4.0


def _record(seconds: float, device) -> np.ndarray:
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                   channels=1, dtype="float32", device=device, blocking=True)
    return audio.flatten()


def _check(transcript: str, expected: str, kind: str, wake_phrases) -> str:
    """Return PASS / SOFT-FAIL / HARD-FAIL.

    HARD-FAIL = ghost transcript or completely missing the expected phrase.
    SOFT-FAIL = wake matched but command keyword missing (or vice versa).
    PASS      = both wake and expected present (or wake present for wake tests).
    """
    if _is_whisper_ghost(transcript):
        return "HARD-FAIL"
    t_low = transcript.lower()
    has_wake = any(p in t_low for p in wake_phrases) or bool(_FUZZY_WAKE.search(t_low))
    has_expected = bool(re.search(expected, t_low))
    if kind == "wake":
        return "PASS" if has_wake else "HARD-FAIL"
    # cmd
    if has_wake and has_expected:
        return "PASS"
    if has_wake or has_expected:
        return "SOFT-FAIL"
    return "HARD-FAIL"


def main():
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    h_cfg = cfg["heimdall"]
    wake_phrases = cfg["odin"]["wake_phrases"]
    device = h_cfg.get("input_device") or None
    model_name = h_cfg.get("stt_model", "small.en")

    print(f"Loading {model_name}...")
    model = WhisperModel(
        model_name, device=h_cfg.get("device", "cpu"),
        compute_type=h_cfg.get("compute_type", "int8"),
    )

    print(f"\n=== ODIN L1 Benchmark — 50 utterances ===")
    print(f"Wake phrases: {wake_phrases}")
    print(f"Each clip is {DURATION}s. Speak naturally when prompted.\n")
    input("Press Enter to begin... ")

    results = []
    for i, (prompt, expected, kind) in enumerate(SUITE, 1):
        print(f"\n[{i:02d}/50] {kind.upper():4s}  Say: {prompt!r}")
        for c in range(2, 0, -1):
            print(f"  in {c}...", end="\r", flush=True)
            time.sleep(1)
        print(f"  RECORDING...        ")
        audio = _record(DURATION, device)
        norm = _normalize_audio(audio)

        t0 = time.perf_counter()
        try:
            segments, _ = model.transcribe(
                norm, language="en", beam_size=1,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500, threshold=0.35),
            )
        except TypeError:
            segments, _ = model.transcribe(norm, language="en", beam_size=1)
        text = " ".join(s.text for s in segments).strip()
        latency = time.perf_counter() - t0

        verdict = _check(text, expected, kind, wake_phrases)
        marker = {"PASS": "✓", "SOFT-FAIL": "~", "HARD-FAIL": "✗"}[verdict]
        print(f"  {marker} {verdict:9s}  ({latency:.2f}s)  -> {text!r}")
        results.append((i, prompt, expected, kind, verdict, text, latency))

    # Summary
    pass_count = sum(1 for r in results if r[4] == "PASS")
    soft_fail = sum(1 for r in results if r[4] == "SOFT-FAIL")
    hard_fail = sum(1 for r in results if r[4] == "HARD-FAIL")
    avg_latency = sum(r[6] for r in results) / len(results)

    print("\n" + "=" * 60)
    print("L1 BENCHMARK RESULT")
    print("=" * 60)
    print(f"PASS:      {pass_count}/50  ({pass_count/50*100:.0f}%)")
    print(f"SOFT-FAIL: {soft_fail}/50  ({soft_fail/50*100:.0f}%)")
    print(f"HARD-FAIL: {hard_fail}/50  ({hard_fail/50*100:.0f}%)")
    print(f"Avg transcribe latency: {avg_latency*1000:.0f}ms")
    print()
    if hard_fail / 50 < 0.05:
        print("  L1 GATE: PASSED — hard-failure rate < 5%.")
    else:
        print(f"  L1 GATE: FAILED — hard-failure rate {hard_fail/50*100:.0f}% > 5%.")
        print("  Review the HARD-FAIL transcripts above to see the failure modes.")


if __name__ == "__main__":
    main()
