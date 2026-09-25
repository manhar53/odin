"""BitNet backend for GILGAMESH.

Wraps `llama-cpp-python` (which has merged BitNet 1.58-bit kernels) so GIL
can swap from Ollama to BitNet via a single config line. The wrapper
emits chunk objects with the same `.message.content` / `.message.tool_calls`
shape `ollama.Client` returns, so GIL's `think()` loop is unchanged.

Why BitNet:
  - `BitNet-b1.58-2B-4T` is a 2-billion-parameter model with ternary (1.58-bit)
    weights. Resident memory ≈ 700 MB. Fits where llama3.2:1b sits today,
    but with 2× the parameters → better chit-chat quality.
  - llama-cpp-python ships pre-built wheels for Windows, so no C++ compile.

Setup:
  1. `python setup_bitnet.py install` — installs the wheel + downloads the
     2B model file (~1.4 GB) into `data/models/bitnet/`.
  2. Flip `gilgamesh.backend: "bitnet"` in config.yaml.
  3. Restart ODIN — the first chat query lazy-loads the model.

Known limits:
  - 1.58-bit models are weaker at structured tool-call output than Q4
    quantized models of similar size. GIL's JSON-content recovery catches
    most malformed tool shapes, but if you see degraded tool dispatch,
    flip back to Ollama or use hybrid mode (TBD).
"""

import os
from types import SimpleNamespace
from typing import Iterator, Optional

# llama-cpp-python is optional. We import lazily so an ODIN install without
# BitNet still boots cleanly when the user's config picks ollama.
try:
    from llama_cpp import Llama
    _HAS_LLAMA_CPP = True
except ImportError:
    _HAS_LLAMA_CPP = False


class BitNetClient:
    """Drop-in replacement for `ollama.Client` exposing only the methods GIL
    actually uses (`.chat`). Returns chunks shaped like ollama's:
        chunk.message.content        (str)
        chunk.message.tool_calls     (list | None)

    The 1.58-bit model rarely produces clean OpenAI-style tool_calls, so we
    always return None for that field and let GIL's JSON-content recovery
    detect tool-call-shaped content from the stream."""

    def __init__(self, model_path: str, *, n_ctx: int = 2048, n_threads: int = 0):
        if not _HAS_LLAMA_CPP:
            raise RuntimeError(
                "llama-cpp-python is not installed. Run `python setup_bitnet.py install`."
            )
        if not os.path.exists(model_path):
            raise RuntimeError(
                f"BitNet model not found at {model_path}. "
                f"Run `python setup_bitnet.py install` to download it."
            )
        self.model_path = model_path
        self.n_ctx = n_ctx
        # n_threads=0 → llama-cpp-python auto-picks based on cores.
        self.n_threads = n_threads
        self._llm: Optional[Llama] = None

    def _ensure_loaded(self):
        if self._llm is None:
            print(f"[BITNET] Loading {os.path.basename(self.model_path)} (lazy first-call)...")
            self._llm = Llama(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                verbose=False,
            )
            print("[BITNET] Model loaded.")

    # ──────────────────────────────────────────────────────────────
    # Public API — matches ollama.Client.chat() arg-for-arg so GIL doesn't
    # care which backend it's calling.
    # ──────────────────────────────────────────────────────────────
    def chat(self, model: str = "", messages: list = None, stream: bool = True,
             tools: Optional[list] = None, options: Optional[dict] = None,
             keep_alive: Optional[str] = None, **_):
        self._ensure_loaded()
        opts = options or {}
        msgs = list(messages or [])

        # Tool descriptions are folded into a system prefix so the model is
        # at least *aware* of what's available. llama-cpp-python's tool_choice
        # support is still partial; we don't try to bind it formally — the
        # model emits tool-call-shaped JSON in content, and GIL's
        # _recover_tool_call_from_json catches it post-stream.
        if tools:
            tool_lines = []
            for t in tools:
                f = t.get("function", {}) if isinstance(t, dict) else {}
                tool_lines.append(f"- {f.get('name', '')}: {f.get('description', '')}")
            tool_block = (
                "You have these tools. To call one, emit a single JSON object "
                'as your reply: {"name": "<tool>", "parameters": {...}}.\n'
                + "\n".join(tool_lines)
            )
            msgs = [{"role": "system", "content": tool_block}] + msgs

        kwargs = {
            "messages": msgs,
            "stream": stream,
            "temperature": opts.get("temperature", 0.4),
            "max_tokens": opts.get("num_predict", 130),
        }

        if not stream:
            response = self._llm.create_chat_completion(**kwargs)
            content = response["choices"][0]["message"].get("content", "") or ""
            yield _chunk(content)
            return

        for ev in self._llm.create_chat_completion(**kwargs):
            delta = ev.get("choices", [{}])[0].get("delta", {})
            content = delta.get("content", "") or ""
            yield _chunk(content)


def _chunk(content: str):
    """Build a chunk-like object compatible with ollama's response shape.
    SimpleNamespace lets us write chunk.message.content downstream."""
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))
