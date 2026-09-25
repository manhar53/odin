"""Set ODIN's piper voice by name. Updates config.yaml in place.

  python setup_voice_set.py en_US-ryan-high
  python setup_voice_set.py en_GB-alan-medium

Voice file must already exist in data/voices/ (run setup_voice.py first if
it's missing). Compare voices with setup_voice_compare.py before committing.
"""

import sys
from pathlib import Path
import yaml

VOICES_DIR = Path("data/voices")
CONFIG_PATH = Path("config.yaml")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        # also list installed voices to make the choice easier
        voices = sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))
        print("Installed voices:")
        for v in voices:
            print(f"  • {v}")
        sys.exit(1)

    name = sys.argv[1]
    onnx = VOICES_DIR / f"{name}.onnx"
    if not onnx.exists():
        print(f"FATAL: {onnx} not found.")
        print(f"Run: python setup_voice.py {name}")
        sys.exit(1)

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    iris = cfg.setdefault("iris", {})
    prev = iris.get("piper_voice_path", "")
    new = onnx.as_posix()
    if prev == new:
        print(f"Already using {name}. Nothing to change.")
        return
    iris["piper_voice_path"] = new
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"iris.piper_voice_path: {prev!r}  →  {new!r}")
    print("Restart ODIN to pick up the new voice.")


if __name__ == "__main__":
    main()
