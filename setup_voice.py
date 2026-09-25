# ODIN — one-time piper voice model downloader
# Usage:
#   python setup_voice.py                       # default: en_US-ryan-high
#   python setup_voice.py en_US-amy-medium      # any voice from rhasspy/piper-voices

import os
import sys
import urllib.request

VOICES_DIR = "data/voices"
DEFAULT_VOICE = "en_US-ryan-high"
HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def parse_voice(name: str) -> str:
    parts = name.split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid voice name '{name}'. Expected e.g. en_US-ryan-high.")
    locale = parts[0]
    lang = locale.split("_")[0]
    voice_name = parts[1]
    quality = parts[2] if len(parts) > 2 else "medium"
    return f"{lang}/{locale}/{voice_name}/{quality}"


def download(url: str, dest: str):
    print(f"[SETUP] Downloading {os.path.basename(dest)}")
    state = {"last_pct": -1}

    def report(block_num, block_size, total_size):
        if total_size <= 0:
            return
        pct = min(100, block_num * block_size * 100 // total_size)
        if pct != state["last_pct"] and pct % 5 == 0:
            print(f"[SETUP]   {pct}%")
            state["last_pct"] = pct

    urllib.request.urlretrieve(url, dest, reporthook=report)


def main():
    voice = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VOICE
    sub_path = parse_voice(voice)
    os.makedirs(VOICES_DIR, exist_ok=True)

    onnx = os.path.join(VOICES_DIR, f"{voice}.onnx")
    json_cfg = os.path.join(VOICES_DIR, f"{voice}.onnx.json")

    if os.path.exists(onnx) and os.path.exists(json_cfg):
        print(f"[SETUP] Voice '{voice}' already installed at {onnx}")
        return

    base = f"{HF_BASE}/{sub_path}"
    if not os.path.exists(onnx):
        download(f"{base}/{voice}.onnx", onnx)
    if not os.path.exists(json_cfg):
        download(f"{base}/{voice}.onnx.json", json_cfg)

    print(f"[SETUP] Voice '{voice}' ready at {onnx}")
    print(f"[SETUP] Update config.yaml: iris.piper_voice_path: {onnx}")


if __name__ == "__main__":
    main()
