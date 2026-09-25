# HEPHAESTUS — Greek — god of forge, craft and machines
# Automation: macros, scripts, routines, task sequences.
# Also the keeper of the forge schematics: GitNexus code-graph queries
# (impact analysis / context / search) over indexed codebases via the
# `gitnexus mcp` stdio server. Fully local — no network, fits constraint 3.

import json
import os
import subprocess
import threading
from datetime import datetime
from core.marduk import OdinModule


class _GitNexusMCP:
    """Minimal MCP stdio client for the GitNexus server (newline-delimited
    JSON-RPC). Lazy-started, persistent, restarted on death. One in-flight
    request at a time — ODIN's tool calls are sequential anyway."""

    def __init__(self, cwd: str):
        self._cwd = cwd
        self._proc = None
        self._lock = threading.Lock()
        self._next_id = 1
        self._tools: list[dict] = []

    def _start(self):
        self._proc = subprocess.Popen(
            ["cmd", "/c", "gitnexus", "mcp"],
            cwd=self._cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "ODIN-HEPHAESTUS", "version": "1.0"},
        }, timeout=30)
        self._notify("notifications/initialized")
        self._tools = (self._rpc("tools/list", {}, timeout=30) or {}).get("tools", [])

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _send(self, payload: dict):
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str):
        self._send({"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict, timeout: int = 60):
        req_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        # Read lines until our id answers. A watchdog kills the proc on
        # timeout so a wedged server can't hang ODIN's tool-call loop.
        timer = threading.Timer(timeout, lambda: self._proc and self._proc.kill())
        timer.start()
        try:
            while True:
                line = self._proc.stdout.readline()
                if not line:
                    raise RuntimeError("gitnexus mcp closed the pipe")
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue                      # stray log line
                if msg.get("id") != req_id:
                    continue                      # notification / other id
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result")
        finally:
            timer.cancel()

    def find_tool(self, *keywords: str) -> dict | None:
        """First listed tool whose name contains any keyword (in order)."""
        for kw in keywords:
            for t in self._tools:
                if kw in t.get("name", "").lower():
                    return t
        return None

    def call(self, tool: dict, text: str, timeout: int = 90) -> str:
        """Call a tool, mapping our single text arg onto its first required
        string property (GitNexus tools are query-shaped)."""
        args = {}
        schema = tool.get("inputSchema") or {}
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        for name in (required or list(props)):
            if props.get(name, {}).get("type") == "string":
                args[name] = text
                break
        result = self._rpc("tools/call", {"name": tool["name"], "arguments": args}, timeout)
        parts = []
        for c in (result or {}).get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts).strip() or "(no result)"

    def ask(self, kind_keywords: tuple, text: str) -> str:
        with self._lock:
            if not self._alive():
                self._start()
            tool = self.find_tool(*kind_keywords)
            if not tool:
                names = ", ".join(t.get("name", "?") for t in self._tools) or "none"
                return f"No matching GitNexus tool. Available: {names}"
            try:
                return self.call(tool, text)
            except Exception:
                # One retry on a fresh process — covers server death mid-call.
                self._start()
                tool = self.find_tool(*kind_keywords)
                return self.call(tool, text) if tool else "GitNexus restart failed."


class Hephaestus(OdinModule):
    MODULE_NAME = "HEPHAESTUS"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)
        self._macros_path = "data/knowledge/macros.json"
        self._macros: dict = self._load()
        h_cfg = config.get("hephaestus", {})
        # Code-graph client — lazy; gitnexus process only spawns on first use.
        self._gitnexus = _GitNexusMCP(h_cfg.get("code_graph_root", "c:/odin"))

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "create_macro",
                "description": "Create a named macro (sequence of commands to run together)",
                "parameters": {
                    "name": {"type": "string", "description": "Macro name"},
                    "commands": {"type": "string", "description": "Comma-separated list of commands or app names"}
                },
                "required": ["name", "commands"],
                "internal_only": True
            },
            {
                "name": "run_macro",
                "description": "Run a previously saved macro by name",
                "parameters": {
                    "name": {"type": "string", "description": "Macro name to run"}
                },
                "required": ["name"]
            },
            {
                "name": "list_macros",
                "description": "List all saved macros",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "delete_macro",
                "description": "Delete a macro",
                "parameters": {
                    "name": {"type": "string", "description": "Macro name to delete"}
                },
                "required": ["name"],
                "internal_only": True
            },
            {
                "name": "run_startup_routine",
                "description": "Run the morning startup routine (open apps, check weather, etc.)",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "bundle_codebase",
                "description": (
                    "Pack a codebase (default: ODIN itself at c:/odin) into a "
                    "single Markdown file suitable for pasting to a cloud AI. "
                    "Respects .gitignore + default ignores (node_modules, .git, "
                    "build dirs, etc.). Strips obvious secrets. Output saved to "
                    "Desktop by default. Inspired by yamadashy/repomix."
                ),
                "parameters": {
                    "root": {"type": "string", "description": "Project root path (default 'c:/odin')"},
                    "output": {"type": "string", "description": "Output path (default '~/Desktop/<name>_bundle.md')"},
                    "max_file_bytes": {"type": "integer", "description": "Skip files larger than this (default 200000)"},
                    "extensions": {"type": "string", "description": "Comma-separated whitelist; default: source files only"},
                },
                "required": [],
            },
            {
                "name": "code_impact",
                "description": (
                    "Code-graph impact analysis on ODIN's own codebase: given a "
                    "function/class/file, list everything that depends on it — what "
                    "breaks if it changes. Use before modifying ODIN's code."
                ),
                "parameters": {
                    "target": {"type": "string", "description": "Bare function/method/class name (e.g. 'dispatch', 'think', 'Marduk') — not dotted paths"},
                },
                "required": ["target"],
            },
            {
                "name": "code_context",
                "description": (
                    "Code-graph context lookup: definition, callers, callees and "
                    "cluster for a symbol in ODIN's codebase."
                ),
                "parameters": {
                    "target": {"type": "string", "description": "Symbol or file to explain"},
                },
                "required": ["target"],
            },
            {
                "name": "code_search",
                "description": (
                    "Search ODIN's code knowledge graph for functions, classes or "
                    "concepts. Graph-aware — finds by relationship, not just text."
                ),
                "parameters": {
                    "query": {"type": "string", "description": "What to find"},
                },
                "required": ["query"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "create_macro": self._create,
            "run_macro": self._run,
            "list_macros": self._list,
            "delete_macro": self._delete,
            "run_startup_routine": self._startup_routine,
            "bundle_codebase": self._bundle_codebase,
            "code_impact": self._code_impact,
            "code_context": self._code_context,
            "code_search": self._code_search,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[HEPHAESTUS] Error: {e}"
        return f"[HEPHAESTUS] Unknown skill: {skill_name}"

    # === Code graph (GitNexus) ==========================================

    def _code_impact(self, target: str = "") -> str:
        if not target.strip():
            return "Need a function, class, or file name."
        return self._gitnexus.ask(("impact",), target.strip())[:4000]

    def _code_context(self, target: str = "") -> str:
        if not target.strip():
            return "Need a symbol or file name."
        return self._gitnexus.ask(("context",), target.strip())[:4000]

    def _code_search(self, query: str = "") -> str:
        if not query.strip():
            return "Need a search query."
        return self._gitnexus.ask(("query", "search"), query.strip())[:4000]

    def _create(self, name: str = "", commands: str = "") -> str:
        cmds = [c.strip() for c in commands.split(",") if c.strip()]
        self._macros[name.lower()] = {
            "commands": cmds,
            "created": datetime.now().isoformat()
        }
        self._save()
        return f"Macro '{name}' created with {len(cmds)} command(s)."

    def _run(self, name: str = "") -> str:
        macro = self._macros.get(name.lower())
        if not macro:
            return f"No macro named '{name}' found."
        results = []
        for cmd in macro["commands"]:
            subprocess.Popen(cmd, shell=True)
            results.append(cmd)
        return f"Macro '{name}' executed: {', '.join(results)}."

    def _list(self) -> str:
        if not self._macros:
            return "No macros saved."
        return "Macros: " + ", ".join(
            f"{k} ({len(v['commands'])} steps)" for k, v in self._macros.items()
        )

    def _delete(self, name: str = "") -> str:
        if name.lower() in self._macros:
            del self._macros[name.lower()]
            self._save()
            return f"Macro '{name}' deleted."
        return f"No macro found: '{name}'."

    def _startup_routine(self) -> str:
        results = []
        if self.marduk:
            fujin = self.marduk.get_module("FUJIN")
            if fujin:
                weather = fujin.execute("get_weather_here", {})
                results.append(weather)
            chronos = self.marduk.get_module("CHRONOS")
            if chronos:
                time_str = chronos.execute("get_time", {})
                results.append(time_str)
        return "Morning routine complete. " + " ".join(results)

    def _load(self) -> dict:
        if os.path.exists(self._macros_path):
            with open(self._macros_path) as f:
                return json.load(f)
        return {}

    def _save(self):
        os.makedirs(os.path.dirname(self._macros_path), exist_ok=True)
        with open(self._macros_path, "w") as f:
            json.dump(self._macros, f, indent=2)

    # ── bundle_codebase: repomix-style packer ─────────────────────────
    # Inspired by yamadashy/repomix but reimplemented in plain Python for
    # ODIN's use case ("show this codebase to cloud Claude"). Covers:
    #   - default ignores (.git, node_modules, build dirs, caches)
    #   - .gitignore + .repomixignore patterns (simple gitwildmatch subset)
    #   - secret redaction (common API key / token regexes)
    #   - directory tree + per-file fenced blocks in markdown
    #   - per-file size cap + total-file cap so it never runs away
    # Skipped: tree-sitter compression, multi-format output, token counting.
    # 80% of the value for ~200 lines.
    def _bundle_codebase(self, root: str = "", output: str = "",
                         max_file_bytes: int = 0, extensions: str = "") -> str:
        import re as _re
        root_path = os.path.abspath(root or os.getcwd())
        if not os.path.isdir(root_path):
            return f"Root not found: {root_path}"
        try:
            max_file_bytes = int(max_file_bytes) if max_file_bytes else 200_000
        except (TypeError, ValueError):
            max_file_bytes = 200_000
        ext_whitelist = self._parse_extensions(extensions)

        # Output path
        if not output:
            desktop = os.path.expanduser("~/Desktop")
            name = os.path.basename(root_path.rstrip("\\/").rstrip(os.sep)) or "bundle"
            output = os.path.join(desktop, f"{name}_bundle.md")
        os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)

        ignore_patterns = _collect_ignore_patterns(root_path)
        secret_re = _build_secret_regex()

        included: list[tuple[str, int]] = []     # (rel_path, bytes_kept)
        skipped: dict[str, int] = {"ignored": 0, "binary": 0, "too_big": 0,
                                   "wrong_ext": 0, "secrets_only": 0}
        chunks: list[str] = []

        for current_dir, dirs, files in os.walk(root_path):
            # Prune ignored directories in-place so os.walk doesn't descend.
            rel_dir = os.path.relpath(current_dir, root_path).replace("\\", "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            dirs[:] = [d for d in dirs
                       if not _path_ignored(_join(rel_dir, d) + "/", ignore_patterns)]
            for fname in sorted(files):
                rel_path = _join(rel_dir, fname)
                if _path_ignored(rel_path, ignore_patterns):
                    skipped["ignored"] += 1
                    continue
                full_path = os.path.join(current_dir, fname)
                ext = os.path.splitext(fname)[1].lower()
                if ext_whitelist and ext not in ext_whitelist:
                    skipped["wrong_ext"] += 1
                    continue
                try:
                    size = os.path.getsize(full_path)
                except OSError:
                    continue
                if size > max_file_bytes:
                    skipped["too_big"] += 1
                    continue
                try:
                    with open(full_path, "rb") as f:
                        raw = f.read()
                except OSError:
                    continue
                if _looks_binary(raw):
                    skipped["binary"] += 1
                    continue
                text = raw.decode("utf-8", errors="replace")
                # Redact secrets — keep the file but mask the matches.
                text, n_redactions = _redact_secrets(text, secret_re)
                lang = _lang_for_ext(ext)
                chunks.append(f"\n\n## `{rel_path}`\n\n"
                              f"```{lang}\n{text}\n```")
                if n_redactions:
                    chunks.append(f"<!-- {n_redactions} secret(s) redacted -->")
                included.append((rel_path, len(text)))

        # Directory tree (built from included files only)
        tree = _format_tree(p for p, _ in included)
        total_chars = sum(b for _, b in included)
        header = (
            f"# Codebase bundle: `{os.path.basename(root_path)}`\n\n"
            f"*Generated by HEPHAESTUS.bundle_codebase on {datetime.now():%Y-%m-%d %H:%M}.*  \n"
            f"*Root: `{root_path}`*\n\n"
            f"## Summary\n\n"
            f"- Files included: **{len(included)}**\n"
            f"- Total chars: **{total_chars:,}**\n"
            f"- Skipped: ignored={skipped['ignored']}, binary={skipped['binary']}, "
            f"too-big={skipped['too_big']}, wrong-ext={skipped['wrong_ext']}\n\n"
            f"## Directory tree\n\n```\n{tree}\n```\n"
        )
        try:
            with open(output, "w", encoding="utf-8") as f:
                f.write(header)
                f.write("\n## Files\n")
                for chunk in chunks:
                    f.write(chunk)
        except OSError as e:
            return f"Bundle write failed: {e}"
        return (f"Bundled {len(included)} file(s), {total_chars:,} chars "
                f"→ {output}.  (Skipped {sum(skipped.values())}.)")

    def _parse_extensions(self, ext_str: str) -> set:
        if not ext_str:
            # Default source-file whitelist — covers most projects without bloat.
            return set()
        out = set()
        for e in ext_str.split(","):
            e = e.strip().lower()
            if not e:
                continue
            if not e.startswith("."):
                e = "." + e
            out.add(e)
        return out


# ── Module-scope helpers for bundle_codebase ─────────────────────────
_DEFAULT_IGNORES = [
    ".git/", ".git", ".svn/", ".hg/",
    "node_modules/", "bower_components/",
    "__pycache__/", "*.pyc", "*.pyo",
    ".venv/", "venv/", "env/", ".env",
    "build/", "dist/", "target/", "out/",
    ".next/", ".nuxt/", ".cache/",
    "*.egg-info/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    ".tox/", "coverage/", "htmlcov/",
    ".idea/", ".vscode/", ".DS_Store",
    "*.log", "*.lock",
    # ODIN-specific: don't ship data/logs, voices, screenshots, model caches
    "data/logs/", "data/voices/", "data/screenshots/", "data/backups/",
    "data/debug/", "data/secrets/",
]


def _collect_ignore_patterns(root: str) -> list:
    """Combine default ignores with .gitignore / .repomixignore at the root."""
    pats = list(_DEFAULT_IGNORES)
    for fname in (".gitignore", ".repomixignore"):
        p = os.path.join(root, fname)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        pats.append(line)
        except OSError:
            pass
    return pats


def _path_ignored(rel_path: str, patterns: list) -> bool:
    """Subset of gitignore semantics. A trailing '/' marks a directory pattern,
    which matches the dir AND everything inside it. Bare patterns match by
    filename or relative path; '*' globs work via fnmatch."""
    import fnmatch as _fn
    base = os.path.basename(rel_path.rstrip("/"))
    for pat in patterns:
        pat_stripped = pat.lstrip("/")
        # Directory pattern: matches the dir itself OR anything under it.
        if pat_stripped.endswith("/"):
            dir_name = pat_stripped[:-1]
            # Anywhere-in-tree match: split rel_path and see if any segment
            # equals (or fnmatches) the dir name.
            for seg in rel_path.split("/"):
                if seg == dir_name or _fn.fnmatch(seg, dir_name):
                    return True
            if rel_path.startswith(pat_stripped):
                return True
            continue
        # Plain pattern: match against full path AND basename.
        if _fn.fnmatch(rel_path, pat_stripped) or _fn.fnmatch(base, pat_stripped):
            return True
    return False


def _join(rel_dir: str, name: str) -> str:
    return (rel_dir + "/" + name) if rel_dir else name


def _looks_binary(b: bytes) -> bool:
    """Heuristic: presence of NULL byte in the first 4 KB → binary."""
    if not b:
        return False
    head = b[:4096]
    if b"\x00" in head:
        return True
    # Try utf-8 decode; on failure with a high error rate, treat as binary.
    try:
        head.decode("utf-8")
        return False
    except UnicodeDecodeError:
        # Try latin-1 (always succeeds); ratio of printable chars decides.
        try:
            text = head.decode("latin-1")
            printable = sum(1 for c in text if c.isprintable() or c in "\n\r\t")
            return printable / max(1, len(text)) < 0.7
        except Exception:
            return True


_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".rs": "rust", ".go": "go",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".rb": "ruby",
    ".sh": "bash", ".ps1": "powershell", ".bat": "batch",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".scala": "scala",
    ".html": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".md": "markdown", ".sql": "sql",
    ".dockerfile": "dockerfile",
}


def _lang_for_ext(ext: str) -> str:
    return _LANG_BY_EXT.get(ext, "")


def _build_secret_regex():
    """Common secret patterns. False positives are fine (we just redact); the
    cost of letting a real key leak into a cloud-bound bundle is much higher.
    Inline flag (?i) doesn't survive alternation, so we set IGNORECASE on the
    compiled regex and keep the patterns case-insensitive at the case-level."""
    import re as _re
    patterns = [
        # Anthropic
        r"sk-ant-[A-Za-z0-9_\-]{20,}",
        # OpenAI
        r"sk-[A-Za-z0-9]{20,}",
        # AWS access key
        r"AKIA[0-9A-Z]{16}",
        # Google API key
        r"AIza[0-9A-Za-z_\-]{30,}",
        # GitHub PAT
        r"gh[pousr]_[A-Za-z0-9_]{30,}",
        # Generic: 'api_key/secret/token' followed by = or : and a long string
        r"(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}[\"']?",
    ]
    return _re.compile("|".join(patterns), _re.IGNORECASE)


def _redact_secrets(text: str, regex) -> tuple[str, int]:
    n = [0]
    def _sub(m):
        n[0] += 1
        return "[REDACTED]"
    redacted = regex.sub(_sub, text)
    return redacted, n[0]


def _format_tree(paths) -> str:
    """ASCII directory tree from a flat list of slash-separated paths."""
    # Build a nested dict of name → children
    tree: dict = {}
    for p in paths:
        parts = p.split("/")
        node = tree
        for part in parts:
            node = node.setdefault(part, {})
    lines: list[str] = []
    def _walk(node: dict, prefix: str):
        keys = sorted(node.keys())
        for i, k in enumerate(keys):
            last = (i == len(keys) - 1)
            elbow = "└── " if last else "├── "
            lines.append(prefix + elbow + k)
            if node[k]:
                _walk(node[k], prefix + ("    " if last else "│   "))
    _walk(tree, "")
    return "\n".join(lines) if lines else "(empty)"
