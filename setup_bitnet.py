"""BitNet installer for ODIN.

Sets up Microsoft's BitNet-b1.58-2B-4T as an alternative local brain for
GILGAMESH. After install, flip `gilgamesh.backend: "bitnet"` in config.yaml
to use it.

  python setup_bitnet.py install    # install deps + download model (~1.4 GB)
  python setup_bitnet.py status     # show what's installed
  python setup_bitnet.py uninstall  # delete the model file (keeps the wheel)

Why BitNet:
  - 2B params at 1.58-bit weights → ~700 MB resident vs ~1.5 GB for
    llama3.2:1b Q4. Fits in the 8 GB budget alongside Whisper-small.en.
  - CPU inference faster than Q4-quantized dense models of similar size.
  - Llama-cpp-python ships prebuilt Windows wheels — no C++ build step.

Caveats:
  - 1.58-bit weights hurt structured tool-call output. Default ODIN backend
    stays Ollama; flip to BitNet only after you've tested it on your
    common queries (use `bench_50.py` or manual checks).
  - First chat after switching backends pays a ~1.5 s lazy-load cost.
"""

import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "data" / "models" / "bitnet"
# Microsoft's official GGUF release on HuggingFace.
HF_REPO     = "microsoft/bitnet-b1.58-2B-4T-gguf"
MODEL_FILE  = "ggml-model-i2_s.gguf"
MODEL_PATH  = MODEL_DIR / MODEL_FILE


# ─── llama-cpp-python install ────────────────────────────────────
def _pip_install(pkg: str):
    print(f"  pip installing {pkg}...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", pkg],
        check=True,
    )


def _check_llama_cpp():
    try:
        import llama_cpp  # noqa: F401
        return True
    except ImportError:
        return False


def _ensure_llama_cpp():
    if _check_llama_cpp():
        print("  llama-cpp-python already installed.")
        return
    print("  llama-cpp-python missing — installing.")
    _pip_install("llama-cpp-python")
    if not _check_llama_cpp():
        raise RuntimeError(
            "llama-cpp-python install failed. On Windows you may need the "
            "Visual C++ Redistributable from "
            "https://aka.ms/vs/17/release/vc_redist.x64.exe"
        )


# ─── Model download ──────────────────────────────────────────────
def _ensure_hf_hub():
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        _pip_install("huggingface_hub")


def _download_model():
    """Pull just the i2_s GGUF (the 1.58-bit quant) into MODEL_DIR. We use
    hf_hub_download so only that one file is fetched, not the whole repo."""
    _ensure_hf_hub()
    from huggingface_hub import hf_hub_download
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if MODEL_PATH.exists():
        size_gb = MODEL_PATH.stat().st_size / (1024 ** 3)
        print(f"  Model already present at {MODEL_PATH} ({size_gb:.2f} GB).")
        return MODEL_PATH
    print(f"  Downloading {MODEL_FILE} from {HF_REPO} (~1.4 GB)...")
    print(f"  Target: {MODEL_PATH}")
    path = hf_hub_download(
        repo_id=HF_REPO,
        filename=MODEL_FILE,
        local_dir=str(MODEL_DIR),
    )
    print(f"  Downloaded to {path}")
    return Path(path)


# ─── Sanity check: open the model once with llama-cpp-python ─────
def _smoke_load():
    print("  Sanity-loading the model (1.5 s, then unloaded)...")
    from llama_cpp import Llama
    llm = Llama(
        model_path=str(MODEL_PATH),
        n_ctx=512,
        n_threads=0,
        verbose=False,
    )
    out = llm.create_chat_completion(
        messages=[{"role": "user", "content": "Say 'BitNet alive' and nothing else."}],
        max_tokens=12,
        temperature=0.0,
    )
    reply = out["choices"][0]["message"]["content"].strip()
    print(f"  Model reply: {reply!r}")
    del llm   # release the mmap + KV cache


# ─── CLI ─────────────────────────────────────────────────────────
def install():
    print("Installing BitNet backend for ODIN...")
    _ensure_llama_cpp()
    _download_model()
    _smoke_load()
    print()
    print("BitNet ready.")
    print("  Flip the backend by adding to config.yaml under `gilgamesh:`:")
    print('      backend: "bitnet"')
    print(f"      bitnet_model_path: \"{MODEL_PATH.as_posix()}\"")
    print("  Then restart ODIN. To revert, delete those lines.")


def status():
    print("BitNet status:")
    print(f"  llama-cpp-python: {'INSTALLED' if _check_llama_cpp() else 'missing'}")
    if MODEL_PATH.exists():
        size_gb = MODEL_PATH.stat().st_size / (1024 ** 3)
        print(f"  Model file:        EXISTS ({size_gb:.2f} GB) at {MODEL_PATH}")
    else:
        print(f"  Model file:        missing (expected at {MODEL_PATH})")


def uninstall():
    print("Removing BitNet model...")
    if MODEL_PATH.exists():
        MODEL_PATH.unlink()
        print(f"  Deleted: {MODEL_PATH}")
    else:
        print("  Model already absent.")
    print("  llama-cpp-python wheel left in place (uninstall manually with pip).")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "status", "uninstall"):
        print(__doc__)
        sys.exit(1)
    {"install": install, "status": status, "uninstall": uninstall}[sys.argv[1]]()


if __name__ == "__main__":
    main()
