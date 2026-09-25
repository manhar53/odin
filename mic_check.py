# ODIN — microphone diagnostic
# Lists all input devices, records a 5-second clip from the default,
# reports peak/RMS, saves the clip to mic_check.wav, and prints a verdict.
#
# Usage:
#   python mic_check.py                    # use default device
#   python mic_check.py 2                  # use device with index 2
#   python mic_check.py "Microphone Array" # use device whose name contains this string

import os
import sys
import wave
import time
import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
DURATION = 5
OUT = "mic_check.wav"


def list_devices():
    print("\n=== Input devices ===")
    devs = sd.query_devices()
    default_in = sd.default.device[0]
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) <= 0:
            continue
        marker = " <-- DEFAULT" if i == default_in else ""
        print(f"  [{i:>2}] {d['name']}  ({d.get('max_input_channels')} ch, {int(d.get('default_samplerate', 0))} Hz){marker}")
    print()


def pick_device(arg) -> int | None:
    if arg is None:
        return None  # use default
    # Numeric index?
    try:
        return int(arg)
    except ValueError:
        pass
    # Name substring match
    devs = sd.query_devices()
    target = arg.lower()
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0 and target in d["name"].lower():
            return i
    print(f"No input device matches '{arg}'.")
    sys.exit(1)


def record(device: int | None) -> np.ndarray:
    name = sd.query_devices(device if device is not None else sd.default.device[0])["name"]
    print(f"Recording {DURATION}s from: {name}")
    print("Say 'Hey ODIN' a few times normally, no shouting...")
    for i in range(3, 0, -1):
        print(f"  starting in {i}...", end="\r", flush=True)
        time.sleep(1)
    print("  RECORDING NOW            ")
    audio = sd.rec(
        int(DURATION * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=device,
        blocking=True,
    ).flatten()
    print("  done.")
    return audio


def diagnose(audio: np.ndarray):
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(audio ** 2)))
    voiced_frac = float(np.mean(np.abs(audio) > 0.01))
    print()
    print("=== Diagnosis ===")
    print(f"  peak amplitude : {peak:.4f}   (1.0 = fully clipped, 0.0 = silence)")
    print(f"  RMS level      : {rms:.4f}   (typical speech ~0.05-0.20)")
    print(f"  fraction loud  : {voiced_frac:.1%}   (% of samples above 0.01)")
    print()
    if peak < 0.02:
        print("  VERDICT: mic is essentially silent.")
        print("  -> Open Windows Sound settings -> Input -> select your mic")
        print("     -> Device properties -> Input volume to 100%, also")
        print("     -> Additional device properties -> Levels tab -> Microphone Boost +20 dB if available.")
        print("  -> If that doesn't help, the mic may be muted in hardware or wrong device picked.")
        print("     Try: python mic_check.py <other-device-index> using the list above.")
    elif peak < 0.10:
        print("  VERDICT: mic is very quiet.")
        print("  -> Bump Windows mic input volume toward 100% (Sound settings -> Input).")
        print("  -> Try Microphone Boost +10 to +20 dB in legacy Sound Control Panel.")
        print("  -> Or pick a louder input device with python mic_check.py <index>.")
    elif peak > 0.97:
        print("  VERDICT: mic is clipping (signal too loud).")
        print("  -> Drop Windows input volume to 50-70%.")
        print("  -> Disable Microphone Boost.")
    elif rms < 0.02:
        print("  VERDICT: peaks are fine but average is too low — likely you're far from the mic.")
        print("  -> Speak closer to the mic (within 30 cm).")
        print("  -> Or use a different mic / headset.")
    else:
        print("  VERDICT: mic levels look healthy. Audio reaching ODIN should be usable.")
        print("  If wake detection still fails, the issue is preprocessing or model — not the mic.")
    print()
    print(f"Saved: {os.path.abspath(OUT)}  (open it to listen and confirm it sounds like you)")


def save(audio: np.ndarray):
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    with wave.open(OUT, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm16)


def main():
    list_devices()
    device = pick_device(sys.argv[1] if len(sys.argv) > 1 else None)
    audio = record(device)
    save(audio)
    diagnose(audio)


if __name__ == "__main__":
    main()
