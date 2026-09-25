# JANUS — Roman — two-faced god of doorways, gateways, transitions
# File system: read, write, navigate, search, manage files on disk

import os
import shutil
import glob
import subprocess
from datetime import datetime
from core.marduk import OdinModule

try:
    from send2trash import send2trash
    _HAS_SEND2TRASH = True
except ImportError:
    _HAS_SEND2TRASH = False


# === Spoken-form aliases for find_files_smart =========================
# Common words users say for standard Windows folders and file types.
# Lets the fast-route skip an LLM round-trip entirely for queries like
# "find PDFs in desktop bigger than 5 MB".
_LOCATION_ALIASES = {
    "desktop":          "~/Desktop",
    "downloads":        "~/Downloads",
    "downloads folder": "~/Downloads",
    "documents":        "~/Documents",
    "docs":             "~/Documents",
    "pictures":         "~/Pictures",
    "photos":           "~/Pictures",
    "videos":           "~/Videos",
    "music":            "~/Music",
    "onedrive":         "~/OneDrive",
    "home":             "~",
    "home folder":      "~",
    "user folder":      "~",
    "my computer":      "C:/",
    "this pc":          "C:/",
    "brain":            "~/Brain",
    "vault":            "~/Brain",
    "obsidian":         "~/Brain",
    "odin":             "c:/odin",
    "odin folder":      "c:/odin",
}

# Spoken file-category → extensions (lowercase, no dot prefix matched separately).
# Empty string [""] means "any file" — special-cased in the walker.
_FILE_TYPE_TO_EXTS = {
    "files":            [""],
    "file":             [""],
    "all files":        [""],
    "everything":       [""],
    "pdf":              [".pdf"],
    "pdfs":             [".pdf"],
    "pdf files":        [".pdf"],
    "pdf documents":    [".pdf"],
    "image":            [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "images":           [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "image files":      [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"],
    "photo":            [".jpg", ".jpeg", ".png", ".heic"],
    "photos":           [".jpg", ".jpeg", ".png", ".heic"],
    "video":            [".mp4", ".avi", ".mkv", ".mov", ".webm"],
    "videos":           [".mp4", ".avi", ".mkv", ".mov", ".webm"],
    "video files":      [".mp4", ".avi", ".mkv", ".mov", ".webm"],
    "audio":            [".mp3", ".wav", ".flac", ".m4a", ".aac"],
    "music files":      [".mp3", ".wav", ".flac", ".m4a"],
    "mp3":              [".mp3"],
    "mp3s":             [".mp3"],
    "python":           [".py"],
    "python files":     [".py"],
    "py":               [".py"],
    "javascript":       [".js", ".jsx", ".ts", ".tsx"],
    "javascript files": [".js"],
    "js":               [".js"],
    "html":             [".html", ".htm"],
    "html files":       [".html", ".htm"],
    "css":              [".css"],
    "css files":        [".css"],
    "json":             [".json"],
    "json files":       [".json"],
    "text":             [".txt"],
    "text files":       [".txt"],
    "txt":              [".txt"],
    "markdown":         [".md", ".markdown"],
    "markdown files":   [".md"],
    "md":               [".md"],
    "word":             [".doc", ".docx"],
    "word files":       [".doc", ".docx"],
    "word documents":   [".doc", ".docx"],
    "docs":             [".doc", ".docx"],
    "excel":            [".xls", ".xlsx"],
    "excel files":      [".xls", ".xlsx"],
    "spreadsheets":     [".xls", ".xlsx", ".csv"],
    "powerpoint":       [".ppt", ".pptx"],
    "presentations":    [".ppt", ".pptx"],
    "zip":              [".zip", ".rar", ".7z", ".tar", ".gz"],
    "zip files":        [".zip"],
    "archives":         [".zip", ".rar", ".7z", ".tar", ".gz"],
    "exe":              [".exe"],
    "executables":      [".exe", ".msi"],
}


def _resolve_location(name: str) -> str:
    """Convert a spoken location ('desktop', 'downloads folder') to a real
    filesystem path. Returns the input unchanged if not recognised — the
    caller can still try to use it as a literal path."""
    if not name:
        return ""
    key = name.lower().strip().rstrip(" .!?,;:")
    return _LOCATION_ALIASES.get(key, name)


def _resolve_file_type(name: str) -> list[str]:
    """Spoken file type to list of extension suffixes ('.pdf', '.docx', ...).
    A single [''] return means 'match anything'."""
    key = name.lower().strip().rstrip(" .!?,;:")
    if key in _FILE_TYPE_TO_EXTS:
        return _FILE_TYPE_TO_EXTS[key]
    # Fallback: treat the word as a literal extension. "log" → ".log"
    return ["." + key.rstrip("s")]  # strip plural 's'


class Janus(OdinModule):
    MODULE_NAME = "JANUS"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "list_directory",
                "description": "List files and folders inside a directory",
                "parameters": {
                    "path": {"type": "string", "description": "Directory path (absolute, ~, or relative)"}
                },
                "required": ["path"]
            },
            {
                "name": "read_file",
                "description": "Read the contents of a text file",
                "parameters": {
                    "path": {"type": "string", "description": "File path"},
                    "max_chars": {"type": "integer", "description": "Maximum characters to read (default 8000)"}
                },
                "required": ["path"]
            },
            {
                "name": "write_file",
                "description": "Write text content to a file (creates or overwrites)",
                "parameters": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Text to write"}
                },
                "required": ["path", "content"]
            },
            {
                "name": "write_and_open",
                "description": "Write text to a file AND open it in the default app. Use this whenever the user asks you to compose text in an editor (e.g. 'write notes about X to notepad' or 'open word and draft an email'). Choose the extension based on the editor: .txt opens in Notepad, .docx in Word, .md in the markdown editor, .py in the code editor. If no folder given, the file lands on the Desktop. This is preferred over chaining open_application + type_text, which has a focus race.",
                "parameters": {
                    "path": {"type": "string", "description": "File path or just a filename (defaults to Desktop)"},
                    "content": {"type": "string", "description": "Text to write into the file"}
                },
                "required": ["path", "content"]
            },
            {
                "name": "append_file",
                "description": "Append text to an existing file (creates if missing)",
                "parameters": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Text to append"}
                },
                "required": ["path", "content"],
                "internal_only": True
            },
            {
                "name": "delete_path",
                "description": "Send a file or folder to the Recycle Bin (recoverable)",
                "parameters": {
                    "path": {"type": "string", "description": "Path to delete"}
                },
                "required": ["path"]
            },
            {
                "name": "create_directory",
                "description": "Create a directory (and parents if needed)",
                "parameters": {
                    "path": {"type": "string", "description": "Directory path"}
                },
                "required": ["path"]
            },
            {
                "name": "find_files",
                "description": "Find files in a directory matching a glob pattern (e.g. *.pdf, **/*.py)",
                "parameters": {
                    "directory": {"type": "string", "description": "Directory to search"},
                    "pattern": {"type": "string", "description": "Glob pattern"}
                },
                "required": ["directory", "pattern"]
            },
            {
                "name": "search_in_files",
                "description": "Search for a text query inside files within a directory",
                "parameters": {
                    "directory": {"type": "string", "description": "Directory to search"},
                    "query": {"type": "string", "description": "Text to search for"},
                    "pattern": {"type": "string", "description": "File glob (default *.txt)"}
                },
                "required": ["directory", "query"]
            },
            {
                "name": "get_path_info",
                "description": "Get info about a file or folder: size, modified date, type",
                "parameters": {
                    "path": {"type": "string", "description": "Path to inspect"}
                },
                "required": ["path"],
                "internal_only": True
            },
            {
                "name": "copy_path",
                "description": "Copy a file or folder to a new location",
                "parameters": {
                    "source": {"type": "string", "description": "Source path"},
                    "destination": {"type": "string", "description": "Destination path"}
                },
                "required": ["source", "destination"],
                "internal_only": True
            },
            {
                "name": "move_path",
                "description": "Move or rename a file or folder",
                "parameters": {
                    "source": {"type": "string", "description": "Source path"},
                    "destination": {"type": "string", "description": "New path"}
                },
                "required": ["source", "destination"],
                "internal_only": True
            },
            {
                "name": "open_in_explorer",
                "description": "Open a folder or file location in Windows File Explorer",
                "parameters": {
                    "path": {"type": "string", "description": "Path to open"}
                },
                "required": ["path"],
                "internal_only": True
            },
            {
                "name": "find_and_open",
                "description": "Search the user profile for a folder or file by name and open it in Explorer.",
                "parameters": {
                    "name": {"type": "string", "description": "Folder or file name to search for"}
                },
                "required": ["name"],
                "internal_only": True
            },
            {
                "name": "locate_file",
                "description": (
                    "Find a file or folder by (fuzzy) name WITHOUT opening it — "
                    "returns the path and remembers it as ODIN's current focus, so "
                    "a follow-up like 'summarize it' or chained commands ('find the "
                    "latest invoice and summarize it') act on what was found. "
                    "'newest' picks the most recently modified match."
                ),
                "parameters": {
                    "name":     {"type": "string", "description": "File/folder name to find (fuzzy ok)"},
                    "location": {"type": "string", "description": "Optional scope: desktop, downloads, documents, brain..."},
                    "newest":   {"type": "boolean", "description": "Pick the most recently modified match"},
                },
                "required": ["name"],
            },
            {
                "name": "find_files_smart",
                "description": "ONE-CALL file search for natural queries like 'find PDFs in desktop bigger than 5 MB'. Resolves spoken location names (desktop, downloads, documents, brain) and file-type categories (PDFs, images, python files, etc.) automatically. PREFER this over find_files for any natural-language file-search query — it's instant and bypasses the LLM.",
                "parameters": {
                    "file_type": {"type": "string", "description": "What kind of files (e.g. 'PDFs', 'images', 'python files', 'files')"},
                    "location":  {"type": "string", "description": "Where to search (e.g. 'desktop', 'downloads', 'documents', 'brain')"},
                    "min_size_mb": {"type": "number", "description": "Optional minimum file size in MB"},
                    "max_size_mb": {"type": "number", "description": "Optional maximum file size in MB"}
                },
                "required": ["file_type", "location"],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "list_directory": self._list_dir,
            "read_file": self._read_file,
            "write_file": self._write_file,
            "write_and_open": self._write_and_open,
            "append_file": self._append_file,
            "delete_path": self._delete,
            "create_directory": self._mkdir,
            "find_files": self._find,
            "search_in_files": self._search,
            "get_path_info": self._info,
            "copy_path": self._copy,
            "move_path": self._move,
            "open_in_explorer": self._open,
            "find_and_open": self._find_and_open,
            "find_files_smart": self._find_files_smart,
            "locate_file": self._locate_file,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[JANUS] Error: {e}"
        return f"[JANUS] Unknown skill: {skill_name}"

    def _resolve(self, path: str) -> str:
        return os.path.abspath(os.path.expanduser(path))

    def _list_dir(self, path: str = ".") -> str:
        full = self._resolve(path)
        if not os.path.isdir(full):
            return f"Not a directory: {full}"
        entries = sorted(os.listdir(full))
        if not entries:
            return f"{full} is empty."
        formatted = []
        for name in entries[:50]:
            sub = os.path.join(full, name)
            tag = "DIR " if os.path.isdir(sub) else "FILE"
            formatted.append(f"[{tag}] {name}")
        suffix = f" (+{len(entries)-50} more)" if len(entries) > 50 else ""
        return f"{full} ({len(entries)} items): " + "; ".join(formatted) + suffix

    def _read_file(self, path: str = "", max_chars: int = 8000) -> str:
        full = self._resolve(path)
        if not os.path.isfile(full):
            return f"File not found: {full}"
        size = os.path.getsize(full)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars + 1)
        except Exception as e:
            return f"Could not read {full}: {e}"
        truncated = len(content) > max_chars
        content = content[:max_chars]
        suffix = f" [truncated, file is {size} bytes]" if truncated else ""
        return f"{full}:\n{content}{suffix}"

    def _write_file(self, path: str = "", content: str = "") -> str:
        full = self._resolve(path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} characters to {full}."

    def _write_and_open(self, path: str = "", content: str = "") -> str:
        # If the LLM gives a bare filename (no directory), drop it on the Desktop
        # — that's where users expect quickly-composed notes to land.
        path = (path or "").strip()
        if not path:
            return "Need a path or filename."
        if not os.path.dirname(path) and not path.startswith(("/", "\\", "~")):
            path = os.path.join(os.path.expanduser("~"), "Desktop", path)
        full = self._resolve(path)
        # Ensure an extension — default .txt so Windows knows what app to open.
        if not os.path.splitext(full)[1]:
            full = full + ".txt"
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            os.startfile(full)
        except OSError as e:
            return f"Wrote {full} but could not open it: {e}"
        return f"Wrote and opened {full}."

    def _append_file(self, path: str = "", content: str = "") -> str:
        full = self._resolve(path)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "a", encoding="utf-8") as f:
            f.write(content)
        return f"Appended {len(content)} characters to {full}."

    def _delete(self, path: str = "") -> str:
        full = self._resolve(path)
        if not os.path.exists(full):
            return f"Path not found: {full}"
        if _HAS_SEND2TRASH:
            send2trash(full)
            return f"Sent to Recycle Bin: {full}"
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        return f"Deleted (permanent — install send2trash for safer delete): {full}"

    def _mkdir(self, path: str = "") -> str:
        full = self._resolve(path)
        os.makedirs(full, exist_ok=True)
        return f"Created directory: {full}"

    def _find(self, directory: str = ".", pattern: str = "*") -> str:
        full = self._resolve(directory)
        if not os.path.isdir(full):
            return f"Not a directory: {full}"
        matches = glob.glob(os.path.join(full, pattern), recursive=True)
        if not matches:
            return f"No matches for '{pattern}' in {full}."
        names = [os.path.relpath(m, full) for m in matches[:50]]
        suffix = f" (+{len(matches)-50} more)" if len(matches) > 50 else ""
        return f"Found {len(matches)} match(es): " + "; ".join(names) + suffix

    def _search(self, directory: str = ".", query: str = "", pattern: str = "*.txt") -> str:
        full = self._resolve(directory)
        if not os.path.isdir(full):
            return f"Not a directory: {full}"
        results = []
        files = glob.glob(os.path.join(full, "**", pattern), recursive=True)
        q = query.lower()
        for path in files[:200]:
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if q in line.lower():
                            results.append(f"{os.path.relpath(path, full)}:{i}: {line.strip()[:120]}")
                            if len(results) >= 20:
                                break
            except Exception:
                continue
            if len(results) >= 20:
                break
        if not results:
            return f"No matches for '{query}' in {full}."
        return f"Search results ({len(results)} hit(s)): " + " | ".join(results)

    def _info(self, path: str = "") -> str:
        full = self._resolve(path)
        if not os.path.exists(full):
            return f"Path not found: {full}"
        st = os.stat(full)
        kind = "directory" if os.path.isdir(full) else "file"
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        return f"{full}: {kind}, {size} bytes, modified {mtime}."

    def _copy(self, source: str = "", destination: str = "") -> str:
        src = self._resolve(source)
        dst = self._resolve(destination)
        if not os.path.exists(src):
            return f"Source not found: {src}"
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.copy2(src, dst)
        return f"Copied {src} to {dst}."

    def _move(self, source: str = "", destination: str = "") -> str:
        src = self._resolve(source)
        dst = self._resolve(destination)
        if not os.path.exists(src):
            return f"Source not found: {src}"
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(src, dst)
        return f"Moved {src} to {dst}."

    def _open(self, path: str = "") -> str:
        full = self._resolve(path)
        if os.path.isdir(full):
            subprocess.Popen(["explorer", full])
        elif os.path.isfile(full):
            subprocess.Popen(["explorer", "/select,", full])
        else:
            return f"Path not found: {full}"
        return f"Opened in Explorer: {full}"

    # Common user-data roots searched by find_and_open. Skips Windows/Program
    # Files / AppData since those are full of system noise the user almost
    # never means by "open my X folder".
    _USER_ROOTS = [
        os.path.expanduser("~"),
        os.path.expanduser("~/Desktop"),
        os.path.expanduser("~/Documents"),
        os.path.expanduser("~/Downloads"),
        os.path.expanduser("~/Videos"),
        os.path.expanduser("~/Pictures"),
        os.path.expanduser("~/Music"),
        os.path.expanduser("~/OneDrive"),
        os.path.expanduser("~/OneDrive/Desktop"),
        os.path.expanduser("~/OneDrive/Documents"),
    ]

    # Map of spoken location hints → the user-root subset to search. When the
    # user says "on desktop" / "in downloads", we restrict the scan to those
    # roots instead of walking everything — both faster and more accurate.
    _LOCATION_HINT_ROOTS = {
        "desktop":   ("~/Desktop", "~/OneDrive/Desktop"),
        "downloads": ("~/Downloads",),
        "documents": ("~/Documents", "~/OneDrive/Documents"),
        "pictures":  ("~/Pictures", "~/OneDrive/Pictures"),
        "videos":    ("~/Videos",),
        "music":     ("~/Music",),
        "onedrive":  ("~/OneDrive",),
        "brain":     ("~/Brain",),
        "home":      ("~",),
        # broad hints — still useful to constrain "anywhere on this machine"
        "drive":     None,    # None → all _USER_ROOTS
        "system":    None, "computer": None, "pc": None, "laptop": None, "machine": None,
    }

    def _find_and_open(self, name: str = "", location: str = "") -> str:
        target = (name or "").strip().strip(".!?,;:\"' ")
        if not target:
            return "Need a name to search for."
        target_lower = target.lower()

        # Resolve which roots to search. With a "location" hint, scope down to
        # those folders; without, fall through to all _USER_ROOTS. The hint
        # arrives lowercased from the regex; pass-through if unknown.
        hint = (location or "").strip().lower()
        if hint and hint in self._LOCATION_HINT_ROOTS and self._LOCATION_HINT_ROOTS[hint]:
            roots = [os.path.expanduser(r) for r in self._LOCATION_HINT_ROOTS[hint]]
        else:
            roots = list(self._USER_ROOTS)

        candidates = []
        # Phase 1: exact case-insensitive entry name in the scoped roots.
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.listdir(root):
                    if entry.lower() == target_lower:
                        candidates.append(os.path.join(root, entry))
                        break
            except OSError:
                continue
            if candidates:
                break

        # Phase 2: fuzzy substring + similarity-ranked fallback in the same roots.
        if not candidates:
            best = self._fuzzy_pick(roots, target_lower, max_depth=3)
            if best:
                candidates.append(best)

        if not candidates:
            scope = f"in {hint}" if hint and self._LOCATION_HINT_ROOTS.get(hint) else "under your profile"
            return f"Scanned {scope} but couldn't find '{target}'. Try a different name or 'find files for {target}'."

        path = candidates[0]
        # Where exactly did we open from — useful feedback so the user can
        # confirm we hit the right copy when Desktop vs OneDrive Desktop both exist.
        normalized = os.path.normpath(path)
        if os.path.isdir(path):
            subprocess.Popen(["explorer", normalized])
            return f"Opened folder: {normalized}"
        subprocess.Popen(["explorer", "/select,", normalized])
        return f"Opened file location: {normalized}"

    def _locate_file(self, name: str = "", location: str = "", newest: bool = False) -> str:
        """find_and_open's search phases, minus the open. Collects ALL
        candidates so 'newest' can rank by mtime; records the winner into
        ODIN's working memory (SESHAT.note_context) so follow-ups and chained
        commands ('...and summarize it') act on what was just found."""
        target = (name or "").strip().strip(".!?,;:\"' ")
        if not target:
            return "Need a name to search for."
        target_lower = target.lower()
        hint = (location or "").strip().lower()
        if hint and hint in self._LOCATION_HINT_ROOTS and self._LOCATION_HINT_ROOTS[hint]:
            roots = [os.path.expanduser(r) for r in self._LOCATION_HINT_ROOTS[hint]]
        else:
            roots = list(self._USER_ROOTS)

        candidates: list[str] = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            try:
                for entry in os.listdir(root):
                    if entry.lower() == target_lower or target_lower in entry.lower():
                        candidates.append(os.path.join(root, entry))
            except OSError:
                continue
        if not candidates:
            candidates = self._fuzzy_pick_all(roots, target_lower, max_depth=3)

        if not candidates:
            scope = f"in {hint}" if hint and self._LOCATION_HINT_ROOTS.get(hint) else "under your profile"
            return f"Scanned {scope} but couldn't find '{target}'."

        # FILES beat folders: "find the latest invoice and summarize it"
        # must not land on a folder that happens to match (field-tested:
        # 'WPS Cloud Files' won over actual documents). Stable sort keeps
        # exact-match ordering within each group.
        candidates.sort(key=lambda p: 0 if os.path.isfile(p) else 1)
        if newest:
            try:
                candidates.sort(key=lambda p: ((0 if os.path.isfile(p) else 1),
                                               -os.path.getmtime(p)))
            except OSError:
                pass
        path = os.path.normpath(candidates[0])

        # Working memory: this path is now ODIN's focus.
        try:
            self.send("SESHAT", "note_context",
                      title=f"Found {os.path.basename(path)}",
                      content=path, kind="file-found")
        except Exception:
            pass

        extra = f" (newest of {len(candidates)} matches)" if newest and len(candidates) > 1 else ""
        return f"Found: {path}{extra}"

    def _fuzzy_pick_all(self, roots, target_lower: str, max_depth: int = 3) -> list:
        """Like _fuzzy_pick but returns ALL candidates scoring >= 0.55,
        best-first, so callers can re-rank (e.g. by mtime for 'newest')."""
        from difflib import SequenceMatcher
        scored: list[tuple[float, str]] = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            root_norm = os.path.normpath(root)
            root_depth = root_norm.count(os.sep)
            for dirpath, dirnames, filenames in os.walk(root_norm):
                depth = dirpath.count(os.sep) - root_depth
                if depth > max_depth:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                               ("node_modules", "__pycache__", "venv", ".venv", "env", "appdata")]
                for nm in dirnames + filenames:
                    nlow = nm.lower()
                    if target_lower in nlow or nlow in target_lower:
                        score = SequenceMatcher(None, nlow, target_lower).ratio() + 0.1
                    else:
                        score = SequenceMatcher(None, nlow, target_lower).ratio()
                    if score >= 0.55:
                        scored.append((score, os.path.join(dirpath, nm)))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [p for _, p in scored[:10]]

    @staticmethod
    def _fuzzy_pick(roots, target_lower: str, max_depth: int = 3):
        """Walk the given roots and return the best-matching entry by
        SequenceMatcher score (folder/file name vs target). Higher than the
        old 'first substring hit' wins for cases like 'Project Files' where
        multiple folders contain 'project'."""
        from difflib import SequenceMatcher
        best_path = None
        best_score = 0.0
        for root in roots:
            if not os.path.isdir(root):
                continue
            root_norm = os.path.normpath(root)
            root_depth = root_norm.count(os.sep)
            for dirpath, dirnames, filenames in os.walk(root_norm):
                depth = dirpath.count(os.sep) - root_depth
                if depth > max_depth:
                    dirnames[:] = []
                    continue
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                               ("node_modules", "__pycache__", "venv", ".venv", "env", "appdata")]
                for name in dirnames + filenames:
                    nlow = name.lower()
                    if target_lower in nlow or nlow in target_lower:
                        score = SequenceMatcher(None, nlow, target_lower).ratio() + 0.1
                    else:
                        score = SequenceMatcher(None, nlow, target_lower).ratio()
                    if score > best_score and score >= 0.55:
                        best_score = score
                        best_path = os.path.join(dirpath, name)
        return best_path

    @staticmethod
    def _bounded_find(root: str, target_lower: str, max_depth: int = 3) -> str | None:
        root = os.path.normpath(root)
        root_depth = root.count(os.sep)
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath.count(os.sep) - root_depth
            if depth > max_depth:
                dirnames[:] = []
                continue
            # Skip hidden / system / virtualenv noise.
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d.lower() not in
                           ("node_modules", "__pycache__", "venv", ".venv", "env", "appdata")]
            for d in dirnames:
                if d.lower() == target_lower or target_lower in d.lower():
                    return os.path.join(dirpath, d)
            for f in filenames:
                if f.lower() == target_lower or target_lower in f.lower():
                    return os.path.join(dirpath, f)
        return None

    def _find_files_smart(self, file_type: str = "", location: str = "",
                          min_size_mb: float = 0, max_size_mb: float = 0) -> str:
        """Local file search with spoken-form arguments. Resolves location
        aliases, file-type aliases, and applies size filter. No LLM needed.
        Walks recursively, skips system / cache dirs, caps depth at 6 so a
        single tree of node_modules doesn't take 10 seconds."""
        loc_resolved = _resolve_location(location or "")
        if not loc_resolved:
            return "Need a location (e.g. 'desktop', 'downloads')."
        path = os.path.expanduser(loc_resolved)
        path = self._resolve(path)
        if not os.path.isdir(path):
            return f"Not a directory: {location!r} (resolved to {path})."
        exts = _resolve_file_type(file_type or "files")
        match_any = exts == [""]
        try:
            min_b = float(min_size_mb) * 1024 * 1024 if min_size_mb else 0
            max_b = float(max_size_mb) * 1024 * 1024 if max_size_mb else 0
        except (TypeError, ValueError):
            min_b = max_b = 0
        SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "env",
                     "appdata", ".git", "$recycle.bin", "system volume information"}
        matches = []  # list of (path, size_bytes)
        root_depth = os.path.normpath(path).count(os.sep)
        for dirpath, dirnames, filenames in os.walk(path):
            # Depth + noise filtering
            if (dirpath.count(os.sep) - root_depth) > 6:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d.lower() not in SKIP_DIRS]
            for f in filenames:
                f_low = f.lower()
                if not match_any and not any(f_low.endswith(ext) for ext in exts):
                    continue
                fp = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                if min_b and sz < min_b:
                    continue
                if max_b and sz > max_b:
                    continue
                matches.append((fp, sz))
        if not matches:
            constraint = ""
            if min_size_mb: constraint += f" bigger than {min_size_mb} MB"
            if max_size_mb: constraint += f" smaller than {max_size_mb} MB"
            return f"No {file_type or 'files'} in {location}{constraint}."
        # Largest first; voice-friendly cap at 10 results.
        matches.sort(key=lambda x: -x[1])
        top = matches[:10]
        lines = []
        for fp, sz in top:
            rel = os.path.relpath(fp, path)
            mb = sz / (1024 * 1024)
            size_label = f"{mb:.1f} MB" if mb >= 0.1 else f"{sz // 1024} KB"
            lines.append(f"{rel} ({size_label})")
        summary = f"Found {len(matches)} {file_type or 'file'}(s) in {location}"
        if min_size_mb: summary += f" bigger than {min_size_mb} MB"
        if max_size_mb: summary += f" smaller than {max_size_mb} MB"
        suffix = f" (+{len(matches)-10} more)" if len(matches) > 10 else ""
        # Working memory: remember what was found so "summarize it" /
        # chained follow-ups act on these results.
        try:
            self.send("SESHAT", "note_context",
                      title=f"Found {file_type or 'files'} in {location}",
                      content="\n".join(fp for fp, _ in top), kind="files-found")
        except Exception:
            pass
        return summary + ": " + "; ".join(lines) + suffix
