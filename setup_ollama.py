"""ODIN — one-time Ollama tuning for 16GB CPU laptops.

Sets OLLAMA_MAX_LOADED_MODELS=1 in the Windows user environment, which makes
Ollama keep only one model in memory at a time. Without this, when you ask
ODIN a chat question (loads 1B) right after an action (loads 3B), both models
end up resident and may swap to disk on 16GB — that's the "brain stopped
responding" message you see when an LLM call exceeds the watchdog timeout.

Trade-off: alternating between chat and action pays a 3-5s model reload.
But that's predictable; swap thrash isn't.

Run once:
    python setup_ollama.py

You'll be prompted to confirm. After it sets the variable, you must restart
Ollama (kill the tray icon and `ollama serve`, or just reboot) for the
change to take effect.
"""
import subprocess
import sys


def main():
    print("This will set OLLAMA_MAX_LOADED_MODELS=1 in your Windows user environment.")
    print()
    print("Effect: Ollama loads at most one model at a time. When ODIN needs the")
    print("        other (1B chat vs 3B tools), it evicts the current one first.")
    print("Trade-off: ~3s reload when alternating, but no swap thrash on 16GB.")
    print()
    confirm = input("Apply this setting? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Skipped.")
        return 0
    try:
        subprocess.run(
            ["setx", "OLLAMA_MAX_LOADED_MODELS", "1"],
            check=True,
            capture_output=True,
        )
        print()
        print("OLLAMA_MAX_LOADED_MODELS=1 written to user env.")
        print()
        print("To activate it now you must restart the Ollama service:")
        print("  1. Right-click the Ollama tray icon -> Quit (or Stop-Process -Name ollama -Force)")
        print("  2. Reopen Ollama from the Start menu, or run:  ollama serve")
        print()
        print("Verify it took effect by running:  ollama ps")
        print("If you see only one model after switching between commands, it's working.")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"FAILED: {e}")
        print("You may need to run this from a terminal with appropriate permissions.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
