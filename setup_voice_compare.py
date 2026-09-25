"""Voice A/B comparator.

Synthesizes the same line through every piper voice in data/voices/, plays
them back-to-back through pygame, and prints the config.yaml line to flip
to whichever sounds best. No commit until you say so.

  python setup_voice_compare.py
  python setup_voice_compare.py "your custom line for testing"
"""

import os
import sys
import wave
import tempfile
from pathlib import Path

import pygame
from piper.voice import PiperVoice


VOICES_DIR = Path("data/voices")
DEFAULT_LINE = (
    "Greetings, mortal. The realms hold steady. "
    "Speak your need plainly."
)


def list_voices():
    return sorted(VOICES_DIR.glob("*.onnx"))


def synthesize(voice_path: Path, text: str) -> Path:
    voice = PiperVoice.load(str(voice_path))
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    out.close()
    with wave.open(out.name, "wb") as wf:
        voice.synthesize_wav(text, wf)
    return Path(out.name)


def play(wav_path: Path):
    pygame.mixer.music.load(str(wav_path))
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        pygame.time.wait(80)
    pygame.mixer.music.unload()


def main():
    line = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LINE
    voices = list_voices()
    if not voices:
        print(f"No .onnx voices found in {VOICES_DIR}.")
        print("Run `python setup_voice.py <voice-name>` to download one first.")
        sys.exit(1)

    print(f"Test line: {line!r}\n")
    pygame.mixer.init()

    results = []
    for vp in voices:
        name = vp.stem
        print(f"=== {name} ===  (synthesizing...)")
        wav = synthesize(vp, line)
        size_kb = wav.stat().st_size // 1024
        print(f"  WAV: {size_kb} KB  →  playing now (press Ctrl+C to skip)")
        try:
            play(wav)
        except KeyboardInterrupt:
            print("  (skipped)")
        results.append((name, str(vp)))
        try:
            os.unlink(wav)
        except OSError:
            pass
        print()

    print("=" * 60)
    print("Pick the voice you liked. Update config.yaml:")
    print()
    print("iris:")
    print("  piper_voice_path: \"<one of these>\"")
    for _, p in results:
        print(f"  # {p}")
    print()
    print("Or run `python setup_voice_set.py <voice-name>` if you want it scripted.")


if __name__ == "__main__":
    main()
