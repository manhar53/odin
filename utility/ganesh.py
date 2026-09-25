# GANESH — Hindu remover of obstacles, scribe of the Mahabharata, patron of
# arts, sciences, and merchant guilds. Mythologically he took Vyasa's
# dictation; in ODIN he handles spreadsheets — read, write, summarize,
# apply formulas, export to CSV. The on-the-nose pairing with VYASA
# (compiler-sage / scribe) is intentional.
#
# Direct openpyxl, no MCP. excel-mcp-server is the standard "expose Excel
# to AI" library but it's MCP-only and ODIN doesn't have an MCP host yet.
# openpyxl gives us the same capability with a fraction of the moving
# parts and integrates with MARDUK directly.
#
# Path resolution: GANESH accepts paths in many forms users speak —
# bare filenames (searches Desktop / Documents / Drive / Brain), absolute
# paths, ~/Documents-style paths. _resolve_path does the lookup.

import csv
import io
import os
import re
from datetime import datetime
from core.marduk import OdinModule

try:
    import openpyxl
    from openpyxl.utils import get_column_letter
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False


# Searched in priority order when the user gives a bare filename ("budget.xlsx").
# First match wins. Drive mount is included so files synced from Drive
# desktop work without specifying G:\My Drive.
_SEARCH_ROOTS = [
    os.path.expanduser("~/Desktop"),
    os.path.expanduser("~/Documents"),
    os.path.expanduser("~/Downloads"),
    r"G:\My Drive",
    os.path.expanduser("~/Brain"),
]

# Recognised extensions. openpyxl handles .xlsx / .xlsm / .xltx / .xltm.
# .xls (legacy Excel) needs xlrd and read-only; we explain the limitation.
_XL_EXTS = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"})
_XL_LEGACY = frozenset({".xls"})


class Ganesh(OdinModule):
    MODULE_NAME = "GANESH"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("ganesh", {})
        # Extra search roots from config — useful for project-specific Excel
        # folders the user wants ODIN to find by name.
        self.extra_roots: list[str] = [
            p for p in (cfg.get("search_roots") or []) if isinstance(p, str)
        ]
        # Hard cap on cells returned by a single read() — prevents a single
        # call from blowing up the LLM context with a 100k-row sheet.
        self.max_read_cells = int(cfg.get("max_read_cells", 2000))

        if not _HAS_OPENPYXL:
            print("[GANESH] openpyxl missing — Excel skills disabled. "
                  "Install with: python -m pip install openpyxl")
        else:
            print(f"[GANESH] Online (openpyxl {openpyxl.__version__}).")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "excel_read",
                "description": (
                    "Read cells from an Excel workbook. Returns a formatted table. "
                    "Accepts a path or a bare filename (GANESH searches Desktop, "
                    "Documents, Downloads, Drive, Brain). Optional sheet name (default: "
                    "first sheet) and A1-style range (default: first 30 rows × all cols)."
                ),
                "parameters": {
                    "path": {"type": "string", "description": "File path or name (e.g. 'budget.xlsx')"},
                    "sheet": {"type": "string", "description": "Sheet name (optional)"},
                    "cell_range": {"type": "string", "description": "A1 range, e.g. 'A1:D20' (optional)"},
                },
                "required": ["path"],
            },
            {
                "name": "excel_summarize",
                "description": (
                    "Summarize an Excel workbook: list of sheets, dimensions, "
                    "and the column headers of each sheet. Use before deeper "
                    "operations so you know the structure."
                ),
                "parameters": {
                    "path": {"type": "string", "description": "File path or name"},
                },
                "required": ["path"],
            },
            {
                "name": "excel_write",
                "description": (
                    "Write a value (or formula) to a single cell. Saves the "
                    "workbook in place. Use 'cell' in A1 form (e.g. 'B5'). "
                    "Formulas start with '=' as usual."
                ),
                "parameters": {
                    "path":  {"type": "string", "description": "File path or name"},
                    "sheet": {"type": "string", "description": "Sheet name (optional; default first)"},
                    "cell":  {"type": "string", "description": "A1 cell reference, e.g. 'B5'"},
                    "value": {"type": "string", "description": "Value or formula to write"},
                },
                "required": ["path", "cell", "value"],
            },
            {
                "name": "excel_create",
                "description": (
                    "Create a new Excel workbook at the given path. Optionally "
                    "names the first sheet. Path can be a bare filename — saved "
                    "to Desktop by default."
                ),
                "parameters": {
                    "path":       {"type": "string", "description": "File path or filename (e.g. 'budget.xlsx')"},
                    "sheet_name": {"type": "string", "description": "Name of the first sheet (default 'Sheet1')"},
                },
                "required": ["path"],
            },
            {
                "name": "excel_append_row",
                "description": (
                    "Append a row of values to the bottom of a sheet. Pass "
                    "values as a comma-separated string. Useful for logging "
                    "structured data like expense entries."
                ),
                "parameters": {
                    "path":   {"type": "string", "description": "File path or name"},
                    "sheet":  {"type": "string", "description": "Sheet name (optional)"},
                    "values": {"type": "string", "description": "Comma-separated values, e.g. '2026-05-13,groceries,42.50'"},
                },
                "required": ["path", "values"],
            },
            {
                "name": "excel_to_csv",
                "description": (
                    "Export a sheet to CSV alongside the workbook. Returns the "
                    "CSV path. Convenient for piping spreadsheet data into "
                    "anything that doesn't speak xlsx."
                ),
                "parameters": {
                    "path":  {"type": "string", "description": "Excel file path or name"},
                    "sheet": {"type": "string", "description": "Sheet name (optional; default first)"},
                },
                "required": ["path"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        if not _HAS_OPENPYXL:
            return "Excel skills unavailable — openpyxl missing."
        _map = {
            "excel_read":       self._read,
            "excel_summarize":  self._summarize,
            "excel_write":      self._write,
            "excel_create":     self._create,
            "excel_append_row": self._append_row,
            "excel_to_csv":     self._to_csv,
        }
        fn = _map.get(skill_name)
        if not fn:
            return f"[GANESH] Unknown skill: {skill_name}"
        try:
            return fn(**args)
        except FileNotFoundError as e:
            return f"[GANESH] File not found: {e}"
        except PermissionError as e:
            return f"[GANESH] Permission denied: {e}. Is the workbook open in Excel? Close it."
        except Exception as e:
            return f"[GANESH] Error: {e}"

    # ── Operations ───────────────────────────────────────────────────
    def _read(self, path: str, sheet: str = "", cell_range: str = "") -> str:
        full_path = self._resolve_path(path, must_exist=True)
        wb = openpyxl.load_workbook(full_path, data_only=True)
        ws = self._pick_sheet(wb, sheet)
        if cell_range:
            try:
                rows = list(ws[cell_range])
            except Exception:
                return f"Invalid range: {cell_range!r}"
        else:
            max_row = min(ws.max_row, 30)
            max_col = min(ws.max_column, 12)
            rows = list(ws.iter_rows(min_row=1, max_row=max_row,
                                     min_col=1, max_col=max_col))
        cells_seen = 0
        out_rows: list[list[str]] = []
        for row in rows:
            out_row = []
            for c in row:
                v = c.value
                out_row.append("" if v is None else str(v))
                cells_seen += 1
                if cells_seen >= self.max_read_cells:
                    break
            out_rows.append(out_row)
            if cells_seen >= self.max_read_cells:
                break
        head = f"{ws.title} ({ws.max_row}×{ws.max_column}, showing {len(out_rows)} row(s))"
        body = _format_table(out_rows)
        return f"{head}\n{body}"

    def _summarize(self, path: str) -> str:
        full_path = self._resolve_path(path, must_exist=True)
        wb = openpyxl.load_workbook(full_path, data_only=True, read_only=True)
        lines = [f"Workbook: {os.path.basename(full_path)}  ({len(wb.sheetnames)} sheet(s))"]
        for name in wb.sheetnames:
            ws = wb[name]
            headers = []
            for c in next(ws.iter_rows(min_row=1, max_row=1, max_col=10), []):
                if c.value is not None:
                    headers.append(str(c.value)[:30])
            dim = f"{ws.max_row or 0}×{ws.max_column or 0}"
            head_str = ", ".join(headers) if headers else "(no headers)"
            lines.append(f"  • {name} [{dim}]  headers: {head_str}")
        wb.close()
        return "\n".join(lines)

    def _write(self, path: str, cell: str, value: str, sheet: str = "") -> str:
        full_path = self._resolve_path(path, must_exist=True)
        wb = openpyxl.load_workbook(full_path)
        ws = self._pick_sheet(wb, sheet)
        coerced = self._coerce_value(value)
        ws[cell] = coerced
        wb.save(full_path)
        return f"Set {ws.title}!{cell} = {coerced!r}."

    def _create(self, path: str, sheet_name: str = "") -> str:
        full_path = self._resolve_path(path, must_exist=False, create_default_root=True)
        if os.path.exists(full_path):
            return f"File already exists: {full_path}. Use excel_write to modify it."
        wb = openpyxl.Workbook()
        ws = wb.active
        if sheet_name:
            ws.title = sheet_name[:31]   # Excel sheet name limit
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        wb.save(full_path)
        return f"Created {full_path}."

    def _append_row(self, path: str, values: str, sheet: str = "") -> str:
        full_path = self._resolve_path(path, must_exist=True)
        wb = openpyxl.load_workbook(full_path)
        ws = self._pick_sheet(wb, sheet)
        row = [self._coerce_value(v.strip()) for v in values.split(",")]
        ws.append(row)
        wb.save(full_path)
        return f"Appended row to {ws.title}: {row}"

    def _to_csv(self, path: str, sheet: str = "") -> str:
        full_path = self._resolve_path(path, must_exist=True)
        wb = openpyxl.load_workbook(full_path, data_only=True, read_only=True)
        ws = self._pick_sheet(wb, sheet)
        out_path = os.path.splitext(full_path)[0] + f"_{ws.title}.csv"
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            for row in ws.iter_rows(values_only=True):
                w.writerow(["" if c is None else c for c in row])
        wb.close()
        return f"Exported {ws.title} → {out_path}"

    # ── Helpers ──────────────────────────────────────────────────────
    def _pick_sheet(self, wb, sheet_name: str):
        if sheet_name:
            if sheet_name in wb.sheetnames:
                return wb[sheet_name]
            # Case-insensitive fallback — voice users won't preserve case.
            for name in wb.sheetnames:
                if name.lower() == sheet_name.lower():
                    return wb[name]
            raise KeyError(f"No sheet named {sheet_name!r}. Available: {wb.sheetnames}")
        return wb.active

    def _resolve_path(self, raw: str, must_exist: bool,
                      create_default_root: bool = False) -> str:
        raw = (raw or "").strip().strip('"\'')
        if not raw:
            raise ValueError("Empty path")
        # Expand ~ and env vars.
        expanded = os.path.expandvars(os.path.expanduser(raw))
        # Absolute or contains a separator → trust it (with .xlsx extension hint).
        if os.path.isabs(expanded) or any(sep in expanded for sep in ("\\", "/")):
            full = expanded
            full = self._with_xlsx_ext(full)
            if must_exist and not os.path.exists(full):
                # Last try: legacy .xls.
                alt = os.path.splitext(full)[0] + ".xls"
                if os.path.exists(alt):
                    raise ValueError(f".xls (legacy) file at {alt} — openpyxl can't read it. Re-save as .xlsx in Excel.")
                raise FileNotFoundError(full)
            return full
        # Bare filename — search.
        candidate = self._with_xlsx_ext(expanded)
        for root in (self.extra_roots + _SEARCH_ROOTS):
            if not os.path.isdir(root):
                continue
            full = os.path.join(root, candidate)
            if os.path.exists(full):
                return full
            # Try a shallow recursive walk for nested folders (max 2 levels deep).
            for sub_root, dirs, files in os.walk(root):
                rel_depth = sub_root[len(root):].count(os.sep)
                if rel_depth > 2:
                    dirs[:] = []
                    continue
                if candidate in files:
                    return os.path.join(sub_root, candidate)
        if must_exist:
            raise FileNotFoundError(
                f"{candidate!r} not found in Desktop / Documents / Downloads / "
                f"Drive / Brain (and 2-level subfolders)."
            )
        if create_default_root:
            # New file: default to Desktop.
            desktop = os.path.expanduser("~/Desktop")
            os.makedirs(desktop, exist_ok=True)
            return os.path.join(desktop, candidate)
        return candidate

    def _with_xlsx_ext(self, name: str) -> str:
        ext = os.path.splitext(name)[1].lower()
        if ext in _XL_EXTS or ext in _XL_LEGACY:
            return name
        return name + ".xlsx"

    def _coerce_value(self, v):
        """Voice users say '42.50' (string); cells should hold numbers when
        the input is numeric. Formulas pass through as strings (openpyxl
        interprets a leading '=' automatically)."""
        if not isinstance(v, str):
            return v
        v = v.strip()
        if v.startswith("="):
            return v
        # Numeric coercion
        try:
            if "." in v or "e" in v.lower():
                return float(v)
            return int(v)
        except ValueError:
            pass
        # Date — basic YYYY-MM-DD
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", v)
        if m:
            try:
                return datetime(*(int(x) for x in m.groups()))
            except ValueError:
                pass
        return v


def _format_table(rows: list[list[str]]) -> str:
    if not rows:
        return "(empty)"
    widths = [0] * max(len(r) for r in rows)
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], min(30, len(c)))
    out = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            c = c if len(c) <= 30 else c[:27] + "..."
            cells.append(c.ljust(widths[i]))
        out.append(" | ".join(cells))
    return "\n".join(out)
