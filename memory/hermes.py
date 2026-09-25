# HERMES — Greek — swift messenger, retriever and deliverer
# Recall: retrieves specific memories from THOTH on demand

import json
import math
import os
import re
import threading
from core.marduk import OdinModule


def _tokenize(text: str) -> list[str]:
    """Lower-cased \\w+ tokens, naive — good enough for RRF lexical ranking."""
    return re.findall(r"\w+", (text or "").lower())

try:
    import ollama
    _HAS_OLLAMA = True
except ImportError:
    _HAS_OLLAMA = False


_EMBED_MODEL = "nomic-embed-text"   # 137M-param, 768-dim, ~270MB, runs on Ollama
_EMBED_CACHE_PATH = "data/memory/hermes_embeddings.json"


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class Hermes(OdinModule):
    MODULE_NAME = "HERMES"
    LAYER = "MEMORY"

    def __init__(self, config: dict):
        super().__init__(config)
        host = (config or {}).get("gilgamesh", {}).get("host", "http://localhost:11434")
        self._client = ollama.Client(host=host) if _HAS_OLLAMA else None
        # message-content -> embedding vector. Persisted so a restart doesn't
        # require re-embedding the whole session.
        self._cache: dict[str, list[float]] = self._load_cache()
        self._cache_lock = threading.Lock()
        # Probe the embedding model once. If the user hasn't pulled it,
        # we silently fall back to substring search.
        self._embed_ok = False
        if self._client:
            try:
                v = self._embed("ODIN")
                self._embed_ok = bool(v)
            except Exception as e:
                print(f"[HERMES] embedding model unavailable ({e}); using substring search. "
                      f"Run: ollama pull {_EMBED_MODEL}")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "search_memory",
                "description": "Search conversation history for a specific topic or keyword",
                "parameters": {
                    "query": {"type": "string", "description": "What to search for in memory"}
                },
                "required": ["query"],
                "internal_only": True
            },
            {
                "name": "get_last_exchange",
                "description": "Retrieve the most recent conversation exchange",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "summarize_session",
                "description": "Summarize what has been discussed in this session",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "search_memory": self._search,
            "get_last_exchange": self._last_exchange,
            "summarize_session": self._summarize,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[HERMES] Error: {e}"
        return f"[HERMES] Unknown skill: {skill_name}"

    def _search(self, query: str = "") -> str:
        """Hybrid retrieval: rank by semantic cosine AND by lexical token overlap,
        then fuse with Reciprocal Rank Fusion. Catches both 'find the message
        about cryptocurrency' (semantic) AND 'find when I said apple' (lexical).
        Lifted from the standard langchain/RAG playbook — fusion is provably
        stronger than either ranker alone, and stays a 30-line pure-python add."""
        thoth = self.marduk.get_module("THOTH") if self.marduk else None
        if not thoth:
            return "Memory store not available."
        messages = thoth.execute("get_messages", {})
        if not isinstance(messages, list) or not messages:
            return "No messages found."

        # Lexical rank — token-overlap ratio. Cheap, handles exact-term recall.
        q_tokens = {t for t in _tokenize(query) if len(t) > 1}
        lex_scored: list[tuple[float, int]] = []
        for idx, m in enumerate(messages):
            content = m.get("content", "")
            if not content:
                continue
            m_tokens = {t for t in _tokenize(content) if len(t) > 1}
            if not m_tokens or not q_tokens:
                continue
            overlap = len(q_tokens & m_tokens) / len(q_tokens)
            if overlap > 0:
                lex_scored.append((overlap, idx))
        lex_scored.sort(key=lambda t: t[0], reverse=True)

        # Semantic rank — cosine on nomic-embed vectors, when available.
        sem_scored: list[tuple[float, int]] = []
        if self._embed_ok:
            try:
                qvec = self._embed(query)
                if qvec:
                    for idx, m in enumerate(messages):
                        content = m.get("content", "")
                        if not content:
                            continue
                        vec = self._get_or_embed(content)
                        if vec:
                            sem_scored.append((_cosine(qvec, vec), idx))
                    sem_scored.sort(key=lambda t: t[0], reverse=True)
                    # Only keep semantically plausible matches in the fused pool.
                    sem_scored = [(s, i) for s, i in sem_scored if s > 0.35]
            except Exception as e:
                print(f"[HERMES] semantic search failed ({e}); using lexical only.")

        # Reciprocal Rank Fusion. Each ranker contributes 1/(60 + rank); k=60
        # is the canonical RRF constant from Cormack et al. (2009).
        if not lex_scored and not sem_scored:
            return f"No memory found for: {query}"
        rrf: dict[int, float] = {}
        for rank, (_, idx) in enumerate(lex_scored[:20]):
            rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (60.0 + rank)
        for rank, (_, idx) in enumerate(sem_scored[:20]):
            rrf[idx] = rrf.get(idx, 0.0) + 1.0 / (60.0 + rank)
        ordered = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)[:5]
        if not ordered:
            return f"No memory found for: {query}"
        return "\n".join(f"[{messages[i]['role']}] {messages[i]['content']}" for i, _ in ordered)

    # === Embedding helpers ============================================
    def _embed(self, text: str) -> list[float]:
        if not self._client:
            return []
        # keep_alive: the embed model must STAY resident. Without it Ollama
        # unloads nomic-embed after ~5 min and every semantic-route/recall
        # pays a ~14s cold reload mid-conversation (the "fast_route=16011ms"
        # mystery from the 2026-07-11 field test).
        resp = self._client.embeddings(model=_EMBED_MODEL, prompt=text,
                                       keep_alive="30m")
        return list(resp.get("embedding") or [])

    def _get_or_embed(self, text: str) -> list[float]:
        """Memoize per-message embeddings on disk so a session can be searched
        without re-running the model on every prior turn."""
        with self._cache_lock:
            cached = self._cache.get(text)
        if cached:
            return cached
        vec = self._embed(text)
        if vec:
            with self._cache_lock:
                self._cache[text] = vec
                # Cap cache at 2k entries — first-in-first-out trim, dirt simple.
                if len(self._cache) > 2000:
                    keys = list(self._cache.keys())
                    for k in keys[:500]:
                        del self._cache[k]
                self._save_cache()
        return vec

    def _load_cache(self) -> dict[str, list[float]]:
        if os.path.exists(_EMBED_CACHE_PATH):
            try:
                with open(_EMBED_CACHE_PATH) as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(_EMBED_CACHE_PATH), exist_ok=True)
            with open(_EMBED_CACHE_PATH, "w") as f:
                json.dump(self._cache, f)
        except Exception:
            pass

    def _last_exchange(self) -> str:
        thoth = self.marduk.get_module("THOTH") if self.marduk else None
        if not thoth:
            return "Memory store not available."
        messages = thoth.execute("get_messages", {})
        if not isinstance(messages, list) or not messages:
            return "No conversation history."
        last = messages[-2:] if len(messages) >= 2 else messages
        return "\n".join(f"[{m['role']}] {m['content']}" for m in last)

    def _summarize(self) -> str:
        thoth = self.marduk.get_module("THOTH") if self.marduk else None
        if not thoth:
            return "Memory store not available."
        messages = thoth.execute("get_messages", {})
        if not isinstance(messages, list) or not messages:
            return "No session to summarize."
        topics = []
        for m in messages:
            if m.get("role") == "user" and len(m.get("content", "")) > 5:
                topics.append(m["content"][:60])
        if not topics:
            return "Session has no recorded topics."
        return f"Session covered {len(topics)} exchanges. Topics included: " + "; ".join(topics[:5])
