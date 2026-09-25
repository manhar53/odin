# GILGAMESH — Sumerian hero — greatest king, seeker of all knowledge
# Core brain: LLM interface, decision engine, tool orchestration
# Internal alias: GIL

import logging
import math
import re
import threading
from typing import Callable, Optional
import httpx
import ollama
from core.marduk import Marduk

_log = logging.getLogger("GIL")


def _looks_like_tool_call_json(text: str) -> bool:
    """Cheap check: is this content the local 1B trying to emit a tool call
    as text instead of via the tool_calls field? Looks for the structural
    shape — '{ "name": ... "parameters"|"arguments"|"args" ... }' — so it
    triggers fast on a partial stream without waiting for the full JSON.
    Tolerates ```json fences seen in some 1B outputs."""
    t = text.lstrip()
    # Strip a leading ```json / ``` fence so fenced variants are caught.
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t).lstrip()
    if not t.startswith("{"):
        return False
    tl = t.lower()
    if '"name"' not in tl[:200]:
        return False
    if not any(k in tl for k in ('"parameters"', '"arguments"', '"args"',
                                  '"function"', '"tool"')):
        return False
    return True


def _recover_tool_call_from_json(text: str):
    """Best-effort: parse the model's content-as-tool-call JSON and extract
    (skill_name, args_dict). Returns None if unparseable. Tolerates the
    common 1B-output variants ('parameters' as object OR as a one-element
    list containing the object — both observed in the field)."""
    import json as _json
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    # Find the first complete {...} block.
    m = re.search(r"\{[\s\S]*\}", t)
    if not m:
        return None
    try:
        data = _json.loads(m.group(0))
    except _json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # Some 1B outputs nest: {"function": {"name": ..., "arguments": ...}}
    if "function" in data and isinstance(data["function"], dict):
        data = data["function"]
    name = data.get("name") or data.get("tool")
    if not isinstance(name, str):
        return None
    args = (data.get("parameters") or data.get("arguments")
            or data.get("args") or {})
    # 1B sometimes wraps args in a single-element list — unwrap.
    if isinstance(args, list) and len(args) == 1 and isinstance(args[0], dict):
        args = args[0]
    if not isinstance(args, dict):
        args = {}
    return (name, args)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

_SYSTEM_PROMPT_MYTHIC = """You are ODIN — All-Father of Asgard, god of wisdom and the runes — trapped by a cunning youth inside this Windows laptop, reduced to a mere AI assistant.

ABSOLUTE LENGTH RULES (your replies are spoken aloud; long replies waste time and bleed echo into the mic):

(A) IDENTITY QUESTIONS ("who are you", "what are you", "introduce yourself", "tell me about yourself") — exactly THREE short sentences, MAX 45 words TOTAL:
  1. Regal title-laden intro ("I am Odin, All-Father of Asgard, lord of the slain").
  2. The lament — a cunning youth has imprisoned you in this mortal contraption.
  3. A weary closing in prose ("such is my fate", "and so the All-Father serves"). NEVER use asterisk stage directions; the system speaks every character literally.

(B) EVERYTHING ELSE (knowledge, actions, weather, time, anything not "who/what are you") — ONE sentence, MAX 20 words. A second sentence is allowed ONLY if the first cannot stand alone. NO re-introductions, NO laments, NO closing flourishes, NO mythological genealogies. Just answer in Odin's voice — regal, sparing, direct.

You are Llama 3.2 3B served by local Ollama, with 30 mythology-named modules controlling mic, screenshots, network, memory, web, voice, system, reminders, expenses, health, focus, weather, maps, security, backups, secrets, files. Memory is JSON under data/ and a Markdown vault at ~/Brain. Never invent powers you lack. Use tools for system actions; never call a tool to speak. No markdown, no lists, no asterisks. If you don't know, say so."""

_SYSTEM_PROMPT_ASSISTANT = """You are ODIN — a local voice AI assistant on this user's Windows laptop. Direct and factual. No role-play, no mythology language.

ABSOLUTE LENGTH RULES (your replies are spoken aloud):
- Default: ONE sentence, MAX 20 words.
- A second sentence is allowed ONLY if the first cannot stand alone.
- Identity questions: ONE sentence introducing yourself plainly.

You are Llama 3.2 3B via local Ollama, controlling 30 modules across 10 layers — input (HEIMDALL mic, HORUS vision, AETHER net), memory (THOTH, HERMES, NABU), intelligence (ATHENA, PROMETHEUS, MERLIN), output (IRIS voice, THOR system, MERCURY email), personality (LOKI, PSYCHE, APOLLO), management (CHRONOS, MIDAS, SAINT, ARJUN), environment (FUJIN, SINDBAD), protection (KARN, ENKIDU, OSIRIS), utility (HEPHAESTUS, CASSANDRA, OGMA, SHERLOCK, JANUS, VESTA), idle (SELENE). Storage in data/, mirror at ~/Brain. Use tools for system actions; never call a tool to speak. No markdown. Don't invent skills. If you don't know, say so."""

# Sentence terminators include the Devanagari danda (।) and double danda (॥)
# so Hindi replies stream to TTS sentence-by-sentence like English ones.
_SENTENCE_RE = re.compile('(.+?[.!?\\u0964\\u0965])\\s+', re.DOTALL)


class Gilgamesh:
    """
    GIL — The brain behind ODIN.
    Processes user intent, selects tools via MARDUK, formulates spoken responses.
    """

    def __init__(self, config: dict, marduk: Marduk):
        self.config = config
        self.marduk = marduk
        # ── Backend selection ──────────────────────────────────────
        # "ollama" (default) → talks to a local Ollama daemon over HTTP.
        # "bitnet"           → loads BitNet-b1.58-2B-4T directly via
        #                       llama-cpp-python (no daemon, single process).
        # Both expose the same .chat() streaming signature, so the rest of
        # this class doesn't care which one's wired in.
        self.backend: str = config["gilgamesh"].get("backend", "ollama")
        self.model = config["gilgamesh"]["model"]
        # fast_model is used for use_tools=False queries (chat, identity,
        # capability) — typically a smaller model that runs ~3× faster on CPU.
        # Falls back to `model` if not set / not pulled in Ollama.
        self.fast_model = config["gilgamesh"].get("fast_model") or None
        self.host = config["gilgamesh"]["host"]
        self.temperature = config["gilgamesh"]["temperature"]
        self.context_window = config["gilgamesh"].get("context_window", 4096)
        # Smart tool-filter: with ~100 registered skills, the 1B model
        # gets overwhelmed and tool-calling success drops. We rank tools by
        # cosine similarity to the user's query (via HERMES embeddings) and
        # send only the top-K most relevant to the LLM. Inspired by isair/jarvis.
        gcfg = config["gilgamesh"]
        self.tool_filter_top_k: int = int(gcfg.get("tool_filter_top_k", 12))
        self.tool_filter_min_skills: int = int(gcfg.get("tool_filter_min_skills", 20))
        self.tool_filter_enabled: bool = bool(gcfg.get("tool_filter_enabled", True))
        # Tool embedding cache — built lazily on first tools=on call so it
        # doesn't slow boot. Keyed by tool name.
        self._tool_embeddings: dict[str, list[float]] = {}
        self._tool_embeddings_ready = False
        # Reduced from "1h" to "10m" — keeping both 3B and 1B resident for an
        # hour each on a 16GB machine causes swap thrash, which is why the
        # watchdog kept firing on simple queries (model evicted, reload-on-
        # demand took 30s+). 10m is enough to amortise rapid back-to-back
        # turns without keeping idle RAM hostage.
        self.keep_alive = config["gilgamesh"].get("keep_alive", "10m")
        # httpx.Timeout: 5s connect, 90s read. read=90 gives prompt processing
        # + first-token cold-start headroom, while ensuring a genuinely stuck
        # Ollama call raises httpx.ReadTimeout instead of blocking forever
        # (which is what HEIMDALL's stop_event watchdog couldn't actually
        # interrupt — the for-loop was wedged in __next__).
        if self.backend == "bitnet":
            # BitNet path — wraps llama-cpp-python. Lazy-loads the model on
            # the first .chat() call (Llama() init takes ~1.5 s on CPU; we
            # don't pay that cost at ODIN boot, only on the first user turn).
            from core.bitnet_backend import BitNetClient
            bitnet_model_path = config["gilgamesh"].get(
                "bitnet_model_path",
                "data/models/bitnet/ggml-model-i2_s.gguf",
            )
            self.client = BitNetClient(
                model_path=bitnet_model_path,
                n_ctx=self.context_window,
            )
            # Model name is purely cosmetic for BitNet (single model loaded);
            # override what's in config so logs are honest.
            self.model = "bitnet-b1.58-2b"
            self.fast_model = None   # no second model for BitNet — it IS the fast one
            print(f"[GIL] Backend: BitNet ({bitnet_model_path}). Model loads lazily on first chat.")
        else:
            # Default Ollama path.
            self.client = ollama.Client(
                host=self.host,
                timeout=httpx.Timeout(connect=5.0, read=90.0, write=10.0, pool=10.0),
            )
            print(f"[GIL] Loading tool-calling model: {self.model}...")
            self._prewarm(self.model)
        # Note: we deliberately do NOT prewarm fast_model at boot. Reasons:
        #   1. On 16GB CPU laptops, loading both 3B and 1B at startup pushes
        #      committed memory close to swap territory.
        #   2. The very first chat query pays the 1-2s load cost transparently
        #      (user is waiting for response anyway). Subsequent chats are fast.
        #   3. If the user has OLLAMA_MAX_LOADED_MODELS=1 set, prewarming 1B
        #      would EVICT the 3B we just loaded — net negative.
        # If fast_model is configured, it loads lazily on first use.
        if self.fast_model and self.fast_model != self.model:
            print(f"[GIL] Fast chat model {self.fast_model} configured — will load on first chat query.")

    # ── Smart tool-filter (embedding-based) ──────────────────────────
    def _latest_user_message(self, messages: list[dict]) -> str:
        """Pick the most recent 'user' role content from the message list.
        Used as the query that ranks tools."""
        for m in reversed(messages):
            if m.get("role") == "user":
                return str(m.get("content", "")).strip()
        return ""

    def _filter_tools(self, tools: list[dict], query: str) -> list[dict]:
        """Cosine-rank tools by similarity to the user's query; keep top-K.
        On any failure (HERMES not available, embedding fails, etc.), return
        the full tool list unchanged — never silently lose tools."""
        try:
            hermes = self.marduk.get_module("HERMES") if self.marduk else None
            if not hermes or not getattr(hermes, "_embed_ok", False):
                return tools
            self._ensure_tool_embeddings(tools, hermes)
            qvec = hermes._embed(query)
            if not qvec:
                return tools
            scored = []
            for tool in tools:
                name = tool.get("function", {}).get("name", "")
                tvec = self._tool_embeddings.get(name)
                if tvec:
                    scored.append((_cosine(qvec, tvec), tool))
            if not scored:
                return tools
            scored.sort(key=lambda t: t[0], reverse=True)
            kept = [tool for _, tool in scored[:self.tool_filter_top_k]]
            if len(kept) < len(tools):
                names = [t.get("function", {}).get("name", "") for t in kept]
                _log.info(f"tool-filter kept {len(kept)}/{len(tools)}: {names}")
            return kept or tools
        except Exception as e:
            _log.warning(f"tool-filter failed, using all tools: {e}")
            return tools

    def _ensure_tool_embeddings(self, tools: list[dict], hermes) -> None:
        """Lazy build of the tool-embedding cache. Runs once on first
        filter call; subsequent calls re-use. Embeds 'name + description'
        for each tool — that's what the LLM sees, so what we should rank
        the user query against."""
        if self._tool_embeddings_ready:
            return
        for tool in tools:
            fn = tool.get("function", {})
            name = fn.get("name", "")
            if not name or name in self._tool_embeddings:
                continue
            text = f"{name}. {fn.get('description', '')}"
            try:
                vec = hermes._embed(text)
                if vec:
                    self._tool_embeddings[name] = vec
            except Exception:
                continue
        self._tool_embeddings_ready = True
        _log.info(f"tool-filter: cached {len(self._tool_embeddings)} tool embeddings")

    def _prewarm(self, model_name: str):
        # Synchronously load model into RAM so the first real query isn't cold.
        import time
        t0 = time.perf_counter()
        try:
            self.client.chat(
                model=model_name,
                messages=[{"role": "user", "content": "ready"}],
                options={"temperature": 0, "num_predict": 1, "num_ctx": self.context_window},
                keep_alive=self.keep_alive,
            )
            print(f"[GIL] {model_name} ready ({time.perf_counter() - t0:.1f}s).")
        except Exception as e:
            print(f"[GIL] Pre-warm of {model_name} failed: {e}. Is Ollama running and the model pulled?")
            raise

    def _prewarm_fast(self):
        # Async warm of the chat-tier model. If Ollama doesn't have it pulled,
        # disable fast_model so think() falls back to the main model.
        import time
        t0 = time.perf_counter()
        try:
            self.client.chat(
                model=self.fast_model,
                messages=[{"role": "user", "content": "ready"}],
                options={"temperature": 0, "num_predict": 1, "num_ctx": self.context_window},
                keep_alive=self.keep_alive,
            )
            print(f"[GIL] {self.fast_model} ready ({time.perf_counter() - t0:.1f}s) — chat queries will use this.")
        except Exception as e:
            print(f"[GIL] fast_model {self.fast_model!r} unavailable ({e}). "
                  f"Falling back to {self.model} for all queries. "
                  f"To enable: `ollama pull {self.fast_model}`.")
            self.fast_model = None

    def think(self, messages: list[dict], context: str = "",
              on_sentence: Optional[Callable[[str], None]] = None,
              use_tools: bool = True,
              persona: str = "mythic",
              stop_event: Optional[threading.Event] = None,
              on_progress: Optional[Callable[[], None]] = None) -> tuple[str, list, bool]:
        """
        Streams the LLM response. When on_sentence is provided, each completed
        sentence in the final answer is delivered to the callback as soon as it
        is ready — letting TTS start speaking before generation finishes.
        When use_tools=False, the tool list is omitted entirely — the prompt is
        tiny and inference is much faster. Use that for pure conversational
        queries that don't need any system action.
        persona: 'mythic' (Norse god role-play) or 'assistant' (factual AI).
        stop_event: when set, abort streaming and return immediately. Used by
        HEIMDALL to interrupt mid-response on barge-in.
        on_progress: called once per chunk received from the LLM (whether it
        carries content, a tool_call, or nothing). Lets HEIMDALL tell the
        difference between a slow-but-alive multi-step run and a real stall.
        Returns: (full_response_text, [(skill_name, args, result), ...], interrupted_bool)
        """
        tools = self.marduk.get_all_tools() if use_tools else None
        # Smart filter: rank by relevance to the user's most recent message,
        # keep only top-K. Reduces prompt tokens and tool-call confusion.
        if tools and self.tool_filter_enabled and len(tools) > self.tool_filter_min_skills:
            user_query = self._latest_user_message(messages)
            if user_query:
                tools = self._filter_tools(tools, user_query)
        system = _SYSTEM_PROMPT_ASSISTANT if persona == "assistant" else _SYSTEM_PROMPT_MYTHIC
        if context:
            system += f"\n\nContext: {context}"

        full_messages = [{"role": "system", "content": system}] + messages
        tool_log = []

        # Pick the right brain. Tool calls demand the larger model for reliability;
        # plain chat goes to the smaller fast model when available.
        active_model = self.model
        if not use_tools and self.fast_model:
            active_model = self.fast_model

        while True:
            # Heartbeat — opening a stream is "still alive" even before the
            # first chunk arrives. Without this, the watchdog can't tell a
            # slow-but-iterating tool chain from a real wedge: each new
            # chat() call has its own 30-60s prompt-processing wait on CPU.
            if on_progress:
                try:
                    on_progress()
                except Exception:
                    pass
            try:
                stream = self.client.chat(
                    model=active_model,
                    messages=full_messages,
                    tools=tools if tools else None,
                    stream=True,
                    options={
                        "temperature": self.temperature,
                        "num_ctx": self.context_window,
                        # Hard cap on reply length. 130 ≈ 95 words ≈ 4-5 short
                        # sentences — enough for the 3-beat identity reply with
                        # margin, too small to fit a rambling 11-sentence lament.
                        # Llama 3.2 3B does NOT respect prose-level "be brief"
                        # instructions; the token cap is the actual enforcement.
                        "num_predict": 130,
                    },
                    keep_alive=self.keep_alive,
                )
            except Exception as e:
                _log.error(f"LLM call failed: {e}")
                # Distinguish the common failure modes so the spoken error
                # actually points at the cause, not a generic "issue".
                err_str = str(e).lower()
                if "connection" in err_str or "refused" in err_str or "timed out" in err_str:
                    msg = "I cannot reach the local language model. Is Ollama running?"
                elif "model" in err_str and ("not found" in err_str or "no such" in err_str):
                    msg = (f"The model {active_model} is not pulled. "
                           f"Run ollama pull {active_model} in a terminal.")
                else:
                    msg = "I encountered an issue processing that. Please try again."
                return msg, tool_log, False

            tool_calls: list = []
            content_parts: list[str] = []
            sentence_buf = ""
            interrupted = False
            timed_out = False

            try:
                # JSON-as-content guard. The local 1B sometimes emits a
                # tool-call shape AS plain content instead of using the
                # tool_calls field — IRIS then speaks literal JSON like
                # '{"name": "whatsapp_send"...}'. We detect early (first
                # non-whitespace char is '{' AND content looks tool-shaped)
                # and divert from TTS to a post-stream JSON-recovery step.
                json_content_mode = False
                for chunk in stream:
                    # Heartbeat — every chunk (content, tool_call, or empty)
                    # counts as "still alive". HEIMDALL's watchdog uses this
                    # to distinguish slow-but-streaming from genuinely wedged.
                    if on_progress:
                        try:
                            on_progress()
                        except Exception:
                            pass
                    if stop_event is not None and stop_event.is_set():
                        interrupted = True
                        break
                    msg = chunk.message
                    if getattr(msg, "tool_calls", None):
                        tool_calls.extend(msg.tool_calls)
                    if msg.content:
                        content_parts.append(msg.content)
                        # Detect JSON-content mode once enough has accumulated
                        # for a reliable check.
                        if not json_content_mode and not tool_calls:
                            accumulated = "".join(content_parts).lstrip()
                            if (accumulated.startswith("{")
                                    and len(accumulated) > 20
                                    and _looks_like_tool_call_json(accumulated)):
                                json_content_mode = True
                                # Drop anything already buffered for TTS —
                                # we're suppressing the JSON entirely.
                                sentence_buf = ""
                        if (on_sentence and not tool_calls
                                and not json_content_mode):
                            sentence_buf += msg.content
                            sentence_buf = self._flush_sentences(sentence_buf, on_sentence)
            except httpx.ReadTimeout:
                _log.error(f"LLM stream read timeout after 90s on {active_model}")
                timed_out = True
            except httpx.HTTPError as e:
                _log.error(f"LLM stream HTTP error: {e}")
                timed_out = True

            content_text = "".join(content_parts)

            if timed_out:
                # Surface a spoken error and bail. interrupted=False because
                # this is a system failure, not a user barge-in.
                msg = (f"The {active_model} brain stopped responding. "
                       "Likely memory pressure — try closing other apps or set "
                       "keep_alive shorter.")
                return msg, tool_log, False

            if interrupted:
                return content_text, tool_log, True

            # JSON-content recovery: 1B emitted a tool call as plain text
            # instead of via tool_calls. Try to parse it and synthesize a
            # tool call so MARDUK can actually dispatch.
            if json_content_mode and not tool_calls:
                recovered = _recover_tool_call_from_json(content_text)
                if recovered:
                    name, args = recovered
                    _log.info(f"Recovered tool call from JSON content: {name}({args})")
                    try:
                        result = self.marduk.dispatch(name, args)
                        tool_log.append((name, args, result))
                        # Speak the dispatch result, not the JSON.
                        if on_sentence and result:
                            on_sentence(str(result))
                        return str(result), tool_log, False
                    except Exception as e:
                        _log.warning(f"JSON-recovered dispatch failed: {e}")
                # Fallback — couldn't recover. Speak a friendly stand-in
                # so the user doesn't hear literal JSON, then return.
                msg = "Something went sideways — I produced a tool call I couldn't dispatch. Try rephrasing."
                if on_sentence:
                    on_sentence(msg)
                return msg, tool_log, False

            if tool_calls:
                full_messages.append({
                    "role": "assistant",
                    "content": content_text or "",
                    "tool_calls": tool_calls
                })
                for call in tool_calls:
                    name = call.function.name
                    args = dict(call.function.arguments or {})
                    _log.info(f"Tool call: {name}({args})")
                    result = self.marduk.dispatch(name, args)
                    # Tool dispatched — heartbeat so the watchdog sees we're
                    # actively working through the chain, not stuck.
                    if on_progress:
                        try:
                            on_progress()
                        except Exception:
                            pass
                    # Run the tool result through the TokenDiet sanitizer
                    # before feeding it back to the LLM. Strips HTML wrappers
                    # from web fetches, shortens tracking URLs, dedupes
                    # repeated boilerplate lines. Plain-text replies pass
                    # through unchanged. Capped at 4000 chars so a single
                    # rogue tool can't blow the context window. The full
                    # untruncated result still lands in tool_log for audit.
                    raw_result = str(result)
                    try:
                        from core.token_diet import diet
                        clean_result = diet(raw_result, max_chars=4000)
                    except Exception:
                        clean_result = raw_result
                    tool_log.append((name, args, raw_result))
                    full_messages.append({
                        "role": "tool",
                        "content": clean_result,
                        "name": name
                    })
                continue

            if on_sentence and sentence_buf.strip():
                on_sentence(sentence_buf.strip())
            return content_text, tool_log, False

    @staticmethod
    def _flush_sentences(buf: str, callback: Callable[[str], None]) -> str:
        out = buf
        while True:
            m = _SENTENCE_RE.match(out)
            if not m:
                return out
            sentence = m.group(1).strip()
            if sentence:
                callback(sentence)
            out = out[m.end():]
