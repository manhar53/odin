# NABU — Babylonian — divine scribe, son of MARDUK, keeper of the Tablet of Destinies
# Obsidian vault sync: mirrors conversations, caches lookups, indexes Markdown notes,
# and provides keyword-ranked recall over the user's entire vault.

import os
import re
from datetime import datetime
from collections import Counter
from core.marduk import OdinModule


# Tiny English stopword set — kept short on purpose so domain words stay searchable.
_STOP_WORDS = frozenset({
    "the","a","an","is","are","was","were","be","been","being","i","you","he","she",
    "it","we","they","my","your","his","her","its","our","their","this","that",
    "these","those","of","in","on","at","to","for","with","by","from","up","about",
    "into","through","during","and","or","but","if","not","no","do","does","did",
    "have","has","had","will","would","could","should","may","might","what","when",
    "where","why","how","who","which","there","here","so","than","then","now","just",
    "as","also","very","more","most","some","any","all","each","other","such","only",
    "own","same","both","few","much","many","one","two","ok","okay","yeah","yes",
    "lets","let","im","ive","ill","youre","youll","dont","wont","cant",
})

_WORD_RE = re.compile(r"[a-z][a-z0-9'_-]+")


def _slug(s: str) -> str:
    """ASCII-only kebab slug for vault filenames. Length-capped at 60."""
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60] or "canvas"


def _build_canvas_json(center_title: str, sub_topics: list[str]) -> dict:
    """JSON Canvas v1.0 spec (https://jsoncanvas.org). The central node is
    placed at origin; children radiate around it. Layout is intentionally
    sparse and readable — Obsidian's canvas will rerun layout if needed."""
    import math
    nodes = []
    edges = []
    center_id = "n_center"
    nodes.append({
        "id": center_id, "type": "text",
        "x": -150, "y": -50, "width": 300, "height": 100,
        "text": f"# {center_title}", "color": "1",
    })
    if not sub_topics:
        return {"nodes": nodes, "edges": []}
    radius = 350
    for i, topic in enumerate(sub_topics):
        angle = 2 * math.pi * i / max(1, len(sub_topics))
        cx = int(radius * math.cos(angle))
        cy = int(radius * math.sin(angle))
        node_id = f"n_{i}"
        nodes.append({
            "id": node_id, "type": "text",
            "x": cx - 110, "y": cy - 40,
            "width": 220, "height": 80,
            "text": topic, "color": "4",
        })
        edges.append({
            "id": f"e_{i}",
            "fromNode": center_id, "fromSide": "right",
            "toNode":   node_id,   "toSide":   "left",
        })
    return {"nodes": nodes, "edges": edges}


def _tokenize(text: str) -> list[str]:
    tokens = _WORD_RE.findall(text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


class Nabu(OdinModule):
    MODULE_NAME = "NABU"
    LAYER = "MEMORY"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("nabu", {})
        self.vault_root = os.path.expanduser(
            cfg.get("vault_root", "~/Brain")
        )
        self.odin_dir = os.path.join(self.vault_root, "ODIN")
        self.chats_dir = os.path.join(self.odin_dir, "chats")
        self.memory_dir = os.path.join(self.odin_dir, "memory")
        self.lookups_dir = os.path.join(self.odin_dir, "lookups")
        self.max_index_bytes = cfg.get("max_index_bytes", 200_000)
        self._enabled = True
        self._index: dict[str, set[str]] = {}
        self._docs: dict[str, dict] = {}

        if not os.path.isdir(self.vault_root):
            print(f"[NABU] Vault root not found at {self.vault_root}. Sync disabled.")
            self._enabled = False
            return

        try:
            for d in (self.chats_dir, self.memory_dir, self.lookups_dir):
                os.makedirs(d, exist_ok=True)
        except OSError as e:
            print(f"[NABU] Could not create vault dirs: {e}. Sync disabled.")
            self._enabled = False
            return

        self._build_index()
        print(f"[NABU] Scribe online — {len(self._docs)} notes indexed under {self.vault_root}.")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "search_vault",
                "description": "Search the user's Obsidian vault (Markdown notes) for any topic, person, project, or past conversation. Returns ranked excerpts with note paths.",
                "parameters": {
                    "query": {"type": "string", "description": "What to search for"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"}
                },
                "required": ["query"]
            },
            {
                "name": "read_note",
                "description": "Read the full text of a specific note from the vault by its relative path (e.g. 'Notes/Foo.md').",
                "parameters": {
                    "path": {"type": "string", "description": "Relative path inside the vault"}
                },
                "required": ["path"]
            },
            {
                "name": "write_note",
                "description": "Write or append a Markdown note inside ODIN's section of the vault. Path is relative to Brain/ODIN/, e.g. 'memory/topic.md'.",
                "parameters": {
                    "path": {"type": "string", "description": "Relative path under Brain/ODIN/"},
                    "content": {"type": "string", "description": "Markdown content"},
                    "append": {"type": "boolean", "description": "Append if true (default), overwrite if false"}
                },
                "required": ["path", "content"]
            },
            {
                "name": "list_topics",
                "description": "List all topic notes ODIN has written in the vault under Brain/ODIN/memory/.",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "vault_stats",
                "description": "Get statistics about the indexed vault.",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "mirror_message",
                "description": "Append a chat message to today's vault chat log (called automatically per turn).",
                "parameters": {
                    "role": {"type": "string", "description": "user or assistant"},
                    "content": {"type": "string", "description": "message content"}
                },
                "required": ["role", "content"],
                "internal_only": True
            },
            {
                "name": "cache_lookup",
                "description": "Save a short summary of a web/lookup result to the vault for future offline recall (called automatically by other modules).",
                "parameters": {
                    "topic": {"type": "string", "description": "What was looked up"},
                    "summary": {"type": "string", "description": "Short summary"}
                },
                "required": ["topic", "summary"],
                "internal_only": True
            },
            {
                "name": "compose_note",
                "description": "ONE-CALL bundled skill for 'write notes about X' / 'open a doc with what you know about X'. Searches the vault for the topic, formats hits as Markdown, writes to a file on the Desktop, and opens it. PREFER this over chaining search_vault + write_and_open.",
                "parameters": {
                    "topic": {"type": "string", "description": "Subject to compile notes about"},
                    "filename": {"type": "string", "description": "Optional filename (defaults to <topic>_notes.txt on Desktop)"}
                },
                "required": ["topic"]
            },
            {
                "name": "summarize_folder",
                "description": "Generate a hierarchical _summary.md for a vault folder. Reads all top-level .md notes in the folder + any child-folder _summary.md files, sends them to SARASWATI for compression, and writes the rolled-up summary at <folder>/_summary.md. Skips folders whose existing summary is still fresh (no source note modified since).",
                "parameters": {
                    "path": {"type": "string", "description": "Relative path inside the vault (e.g. 'ODIN/memory'). Empty = vault root."},
                    "force": {"type": "boolean", "description": "Re-summarize even if the current _summary.md is fresh"}
                },
                "required": [],
                "internal_only": True
            },
            {
                "name": "build_memory_tree",
                "description": "Walk the vault bottom-up and (re)generate _summary.md for every folder whose content has changed since its last summary. Builds the Karpathy-style hierarchical knowledge tree where each folder rolls up its children.",
                "parameters": {
                    "root": {"type": "string", "description": "Vault subroot to traverse (default 'ODIN')"},
                    "force": {"type": "boolean", "description": "Re-summarize all folders regardless of staleness"}
                },
                "required": [],
                "internal_only": True
            },
            {
                "name": "create_canvas",
                "description": (
                    "Create an Obsidian Canvas (.canvas, JSON Canvas format) "
                    "in the vault. Canvas files render in Obsidian as a visual "
                    "node graph — perfect for mind-mapping a research topic. "
                    "Pass 'nodes' as comma-separated headings; each becomes a "
                    "text node connected to the central topic node."
                ),
                "parameters": {
                    "title": {"type": "string", "description": "Canvas title (also the central node text)"},
                    "nodes": {"type": "string", "description": "Comma-separated sub-topics (optional)"},
                    "path":  {"type": "string", "description": "Relative path under Brain/ODIN/ (default 'canvas/<slug>.canvas')"},
                },
                "required": ["title"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        if not self._enabled and skill_name not in ("vault_stats",):
            return "[NABU] Disabled — vault root not reachable."
        _map = {
            "search_vault":      self._search_vault,
            "read_note":         self._read_note,
            "write_note":        self._write_note,
            "list_topics":       self._list_topics,
            "vault_stats":       self._vault_stats,
            "mirror_message":    self._mirror_message,
            "cache_lookup":      self._cache_lookup,
            "compose_note":      self._compose_note,
            "create_canvas":     self._create_canvas,
            "summarize_folder":  self._summarize_folder,
            "build_memory_tree": self._build_memory_tree,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[NABU] Error: {e}"
        return f"[NABU] Unknown skill: {skill_name}"

    # === Indexing ===

    def _iter_md_files(self):
        for root, dirs, files in os.walk(self.vault_root):
            # Skip hidden dirs (.obsidian, .git, .trash) — these aren't user notes.
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if f.endswith(".md"):
                    yield os.path.join(root, f)

    def _build_index(self):
        self._index.clear()
        self._docs.clear()
        for path in self._iter_md_files():
            self._index_file(path)

    def _index_file(self, path: str):
        try:
            st = os.stat(path)
        except OSError:
            return
        if st.st_size > self.max_index_bytes:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return
        tokens = _tokenize(text)
        counter = Counter(tokens)
        # Drop any prior token entries for this path before re-indexing.
        prior = self._docs.get(path)
        if prior:
            for tok in prior["tokens"]:
                s = self._index.get(tok)
                if s:
                    s.discard(path)
                    if not s:
                        self._index.pop(tok, None)
        self._docs[path] = {"mtime": st.st_mtime, "length": len(tokens), "tokens": counter}
        for tok in counter:
            self._index.setdefault(tok, set()).add(path)

    def _refresh_index(self):
        """Pick up new + changed + deleted files since last scan. Cheap on small vaults."""
        seen = set()
        for path in self._iter_md_files():
            seen.add(path)
            doc = self._docs.get(path)
            try:
                mt = os.path.getmtime(path)
            except OSError:
                continue
            if doc is None or doc.get("mtime") != mt:
                self._index_file(path)
        for stale in [p for p in self._docs if p not in seen]:
            self._unindex_file(stale)

    def _unindex_file(self, path: str):
        doc = self._docs.pop(path, None)
        if not doc:
            return
        for tok in doc["tokens"]:
            s = self._index.get(tok)
            if s:
                s.discard(path)
                if not s:
                    self._index.pop(tok, None)

    # === Search ===

    def _path_weight(self, path: str) -> float:
        """Bias search results by source kind. Chat logs (ODIN/chats/) are
        conversation history — they accumulate every word the user has ever
        said, so without down-weighting they dominate searches for ANY common
        word the user has mentioned in passing. Reference notes (Notes/)
        and research/lookups are the actual knowledge surface."""
        try:
            rel = os.path.relpath(path, self.vault_root).replace("\\", "/")
        except ValueError:
            return 1.0
        if rel.startswith("ODIN/chats/"):    return 0.10  # chat logs — only surface if nothing else matches
        if rel.startswith("ODIN/research/"): return 1.50  # deep research — preferred
        if rel.startswith("ODIN/memory/"):   return 1.20  # ODIN's own topic notes
        if rel.startswith("ODIN/lookups/"):  return 0.80  # cached web lookups
        return 1.00                                       # Notes/, root — full weight

    def _search_vault(self, query: str = "", limit: int = 5) -> str:
        self._refresh_index()
        if not query or not query.strip():
            return "Empty query."
        terms = _tokenize(query)
        if not terms:
            return "No searchable terms in query."
        try:
            limit = max(1, min(20, int(limit)))
        except (TypeError, ValueError):
            limit = 5
        scores: dict[str, float] = {}
        for term in terms:
            for path in self._index.get(term, ()):
                tf = self._docs[path]["tokens"].get(term, 0)
                length = max(self._docs[path]["length"], 1)
                # length-normalized term frequency * path-kind weight
                scores[path] = scores.get(path, 0.0) + (tf / (length ** 0.5)) * self._path_weight(path)
        if not scores:
            return f"Nothing in vault matches: {query}"
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        out = []
        for path, _score in ranked:
            rel = os.path.relpath(path, self.vault_root).replace("\\", "/")
            excerpt = self._first_match_excerpt(path, terms)
            out.append(f"[[{rel}]] — {excerpt}")
        return "\n".join(out)

    def _first_match_excerpt(self, path: str, terms: list[str], width: int = 180) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return ""
        lower = text.lower()
        for term in terms:
            i = lower.find(term)
            if i >= 0:
                start = max(0, i - width // 2)
                end = min(len(text), i + width // 2)
                snippet = text[start:end].replace("\n", " ").strip()
                if start > 0:
                    snippet = "…" + snippet
                if end < len(text):
                    snippet = snippet + "…"
                return snippet
        return text[:width].replace("\n", " ").strip()

    # === Note I/O ===

    def _read_note(self, path: str = "") -> str:
        full = self._resolve_vault_path(path, allow_outside_odin=True)
        if not full:
            return f"Path '{path}' is outside the vault."
        if not os.path.isfile(full):
            return f"Note not found: {path}"
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            return f"Could not read note: {e}"

    def _create_canvas(self, title: str = "", nodes: str = "", path: str = "") -> str:
        """Write an Obsidian Canvas file (.canvas, JSON Canvas v1.0). The
        format is a JSON document with nodes (positioned text rectangles)
        and edges (connections). We lay out a central title node with
        spoke children — minimal but renders cleanly in Obsidian.
        Pattern lifted from kepano's Obsidian skills (canvas spec)."""
        title = (title or "").strip()
        if not title:
            return "Need a canvas title."
        rel_path = path or f"canvas/{_slug(title)}.canvas"
        if not rel_path.endswith(".canvas"):
            rel_path = rel_path + ".canvas"
        full = self._resolve_vault_path(rel_path, allow_outside_odin=False)
        if not full:
            return "Canvas path must be under ODIN/ inside the vault."
        os.makedirs(os.path.dirname(full), exist_ok=True)
        sub_topics = [s.strip() for s in (nodes or "").split(",") if s.strip()]
        canvas_doc = _build_canvas_json(title, sub_topics)
        try:
            with open(full, "w", encoding="utf-8") as f:
                json.dump(canvas_doc, f, indent=2)
            return (f"Created canvas {os.path.relpath(full, self.vault_root)} "
                    f"with {len(sub_topics) + 1} node(s). Open in Obsidian.")
        except OSError as e:
            return f"Could not write canvas: {e}"

    def _write_note(self, path: str = "", content: str = "", append: bool = True) -> str:
        # Writes are restricted to Brain/ODIN/ so we never clobber user notes.
        full = self._resolve_vault_path(path, allow_outside_odin=False)
        if not full:
            return "Writes must target a path under ODIN/ inside the vault."
        os.makedirs(os.path.dirname(full), exist_ok=True)
        mode = "a" if append else "w"
        try:
            with open(full, mode, encoding="utf-8") as f:
                if append and os.path.exists(full) and os.path.getsize(full) > 0:
                    f.write("\n")
                f.write(content)
            self._index_file(full)
            return f"Wrote {os.path.relpath(full, self.vault_root)}."
        except OSError as e:
            return f"Could not write note: {e}"

    def _resolve_vault_path(self, rel_path: str, allow_outside_odin: bool):
        rel_path = (rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not rel_path:
            return None
        if ".." in rel_path.split("/"):
            return None
        if not allow_outside_odin and not rel_path.startswith("ODIN/"):
            rel_path = "ODIN/" + rel_path
        full = os.path.normpath(os.path.join(self.vault_root, rel_path))
        boundary = os.path.normpath(self.vault_root if allow_outside_odin else self.odin_dir)
        if not full.startswith(boundary):
            return None
        if not full.lower().endswith(".md"):
            full += ".md"
        return full

    def _list_topics(self) -> str:
        if not os.path.isdir(self.memory_dir):
            return "No topic notes yet."
        topics = sorted(
            f[:-3] for f in os.listdir(self.memory_dir) if f.endswith(".md")
        )
        if not topics:
            return "No topic notes yet."
        return "Topics: " + ", ".join(topics)

    def _vault_stats(self) -> str:
        if not self._enabled:
            return f"NABU disabled. Vault root expected at {self.vault_root}."
        total_tokens = sum(d["length"] for d in self._docs.values())
        return (f"{len(self._docs)} notes indexed under {self.vault_root}; "
                f"{total_tokens} tokens; {len(self._index)} unique terms.")

    # === Auto-mirroring ===

    def _mirror_message(self, role: str = "", content: str = "") -> str:
        if not content or not content.strip():
            return "skipped"
        date = datetime.now().strftime("%Y-%m-%d")
        ts = datetime.now().strftime("%H:%M")
        path = os.path.join(self.chats_dir, f"{date}.md")
        new_file = not os.path.exists(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                if new_file:
                    f.write(f"# ODIN — {date}\n\n")
                speaker = "**You**" if role == "user" else "**ODIN**"
                f.write(f"- {ts} {speaker}: {content}\n")
            self._index_file(path)
            return "mirrored"
        except OSError as e:
            return f"mirror failed: {e}"

    def _cache_lookup(self, topic: str = "", summary: str = "") -> str:
        if not topic.strip() or not summary.strip():
            return "skipped"
        slug = re.sub(r"[^a-z0-9]+", "-", topic.strip().lower()).strip("-")[:60] or "misc"
        path = os.path.join(self.lookups_dir, f"{slug}.md")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_file = not os.path.exists(path)
        try:
            with open(path, "a", encoding="utf-8") as f:
                if new_file:
                    f.write(f"# {topic.strip()}\n\n")
                f.write(f"- *{ts}*: {summary}\n")
            self._index_file(path)
            return "cached"
        except OSError as e:
            return f"cache failed: {e}"

    def _compose_note(self, topic: str = "", filename: str = "") -> str:
        """Bundled flow: search vault for topic → format hits → write to
        file → open in default app. Bypasses the LLM tool-chain entirely
        for 'write notes about X' commands. About 200ms total instead of
        the 2-3 minute LLM round-trip on CPU."""
        topic = (topic or "").strip()
        if not topic:
            return "Need a topic to compose."
        # Get up to 5 vault hits with longer excerpts than the default search.
        self._refresh_index()
        terms = _tokenize(topic)
        if not terms:
            return f"No searchable terms in '{topic}'."
        scores: dict[str, float] = {}
        for term in terms:
            for path in self._index.get(term, ()):
                tf = self._docs[path]["tokens"].get(term, 0)
                length = max(self._docs[path]["length"], 1)
                # Apply path weighting so compose_note doesn't pull from chat logs.
                scores[path] = scores.get(path, 0.0) + (tf / (length ** 0.5)) * self._path_weight(path)
        if not scores:
            return f"No vault notes match '{topic}'. Nothing to compose."
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:5]

        # Build the document. Title + timestamp + per-source sections with the
        # full first 1500 chars of each note (not just an excerpt).
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        out = [f"# Notes on {topic}\n",
               f"_Compiled by ODIN from your vault on {ts}._\n",
               f"_{len(ranked)} relevant note(s) found._\n", ""]
        for path, _score in ranked:
            rel = os.path.relpath(path, self.vault_root).replace("\\", "/")
            out.append(f"## From [[{rel}]]\n")
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    body = f.read()
            except OSError:
                body = "(unreadable)"
            # Strip frontmatter
            if body.startswith("---"):
                end = body.find("---", 3)
                if end > 0:
                    body = body[end+3:].lstrip()
            out.append(body[:1500].strip())
            if len(body) > 1500:
                out.append("\n_…(truncated)_")
            out.append("\n")
        content = "\n".join(out)

        # Resolve target path. Default = Desktop/<slug>_notes.txt.
        if not filename or not filename.strip():
            slug = re.sub(r"[^a-z0-9]+", "_", topic.lower()).strip("_")[:40] or "notes"
            filename = f"{slug}_notes.txt"
        if not os.path.dirname(filename) and not filename.startswith(("/", "\\", "~")):
            filename = os.path.join(os.path.expanduser("~"), "Desktop", filename)
        if not os.path.splitext(filename)[1]:
            filename += ".txt"
        try:
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            with open(filename, "w", encoding="utf-8") as f:
                f.write(content)
            try:
                os.startfile(filename)
            except OSError:
                pass
            return f"Composed notes on '{topic}' at {filename}."
        except OSError as e:
            return f"Could not compose: {e}"

    # ─────────────────────────────────────────────────────────────────
    # Memory Tree — hierarchical auto-summarization of the vault.
    #
    # The pattern (Karpathy-style "Obsidian wiki" / OpenHuman's Memory Tree):
    # every folder gets a `_summary.md` that compresses its contents +
    # child summaries into a navigable rollup. Asking "what have I learned
    # about X" no longer requires substring-matching 200 notes — the top-
    # level summary points you to the right subtree, and you drill down.
    #
    # Source-of-truth for staleness: file mtime. A folder's _summary.md
    # is stale if ANY .md inside it (or any direct-child _summary.md) was
    # modified after the summary itself. SELENE schedules the bottom-up
    # rebuild on its idle loop so this never runs mid-conversation.
    # ─────────────────────────────────────────────────────────────────

    _SUMMARY_FILENAME = "_summary.md"
    _SUMMARY_MAX_SOURCE_CHARS = 12000   # cap input fed to SARASWATI per folder
    _SUMMARY_SKIP_DIRS = {".obsidian", ".trash", ".git", "node_modules"}

    def _summary_path(self, folder_abs: str) -> str:
        return os.path.join(folder_abs, self._SUMMARY_FILENAME)

    def _folder_is_stale(self, folder_abs: str) -> bool:
        """Has any source note been modified since this folder's _summary.md?"""
        sp = self._summary_path(folder_abs)
        if not os.path.exists(sp):
            return True
        try:
            sm = os.path.getmtime(sp)
        except OSError:
            return True
        try:
            for entry in os.listdir(folder_abs):
                if entry == self._SUMMARY_FILENAME:
                    continue
                full = os.path.join(folder_abs, entry)
                if os.path.isfile(full) and entry.lower().endswith(".md"):
                    if os.path.getmtime(full) > sm:
                        return True
                if os.path.isdir(full):
                    # Child summaries count as inputs to the parent summary.
                    child_sum = self._summary_path(full)
                    if os.path.exists(child_sum) and os.path.getmtime(child_sum) > sm:
                        return True
        except OSError:
            return True
        return False

    def _gather_summary_sources(self, folder_abs: str):
        """Return (concatenated_content, source_list). Sources are top-level
        .md files in this folder PLUS direct-child folders' _summary.md (so
        the rollup is recursive). We don't recurse into grandchild content —
        that's already captured by each child's own _summary.md."""
        parts: list[str] = []
        sources: list[dict] = []
        try:
            entries = sorted(os.listdir(folder_abs))
        except OSError:
            return "", []
        # 1) Top-level notes in this folder (skip the folder's own _summary).
        for entry in entries:
            if entry == self._SUMMARY_FILENAME:
                continue
            full = os.path.join(folder_abs, entry)
            if os.path.isfile(full) and entry.lower().endswith(".md"):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        body = f.read()
                    parts.append(f"\n--- FILE: {entry} ---\n{body}")
                    sources.append({"path": entry, "mtime": os.path.getmtime(full)})
                except OSError:
                    continue
        # 2) Direct child folders' _summary.md (recursive rollup).
        for entry in entries:
            full = os.path.join(folder_abs, entry)
            if os.path.isdir(full) and entry not in self._SUMMARY_SKIP_DIRS \
                    and not entry.startswith("."):
                child_sum = self._summary_path(full)
                if os.path.exists(child_sum):
                    try:
                        with open(child_sum, "r", encoding="utf-8") as f:
                            body = f.read()
                        parts.append(f"\n--- CHILD FOLDER SUMMARY: {entry}/ ---\n{body}")
                        sources.append({
                            "path":  f"{entry}/{self._SUMMARY_FILENAME}",
                            "mtime": os.path.getmtime(child_sum),
                        })
                    except OSError:
                        continue
        return "".join(parts).strip(), sources

    def _summarize_folder(self, path: str = "", force: bool = False) -> str:
        """Generate (or refresh) a _summary.md for one vault folder."""
        rel = (path or "").strip()
        folder_abs = os.path.normpath(os.path.join(self.vault_root, rel)) if rel else self.vault_root
        if not folder_abs.startswith(os.path.normpath(self.vault_root)):
            return "[NABU] summarize_folder: path must be inside the vault."
        if not os.path.isdir(folder_abs):
            return f"[NABU] summarize_folder: not a folder: {folder_abs}"

        if not force and not self._folder_is_stale(folder_abs):
            return f"[NABU] {rel or '<root>'} summary is fresh — no rebuild needed."

        content, sources = self._gather_summary_sources(folder_abs)
        if not content:
            return f"[NABU] {rel or '<root>'} has no markdown content to summarize."

        # Trim to keep the SARASWATI call cheap. TokenDiet handles the heavy
        # lifting; the explicit cap is a hard backstop.
        try:
            from core.token_diet import diet
            content = diet(content, max_chars=self._SUMMARY_MAX_SOURCE_CHARS)
        except Exception:
            content = content[:self._SUMMARY_MAX_SOURCE_CHARS]

        if not self.marduk:
            return "[NABU] summarize_folder needs MARDUK access to reach SARASWATI."
        sara = self.marduk.get_module("SARASWATI")
        if not sara:
            return "[NABU] summarize_folder needs SARASWATI to be online."

        prompt = (
            f"Below are the notes inside the vault folder '{rel or '<root>'}'. "
            f"Some entries are direct notes; CHILD FOLDER SUMMARY blocks come "
            f"from sub-folder rollups (already summarized once — preserve their "
            f"essence). Produce a hierarchical Markdown summary that:\n"
            f"  • opens with 2-4 bullet 'topics covered' headlines,\n"
            f"  • follows with one paragraph per topic, citing source filenames "
            f"in parens, and\n"
            f"  • ends with a single 'see also' line pointing at child folders worth drilling into.\n"
            f"Tone: terse, encyclopedic, never restate the contents verbatim.\n\n"
            f"{content}"
        )

        try:
            summary = sara.execute("ask_ai", {"question": prompt})
        except Exception as e:
            return f"[NABU] SARASWATI summarization failed: {e}"
        if not summary or summary.startswith("[SARASWATI]") or summary.startswith("Cloud"):
            return f"[NABU] summarization returned no usable content: {summary[:120]}"

        # Frontmatter records what we summarized so future runs can detect staleness.
        from datetime import datetime
        header_lines = ["---",
                        f"generated: {datetime.now().isoformat(timespec='seconds')}",
                        f"folder: {rel or '<root>'}",
                        f"source_count: {len(sources)}",
                        "sources:"]
        for s in sources:
            header_lines.append(f"  - path: {s['path']}")
            header_lines.append(f"    mtime: {datetime.fromtimestamp(s['mtime']).isoformat(timespec='seconds')}")
        header_lines.append("---")
        header = "\n".join(header_lines) + "\n\n"

        try:
            with open(self._summary_path(folder_abs), "w", encoding="utf-8") as f:
                f.write(header + summary.strip() + "\n")
        except OSError as e:
            return f"[NABU] could not write _summary.md: {e}"
        return f"[NABU] summarized {rel or '<root>'} ({len(sources)} sources → {len(summary)} chars)"

    def _build_memory_tree(self, root: str = "ODIN", force: bool = False) -> str:
        """Bottom-up walk: summarize every folder whose summary is stale. Child
        folders are processed before their parents so the parent rollup sees
        fresh _summary.md content from its children."""
        root_abs = os.path.normpath(os.path.join(self.vault_root, root or ""))
        if not root_abs.startswith(os.path.normpath(self.vault_root)):
            return "[NABU] build_memory_tree: root must be inside the vault."
        if not os.path.isdir(root_abs):
            return f"[NABU] build_memory_tree: not a folder: {root_abs}"

        # Collect all folders DFS bottom-up. os.walk topdown=False gives us
        # leaves first, which is exactly what we need for child→parent rollup.
        folders: list[str] = []
        for dirpath, dirnames, _ in os.walk(root_abs, topdown=False):
            dirnames[:] = [d for d in dirnames if d not in self._SUMMARY_SKIP_DIRS
                           and not d.startswith(".")]
            folders.append(dirpath)

        built = 0
        skipped = 0
        for folder in folders:
            rel = os.path.relpath(folder, self.vault_root).replace(os.sep, "/")
            if not force and not self._folder_is_stale(folder):
                skipped += 1
                continue
            try:
                result = self._summarize_folder(rel, force=force)
                if result.startswith("[NABU] summarized"):
                    built += 1
                else:
                    skipped += 1
            except Exception as e:
                print(f"[NABU] build_memory_tree: {rel} failed: {e}")
                skipped += 1
        return f"[NABU] memory tree: {built} folders refreshed, {skipped} unchanged"
