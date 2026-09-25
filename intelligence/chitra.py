# CHITRA — short for Chitragupta, Hindu scribe-god who keeps the eternal
# ledger of every soul's deeds. In ODIN, CHITRA is the bridge to the user's
# Google Drive: 5 TB of personal documents, papers, photos, screenshots,
# emails-exported, books. ODIN can search, fetch, and answer questions
# grounded in the user's own corpus — not just public web.
#
# Architectural placement: intelligence/ alongside SARASWATI / AKASHA. Like
# them, CHITRA is a knowledge gateway — not perception, not effecting.
#
# Two access modes — picked at init, both can coexist:
#
#   1. LOCAL MOUNT (default, zero-config).  Google Drive for Desktop mounts
#      the user's Drive at G:\My Drive (or %USERPROFILE%\My Drive depending
#      on mirror/stream mode). When that path exists, CHITRA reads files
#      directly from disk — no OAuth, no API quota, no Cloud Console. This
#      is the path that works "out of the box" the moment Drive desktop is
#      signed in.
#
#   2. DRIVE API (optional, advanced).  OAuth 2.0 installed-app flow,
#      drive.readonly scope. Required only for full-text search across
#      Google Docs / Sheets / Slides (which appear as .gdoc shortcut files
#      on the local mount, not their actual content). Falls back gracefully
#      when credentials.json is missing.
#
# Each skill prefers local mount when available, falls back to API for
# Google-native files. Both modes off → skill returns a clear setup hint.
#
# Read-only by design. WRITES belong to the user's intentional actions
# through their normal Drive client.

import io
import json
import os
import re
from datetime import datetime, timedelta
from core.marduk import OdinModule

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
    from googleapiclient.errors import HttpError
    _HAS_GOOGLE = True
except ImportError:
    _HAS_GOOGLE = False


_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
_DEFAULT_CREDS_PATH = "data/secrets/google_oauth_credentials.json"
_DEFAULT_TOKEN_PATH = "data/secrets/google_oauth_token.json"
_CACHE_DIR = "data/knowledge/chitra_cache"

_EXPORT_FORMATS = {
    "application/vnd.google-apps.document":     "text/plain",
    "application/vnd.google-apps.spreadsheet":  "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

# Common Google Drive desktop mount points on Windows. Stream-mode uses a
# drive letter (default G:); mirror-mode mounts under the user profile.
# Checked at init in this priority order; first match wins.
_LOCAL_MOUNT_CANDIDATES = [
    r"G:\My Drive",
    r"H:\My Drive",
    r"I:\My Drive",
    os.path.expandvars(r"%USERPROFILE%\My Drive"),
    os.path.expandvars(r"%USERPROFILE%\Google Drive"),
]

# File extensions worth full-text-scanning during local search. PDFs handled
# via pypdf inside _local_extract; the rest are read as utf-8.
_TEXTUAL_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yml",
    ".yaml", ".log", ".html", ".htm", ".xml", ".py", ".js", ".ts", ".java",
    ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".rb", ".sh", ".ps1", ".sql",
})

# .gdoc / .gsheet / .gslides are JSON shortcut files written by Drive
# desktop. They contain {doc_id, url, ...} — not the document content.
# We surface them in search results (name match works), and route fetch
# through the API when available, otherwise we extract the URL/id so the
# user at least gets a clickable link.
_GDRIVE_SHORTCUT_EXTS = frozenset({".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform"})


class Chitra(OdinModule):
    MODULE_NAME = "CHITRA"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("chitra", {})
        self.credentials_path = cfg.get("credentials_path", _DEFAULT_CREDS_PATH)
        self.token_path = cfg.get("token_path", _DEFAULT_TOKEN_PATH)
        self.cache_dir = cfg.get("cache_dir", _CACHE_DIR)
        self.max_file_bytes = int(cfg.get("max_file_bytes", 5 * 1024 * 1024))
        self.scope_folder_id: str = (cfg.get("scope_folder_id") or "").strip()
        # Local mount detection — explicit config wins, then auto-probe.
        explicit_mount = (cfg.get("local_mount_path") or "").strip()
        if explicit_mount:
            self._local_root = explicit_mount if os.path.isdir(explicit_mount) else ""
        else:
            self._local_root = self._auto_detect_local_mount()
        # Walk-time safety caps so a CHITRA search on a 5 TB drive doesn't
        # take minutes. Anything past these is silently skipped.
        self.max_walk_files = int(cfg.get("max_walk_files", 50000))
        self.max_content_scan_bytes = int(cfg.get("max_content_scan_bytes", 2 * 1024 * 1024))
        # File-index lifecycle. Without an index, every search re-walks the
        # entire mount and re-opens PDFs — 27-minute searches on 5 TB. With
        # an index, the first walk is one-time and subsequent searches are
        # name-matches against a JSON list (<1s). Stale after TTL hours.
        self.index_ttl_hours = int(cfg.get("index_ttl_hours", 24))
        self.index_path = cfg.get("index_path", os.path.join(self.cache_dir, "index.json"))
        # Cap content-scan work per search so an ambiguous query doesn't
        # tank latency. Content scan is ONLY used when name match yields
        # too few hits — most personal Drives have rich filenames.
        self.max_content_scan_files = int(cfg.get("max_content_scan_files", 20))
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)

        self._service = None
        self.enabled = False

        # API path — optional. Skipped silently if no credentials.json.
        if _HAS_GOOGLE and os.path.exists(self.credentials_path):
            try:
                self._service = self._build_service(interactive=False)
            except Exception as e:
                print(f"[CHITRA] API init failed (will retry on first call): {e}")

        # Status banner — clear about which mode(s) are live.
        modes = []
        if self._local_root:
            modes.append(f"local mount ({self._local_root})")
        if self._service:
            modes.append("Drive API")
        elif _HAS_GOOGLE and os.path.exists(self.credentials_path):
            modes.append("Drive API (auth pending)")
        if modes:
            self.enabled = True
            print(f"[CHITRA] Online — {', '.join(modes)}.")
        else:
            print("[CHITRA] No access path configured. Either install Google Drive "
                  "for Desktop (zero-config), or set chitra.local_mount_path / "
                  "drop OAuth credentials at data/secrets/google_oauth_credentials.json.")

    # ── Skill registration ───────────────────────────────────────────
    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "drive_search",
                "description": (
                    "Search the user's Google Drive for files by name and content "
                    "(full-text where supported). Reads the locally-mounted Drive "
                    "(G:\\My Drive via Drive desktop) when available — instant, no "
                    "API quota — and falls back to the Drive API for Google Docs "
                    "content. Use for 'find X in my drive' / 'where did I save Y'."
                ),
                "parameters": {
                    "query": {"type": "string", "description": "Search terms"},
                    "limit": {"type": "integer", "description": "Number of results (default 5, max 20)"},
                },
                "required": ["query"],
            },
            {
                "name": "drive_fetch",
                "description": (
                    "Fetch a file from the user's Drive and return extracted text. "
                    "Accepts a name (search-first) or a local path. PDFs parsed via "
                    "pypdf, images via HORUS OCR, .docx via python-docx."
                ),
                "parameters": {
                    "name_or_id": {"type": "string", "description": "File name, local path, or Drive file id"},
                    "max_chars": {"type": "integer", "description": "Trim output (default 4000)"},
                },
                "required": ["name_or_id"],
            },
            {
                "name": "drive_recent",
                "description": "List files in the user's Drive modified in the last N days.",
                "parameters": {
                    "days": {"type": "integer", "description": "Look-back window (default 7)"},
                    "limit": {"type": "integer", "description": "How many files (default 10)"},
                },
                "required": [],
            },
            {
                "name": "reindex_drive",
                "description": (
                    "Force CHITRA to rebuild its index of the local Drive mount. "
                    "Use after pinning many new files in Drive desktop or after a "
                    "long delay where files have been added. Normally the index "
                    "auto-refreshes every 24h."
                ),
                "parameters": {},
                "required": [],
            },
            {
                "name": "ask_my_drive",
                "description": (
                    "Answer a question grounded in the user's Drive corpus. Searches "
                    "Drive, fetches the top-relevant documents, asks SARASWATI cloud "
                    "LLM with their CONTENT as context. Use for 'what did that paper "
                    "say about X' / 'summarize my notes on Y'."
                ),
                "parameters": {
                    "question": {"type": "string", "description": "Natural-language question about the user's Drive"},
                },
                "required": ["question"],
            },
            {
                "name": "drive_ingest_recent",
                "description": (
                    "Fetch recently-modified Drive files and store summarized "
                    "Markdown notes in the vault at Brain/ODIN/drive/. Skips "
                    "files already ingested in this run window. Called by "
                    "SELENE on its 20-min loop; can also be invoked on demand."
                ),
                "parameters": {
                    "days":  {"type": "integer", "description": "Look-back window (default 2)"},
                    "limit": {"type": "integer", "description": "Max files per pass (default 6, max 20)"},
                },
                "required": [],
                "internal_only": True,
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        if not self.enabled:
            return ("Drive integration unavailable. Install Google Drive for Desktop "
                    "(simplest) or configure OAuth credentials.")
        _map = {
            "drive_search":         self._drive_search,
            "drive_fetch":          self._drive_fetch,
            "drive_recent":         self._drive_recent,
            "ask_my_drive":         self._ask_my_drive,
            "reindex_drive":        self._reindex_drive,
            "drive_ingest_recent":  self._drive_ingest_recent,
        }
        fn = _map.get(skill_name)
        if not fn:
            return f"[CHITRA] Unknown skill: {skill_name}"
        try:
            return fn(**args)
        except HttpError as e:
            status = getattr(e.resp, "status", "?")
            return f"[CHITRA] Drive API error ({status}): {e}"
        except Exception as e:
            return f"[CHITRA] Error: {e}"

    # ── Skill implementations (mode-routed) ─────────────────────────
    def _drive_search(self, query: str = "", limit: int = 5) -> str:
        query = (query or "").strip()
        if not query:
            return "Need a search query."
        try:
            limit = max(1, min(20, int(limit or 5)))
        except (TypeError, ValueError):
            limit = 5
        if self._local_root:
            hits = self._local_search(query, limit)
            if hits:
                return self._format_hits(hits)
            # Local found nothing — try the API for Google Docs content,
            # which lives only in the cloud, not on disk.
            if self._lazy_api():
                return self._api_search(query, limit)
            return f"No files matched '{query}' in the local mount."
        if self._lazy_api():
            return self._api_search(query, limit)
        return "Drive not accessible."

    def _drive_fetch(self, name_or_id: str = "", max_chars: int = 4000) -> str:
        name_or_id = (name_or_id or "").strip()
        if not name_or_id:
            return "Need a file name, path, or id."
        try:
            max_chars = max(500, min(50000, int(max_chars or 4000)))
        except (TypeError, ValueError):
            max_chars = 4000
        # If it's an absolute path that exists, read directly.
        if os.path.isabs(name_or_id) and os.path.exists(name_or_id):
            text = self._local_extract(name_or_id)
            if text:
                return self._trim(text, max_chars, name_or_id)
            return f"Could not extract text from {name_or_id}."
        # Local mount: search by name → first hit → extract.
        if self._local_root:
            hits = self._local_search(name_or_id, limit=1)
            if hits:
                path = hits[0]["path"]
                text = self._local_extract(path)
                if text:
                    return self._trim(text, max_chars, hits[0]["name"])
        # API path for Google Docs / Sheets / Slides content.
        if self._lazy_api():
            file_id, file_name, mime = self._api_resolve_id(name_or_id)
            if file_id:
                text = self._api_fetch_and_extract(file_id, mime)
                if text:
                    return self._trim(text, max_chars, file_name)
        return f"No Drive file matched '{name_or_id}'."

    def _drive_recent(self, days: int = 7, limit: int = 10) -> str:
        try:
            days = max(1, min(365, int(days or 7)))
        except (TypeError, ValueError):
            days = 7
        try:
            limit = max(1, min(50, int(limit or 10)))
        except (TypeError, ValueError):
            limit = 10
        if self._local_root:
            recent = self._local_recent(days, limit)
            if recent:
                return "\n".join(f"• {h['name']} [{h['ext']}]  ({h['mtime_short']})  {h['rel_path']}"
                                 for h in recent)
            return f"No files modified in the local mount in the last {days} day(s)."
        if self._lazy_api():
            return self._api_recent(days, limit)
        return "Drive not accessible."

    def _drive_ingest_recent(self, days: int = 2, limit: int = 6) -> str:
        """Pull recent Drive files, fetch text, summarize via SARASWATI, write
        to <vault>/ODIN/drive/. Skips files already ingested (tracked by a
        small ledger so we don't redo on every loop pass).

        Conservative defaults: 2 days back, 6 files per pass. SELENE fires
        this every 20 min — over an hour that's enough catch-up cadence
        without burning quota."""
        if not self.marduk:
            return "[CHITRA] ingest needs MARDUK access."
        nabu = self.marduk.get_module("NABU")
        sara = self.marduk.get_module("SARASWATI")
        if not nabu or not nabu._enabled:
            return "[CHITRA] ingest needs NABU vault active."
        if not sara or not getattr(sara, "providers", None):
            return "[CHITRA] ingest needs a SARASWATI cloud provider configured."

        try:
            days  = max(1, min(30, int(days or 2)))
            limit = max(1, min(20, int(limit or 6)))
        except (TypeError, ValueError):
            days, limit = 2, 6

        # File-level dedup ledger so we don't re-ingest the same file every loop.
        ledger_path = os.path.join("data", "knowledge", "chitra_ingest_ledger.json")
        try:
            os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
            if os.path.exists(ledger_path):
                with open(ledger_path, "r", encoding="utf-8") as f:
                    ledger = json.load(f)
            else:
                ledger = {}
        except (OSError, json.JSONDecodeError):
            ledger = {}

        # Source the recent list. Prefer local mount (free), fall back to API.
        candidates = []
        if self._local_root:
            candidates = self._local_recent(days, limit * 3)   # over-fetch then filter
        elif self._lazy_api():
            # Reuse the API path text-output by parsing — cheaper than refactor.
            api_text = self._api_recent(days, limit * 3)
            # We can't easily re-extract structured info from the API text,
            # so on API-only setups we currently skip ingest. Local-mount
            # users get the full ride.
            return "[CHITRA] ingest currently requires Google Drive for Desktop (local mount)."
        else:
            return "[CHITRA] ingest skipped — Drive not accessible."

        # Filter: already-ingested + this run's window.
        from datetime import datetime
        targets = []
        for h in candidates:
            key = h.get("rel_path") or h.get("name", "")
            mtime = h.get("mtime", 0)
            prev_mtime = ledger.get(key, {}).get("mtime", 0)
            if mtime > prev_mtime:
                targets.append((key, h))
            if len(targets) >= limit:
                break

        if not targets:
            return "[CHITRA] ingest: no new Drive files."

        # Vault destination.
        vault_dir = os.path.join(nabu.odin_dir, "drive")
        try:
            os.makedirs(vault_dir, exist_ok=True)
        except OSError as e:
            return f"[CHITRA] could not create vault drive dir: {e}"

        from core.token_diet import diet
        ingested = 0
        for key, h in targets:
            name = h.get("name", "untitled")
            path = h.get("abs_path") or h.get("path") or h.get("rel_path")
            if not path:
                continue
            # Pull text via the same extractor _drive_fetch uses.
            try:
                text = self._local_extract(path)
            except Exception as e:
                text = f"(extract failed: {e})"
            if not text or len(text) < 80:
                # Skip near-empty files — they yield useless summaries.
                ledger[key] = {"mtime": h.get("mtime", 0), "ingested_at": datetime.now().isoformat(timespec="seconds"), "skipped": True}
                continue
            clean = diet(text, max_chars=8000)
            try:
                summary = sara.execute("ask_ai", {
                    "question": (
                        f"Summarize this Drive file in 4-8 lines of Markdown. Front-load the gist. "
                        f"Keep specific names / dates / numbers. End with one 'context' bullet noting "
                        f"what kind of file this is (memo, code, spreadsheet, paper, etc.).\n\n"
                        f"FILENAME: {name}\n\n{clean}"
                    )
                })
            except Exception as e:
                summary = f"(summary failed: {e})"
            # Vault note.
            slug = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(name)[0])[:80] or "file"
            out_path = os.path.join(vault_dir, f"{slug}.md")
            try:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(f"---\n")
                    f.write(f"source: drive\n")
                    f.write(f"file: {name}\n")
                    f.write(f"path: {h.get('rel_path', path)}\n")
                    f.write(f"modified: {datetime.fromtimestamp(h.get('mtime', 0)).isoformat(timespec='seconds')}\n")
                    f.write(f"ingested: {datetime.now().isoformat(timespec='seconds')}\n")
                    f.write(f"---\n\n# {name}\n\n{summary.strip()}\n")
                ingested += 1
                ledger[key] = {"mtime": h.get("mtime", 0), "ingested_at": datetime.now().isoformat(timespec="seconds"), "vault_path": out_path}
            except OSError as e:
                print(f"[CHITRA] write {out_path} failed: {e}")

        # Persist ledger.
        try:
            with open(ledger_path, "w", encoding="utf-8") as f:
                json.dump(ledger, f, indent=2)
        except OSError:
            pass

        return f"[CHITRA] drive ingest: {ingested}/{len(targets)} files summarized into vault"

    def _ask_my_drive(self, question: str = "") -> str:
        question = (question or "").strip()
        if not question:
            return "Need a question."
        if not self.marduk:
            return "[CHITRA] MARDUK not connected."
        sara = self.marduk.get_module("SARASWATI")
        if not sara or not getattr(sara, "providers", None):
            return ("No cloud LLM configured — can find Drive files but can't "
                    "synthesize. Configure SARASWATI providers first.")
        terms = _extract_keywords(question)
        if not terms:
            return f"Couldn't extract usable keywords from: {question}"
        # Pick whichever search path works.
        hits = []
        if self._local_root:
            hits = self._local_search(" ".join(terms), limit=5)
        if not hits and self._lazy_api():
            api_hits = self._api_search_for_keywords(terms, limit=5)
            hits = [{"path": None, "name": h.get("name", ""),
                     "ext": _short_mime(h.get("mimeType", "")), "id": h.get("id"),
                     "mime": h.get("mimeType", "")} for h in api_hits]
        if not hits:
            return f"No Drive files matched any keywords from: {question}"
        # Fetch + extract top 3 by whichever path is appropriate.
        snippets: list[str] = []
        for h in hits[:3]:
            try:
                if h.get("path"):
                    text = self._local_extract(h["path"])
                elif h.get("id"):
                    text = self._api_fetch_and_extract(h["id"], h.get("mime", ""))
                else:
                    text = ""
                if text:
                    snippets.append(f"### From: {h['name']}\n{text[:2500]}")
            except Exception:
                continue
        if not snippets:
            return "Found matching files but couldn't extract their contents."
        context = "\n\n---\n\n".join(snippets)
        prompt = (
            f"The user asked: {question}\n\n"
            f"Their relevant Drive documents (use these as ground truth; cite "
            f"file names where helpful):\n\n{context}"
        )
        return sara.execute("ask_ai", {"question": prompt})

    # ── Local mount path ─────────────────────────────────────────────
    @staticmethod
    def _auto_detect_local_mount() -> str:
        for path in _LOCAL_MOUNT_CANDIDATES:
            if path and os.path.isdir(path):
                return path
        return ""

    def _local_search(self, query: str, limit: int) -> list[dict]:
        """Search the file index (built on first call, cached on disk).
        Ranks by filename token-overlap; falls back to a SMALL content scan
        on the top-name-misses only if name hits are thin. Sub-second on a
        50k-file index after the first walk."""
        q_lower = query.lower()
        q_tokens = [t for t in re.findall(r"\w+", q_lower) if len(t) >= 2]
        if not q_tokens:
            return []
        index = self._get_index()
        if not index:
            return []
        name_hits: list[tuple[float, dict]] = []
        for entry in index:
            name_lower = entry["name"].lower()
            n_score = sum(1 for t in q_tokens if t in name_lower)
            if q_lower in name_lower:
                n_score += 2
            if n_score > 0:
                name_hits.append((float(n_score), entry))
        name_hits.sort(key=lambda t: t[0], reverse=True)

        # If name match was strong, return early — no content scan needed.
        if len(name_hits) >= limit:
            return [e for _, e in name_hits[:limit]]

        # Thin name results → do a SMALL, BOUNDED content scan over the
        # most-recently-modified textual files. Capped at max_content_scan_files
        # so a 5 TB drive can't blow our budget.
        content_hits: list[tuple[float, dict]] = []
        scan_pool = [e for e in index
                     if (e["ext"] in _TEXTUAL_EXTS or e["ext"] == ".pdf")
                     and e["size"] <= self.max_content_scan_bytes]
        scan_pool.sort(key=lambda e: e["mtime"], reverse=True)
        scanned = 0
        for entry in scan_pool:
            if scanned >= self.max_content_scan_files:
                break
            scanned += 1
            c_score = self._content_match_score(entry["path"], q_tokens)
            if c_score > 0:
                content_hits.append((c_score, entry))
        content_hits.sort(key=lambda t: t[0], reverse=True)

        seen: set[str] = set()
        ordered: list[dict] = []
        for _, e in name_hits + content_hits:
            if e["path"] in seen:
                continue
            seen.add(e["path"])
            ordered.append(e)
            if len(ordered) >= limit:
                break
        return ordered

    def _local_recent(self, days: int, limit: int) -> list[dict]:
        cutoff = (datetime.now() - timedelta(days=days)).timestamp()
        index = self._get_index()
        recents = [e for e in index if e["mtime"] >= cutoff]
        recents.sort(key=lambda e: e["mtime"], reverse=True)
        return recents[:limit]

    # ── File index (lazy build, on-disk cache, TTL) ────────────────
    def _get_index(self) -> list[dict]:
        """Return the local file index. Builds it on first call, caches to
        disk, re-uses for `index_ttl_hours` hours. After TTL, rebuilds on
        next call. Skips dotfiles, node_modules, ODIN's own data folder."""
        if os.path.exists(self.index_path):
            try:
                age_hours = (datetime.now().timestamp() - os.path.getmtime(self.index_path)) / 3600
                if age_hours < self.index_ttl_hours:
                    with open(self.index_path, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    if isinstance(cached, list):
                        return cached
            except (OSError, json.JSONDecodeError):
                pass
        # (Re)build.
        print(f"[CHITRA] Building file index of {self._local_root} "
              f"(one-time, cached for {self.index_ttl_hours}h)...")
        t0 = datetime.now()
        index = self._build_index()
        elapsed = (datetime.now() - t0).total_seconds()
        print(f"[CHITRA] Index built: {len(index)} files in {elapsed:.1f}s.")
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(index, f)
        except OSError as e:
            self._log.warning(f"index save failed: {e}")
        return index

    def _build_index(self) -> list[dict]:
        root = self._local_root
        sub = (self.config.get("chitra", {}).get("scope_local_subdir") or "").strip()
        if sub:
            cand = os.path.join(root, sub)
            if os.path.isdir(cand):
                root = cand
        index: list[dict] = []
        count = 0
        # Skip these directory names anywhere in the tree — they're junk for
        # personal-Drive search and would balloon the index.
        skip_dirs = {".trash", ".tmp", "node_modules", "__pycache__", ".git", ".venv", "venv"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d.lower() not in skip_dirs]
            for fname in filenames:
                if fname.startswith(".") or fname.lower() == "desktop.ini":
                    continue
                count += 1
                if count > self.max_walk_files:
                    print(f"[CHITRA] Hit max_walk_files ({self.max_walk_files}) — index "
                          f"truncated. Raise chitra.max_walk_files in config if your Drive "
                          f"is larger.")
                    return index
                path = os.path.join(dirpath, fname)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                ext = os.path.splitext(fname)[1].lower()
                index.append({
                    "name": fname,
                    "path": path,
                    "rel_path": os.path.relpath(path, self._local_root),
                    "ext": ext,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "mtime_short": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d"),
                })
        return index

    def _reindex_drive(self) -> str:
        """Public hook: forces an index rebuild. Useful after adding many files."""
        if not self._local_root:
            return "No local Drive mount configured — nothing to reindex."
        try:
            if os.path.exists(self.index_path):
                os.remove(self.index_path)
        except OSError:
            pass
        idx = self._get_index()
        return f"Re-indexed {len(idx)} files in {self._local_root}."

    def _content_match_score(self, path: str, q_tokens: list[str]) -> float:
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".pdf":
                text = self._extract_pdf_from_disk(path)
            else:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(self.max_content_scan_bytes)
            text_lower = text.lower()
            return float(sum(1 for t in q_tokens if t in text_lower))
        except Exception:
            return 0.0

    def _local_extract(self, path: str) -> str:
        """Read + extract text from a local Drive file. Cached on disk by
        path+mtime so re-asks are free."""
        try:
            st = os.stat(path)
        except OSError:
            return ""
        cache_key = re.sub(r"[^a-zA-Z0-9]+", "_", path)[-120:]
        cache_path = os.path.join(self.cache_dir, f"local_{cache_key}_{int(st.st_mtime)}.txt")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = f.read()
                if cached:
                    return cached
            except OSError:
                pass
        ext = os.path.splitext(path)[1].lower()
        text = ""
        try:
            if ext == ".pdf":
                text = self._extract_pdf_from_disk(path)
            elif ext == ".docx":
                text = self._extract_docx_from_disk(path)
            elif ext in _GDRIVE_SHORTCUT_EXTS:
                text = self._extract_gdrive_shortcut(path)
            elif ext in _TEXTUAL_EXTS or ext == "":
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(self.max_file_bytes)
            elif ext in (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"):
                horus = self.marduk.get_module("HORUS") if self.marduk else None
                if horus:
                    result = horus.execute("ocr_image", {"path": path})
                    text = result if isinstance(result, str) else ""
            # Anything else (videos, audio, archives) is intentionally left empty.
        except Exception:
            text = ""
        text = (text or "").strip()
        if text:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass
        return text

    def _extract_pdf_from_disk(self, path: str) -> str:
        try:
            import pypdf
            with open(path, "rb") as f:
                reader = pypdf.PdfReader(f)
                parts = []
                for page in reader.pages[:50]:
                    try:
                        parts.append(page.extract_text() or "")
                    except Exception:
                        continue
                return "\n\n".join(p for p in parts if p.strip())
        except Exception:
            return ""

    def _extract_docx_from_disk(self, path: str) -> str:
        try:
            import docx
            doc = docx.Document(path)
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return ""

    def _extract_gdrive_shortcut(self, path: str) -> str:
        """A .gdoc/.gsheet/.gslides file is a JSON pointer. Without the API
        we can only surface the link; with the API, we'd export() the doc.
        Try API path on demand, otherwise return the URL so the user can
        click through."""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                meta = json.load(f)
            url = meta.get("url") or meta.get("resource_id") or ""
            doc_id = meta.get("doc_id") or meta.get("resource_id") or ""
            if self._lazy_api() and doc_id and not doc_id.startswith("http"):
                # Pull the live content via API.
                try:
                    f = self._service.files().get(
                        fileId=doc_id, fields="id, name, mimeType"
                    ).execute()
                    return self._api_fetch_and_extract(f["id"], f.get("mimeType", ""))
                except Exception:
                    pass
            return f"(Google Drive shortcut — open in browser: {url})" if url else ""
        except Exception:
            return ""

    # ── API path (optional fallback) ─────────────────────────────────
    def _lazy_api(self) -> bool:
        """Build the service on first need. Returns True if usable."""
        if self._service is not None:
            return True
        if not _HAS_GOOGLE or not os.path.exists(self.credentials_path):
            return False
        try:
            self._service = self._build_service(interactive=True)
            return self._service is not None
        except Exception as e:
            print(f"[CHITRA] API auth failed: {e}")
            return False

    def _build_service(self, interactive: bool):
        creds = None
        if os.path.exists(self.token_path):
            try:
                creds = Credentials.from_authorized_user_file(self.token_path, _SCOPES)
            except Exception as e:
                self._log.warning(f"loading token failed: {e}")
        if creds and not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    self._save_token(creds)
                except Exception as e:
                    self._log.warning(f"token refresh failed: {e}")
                    creds = None
        if not creds and interactive:
            flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, _SCOPES)
            creds = flow.run_local_server(port=0, open_browser=True,
                                          authorization_prompt_message="")
            self._save_token(creds)
        if not creds:
            return None
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _save_token(self, creds):
        try:
            with open(self.token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            self._log.warning(f"saving token failed: {e}")

    def _api_search(self, query: str, limit: int) -> str:
        q_parts = [f"(name contains '{_escape(query)}' or fullText contains '{_escape(query)}')",
                   "trashed = false"]
        if self.scope_folder_id:
            q_parts.insert(0, f"'{self.scope_folder_id}' in parents")
        resp = self._service.files().list(
            q=" and ".join(q_parts), pageSize=limit,
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        ).execute()
        files = resp.get("files", [])
        if not files:
            return f"No Drive files matched '{query}'."
        lines = []
        for f in files:
            kind = _short_mime(f.get("mimeType", ""))
            modified = (f.get("modifiedTime") or "")[:10]
            lines.append(f"• {f['name']} [{kind}] (api, modified={modified})")
        return "\n".join(lines)

    def _api_resolve_id(self, name_or_id: str) -> tuple[str, str, str]:
        if re.fullmatch(r"[A-Za-z0-9_\-]{20,60}", name_or_id):
            try:
                f = self._service.files().get(
                    fileId=name_or_id, fields="id, name, mimeType"
                ).execute()
                return f["id"], f["name"], f.get("mimeType", "")
            except HttpError:
                pass
        q_parts = [f"name contains '{_escape(name_or_id)}'", "trashed = false"]
        if self.scope_folder_id:
            q_parts.insert(0, f"'{self.scope_folder_id}' in parents")
        resp = self._service.files().list(
            q=" and ".join(q_parts), pageSize=1,
            fields="files(id, name, mimeType)",
            orderBy="modifiedTime desc",
        ).execute()
        files = resp.get("files", [])
        if files:
            f = files[0]
            return f["id"], f["name"], f.get("mimeType", "")
        return "", "", ""

    def _api_search_for_keywords(self, terms: list[str], limit: int = 5) -> list[dict]:
        if not terms:
            return []
        or_clauses = " or ".join(
            f"fullText contains '{_escape(t)}' or name contains '{_escape(t)}'"
            for t in terms[:5]
        )
        q_parts = [f"({or_clauses})", "trashed = false"]
        if self.scope_folder_id:
            q_parts.insert(0, f"'{self.scope_folder_id}' in parents")
        resp = self._service.files().list(
            q=" and ".join(q_parts), pageSize=limit,
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        ).execute()
        return resp.get("files", []) or []

    def _api_recent(self, days: int, limit: int) -> str:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat("T") + "Z"
        q_parts = [f"modifiedTime > '{cutoff}'", "trashed = false"]
        if self.scope_folder_id:
            q_parts.insert(0, f"'{self.scope_folder_id}' in parents")
        resp = self._service.files().list(
            q=" and ".join(q_parts), pageSize=limit,
            fields="files(id, name, mimeType, modifiedTime)",
            orderBy="modifiedTime desc",
        ).execute()
        files = resp.get("files", [])
        if not files:
            return f"No Drive files modified in the last {days} day(s)."
        return "\n".join(
            f"• {f['name']} [{_short_mime(f.get('mimeType',''))}]  ({(f.get('modifiedTime') or '')[:10]})"
            for f in files
        )

    def _api_fetch_and_extract(self, file_id: str, mime_type: str) -> str:
        cache_path = os.path.join(self.cache_dir, f"api_{file_id}.txt")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = f.read()
                if cached:
                    return cached
            except OSError:
                pass
        try:
            if mime_type in _EXPORT_FORMATS:
                data = self._service.files().export(
                    fileId=file_id, mimeType=_EXPORT_FORMATS[mime_type]
                ).execute()
                text = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else str(data)
            else:
                req = self._service.files().get_media(fileId=file_id)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, req)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                    if buf.tell() > self.max_file_bytes:
                        return f"(file > {self.max_file_bytes // (1024*1024)} MB — skipped)"
                raw = buf.getvalue()
                text = self._extract_bytes_api(raw, mime_type)
        except HttpError as e:
            return f"(fetch failed: {e})"
        text = (text or "").strip()
        if text:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                pass
        return text

    def _extract_bytes_api(self, raw: bytes, mime_type: str) -> str:
        if not raw:
            return ""
        if mime_type.startswith("text/") or mime_type in ("application/json", "application/xml"):
            return raw.decode("utf-8", errors="ignore")
        if mime_type == "application/pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(io.BytesIO(raw))
                parts = [page.extract_text() or "" for page in reader.pages[:50]]
                return "\n\n".join(p for p in parts if p.strip())
            except Exception:
                return ""
        if mime_type.startswith("image/") and self.marduk:
            horus = self.marduk.get_module("HORUS")
            if horus:
                ext = mime_type.split("/")[-1].split("+")[0] or "png"
                tmp = os.path.join(self.cache_dir, f"_ocr_tmp.{ext}")
                try:
                    with open(tmp, "wb") as f:
                        f.write(raw)
                    result = horus.execute("ocr_image", {"path": tmp})
                    return result if isinstance(result, str) else ""
                finally:
                    try: os.remove(tmp)
                    except OSError: pass
        if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            try:
                import docx
                d = docx.Document(io.BytesIO(raw))
                return "\n\n".join(p.text for p in d.paragraphs if p.text.strip())
            except Exception:
                return ""
        try:
            decoded = raw.decode("utf-8", errors="ignore")
            printable = sum(1 for c in decoded if c.isprintable() or c in "\n\r\t")
            if decoded and printable / max(1, len(decoded)) > 0.7:
                return decoded
        except Exception:
            pass
        return ""

    # ── Output helpers ───────────────────────────────────────────────
    def _format_hits(self, hits: list[dict]) -> str:
        lines = []
        for h in hits:
            kind = h.get("ext", "") or "file"
            lines.append(f"• {h['name']} [{kind.lstrip('.')}]  "
                         f"({h.get('mtime_short','')})  {h.get('rel_path','')}")
        return "\n".join(lines)

    def _trim(self, text: str, max_chars: int, name: str) -> str:
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n... (truncated, full length {len(text)} chars)"
        return f"# {name}\n\n{text}"


# ── Module-level helpers ─────────────────────────────────────────────
def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _short_mime(mime: str) -> str:
    if not mime:
        return "file"
    if mime.startswith("application/vnd.google-apps."):
        return mime.split(".")[-1]
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return mime.split("/")[-1]
    if mime.startswith("text/"):
        return "text"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    return mime.split("/")[-1] or "file"


_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "i", "me", "my", "you", "your", "what", "which",
    "who", "whom", "whose", "where", "when", "why", "how", "this", "that",
    "these", "those", "from", "with", "about", "by", "as", "show", "tell",
    "find", "search", "look",
})


def _extract_keywords(question: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", question.lower())
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= 5:
            break
    return out
