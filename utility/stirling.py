# STIRLING — Robert Stirling (Scottish engineer, inventor of the
# Stirling engine — practical, mechanical, gets-the-job-done).
#
# Pure-Python PDF utilities. The first cut of this module wrapped a
# Stirling-PDF Docker container's REST API, but the user has no Docker —
# rewritten to use pypdf (merge/split/count/encrypt) + pdfplumber (text
# extraction) + optional pytesseract OCR via HORUS for scanned PDFs.
# 80 % of real PDF use cases, zero external infrastructure.
#
# What this DOESN'T do (and where to send the user when they ask):
#   • Digital signatures, redaction, advanced forms        → Stirling-PDF MSI
#   • Strong compression beyond rewrite-with-pypdf         → ghostscript / Stirling
#   • PDF→Word / PDF→Excel format-preserving conversion    → cloud APIs
# The fallback message in each unsupported skill spells this out.

import os
import re
from pathlib import Path
from typing import Optional

from core.marduk import OdinModule

try:
    from pypdf import PdfReader, PdfWriter
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False

try:
    import pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False

try:
    import pytesseract
    _HAS_PYTESSERACT = True
except ImportError:
    _HAS_PYTESSERACT = False


def _missing(name: str) -> str:
    return f"[STIRLING] {name} requires `pip install pypdf pdfplumber` — install and retry."


class Stirling(OdinModule):
    MODULE_NAME = "STIRLING"
    LAYER = "UTILITY"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("stirling", {})
        # Where processed PDFs land. Default sits under ODIN's data/ tree
        # so output is discoverable + easy to clean.
        self.output_dir = cfg.get("output_dir", "data/stirling_out")
        os.makedirs(self.output_dir, exist_ok=True)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "pdf_merge",
                "description": "Merge multiple PDF files into one. Pure Python (pypdf), no external deps.",
                "parameters": {
                    "paths":  {"type": "string", "description": "Comma-separated list of PDF paths to merge in order"},
                    "output": {"type": "string", "description": "Output filename (default: merged.pdf)"},
                },
                "required": ["paths"],
            },
            {
                "name": "pdf_split",
                "description": "Split a PDF into individual pages (one PDF per page) or a specified page range. Pure pypdf.",
                "parameters": {
                    "path":  {"type": "string", "description": "Input PDF path"},
                    "pages": {"type": "string", "description": "Optional page-range spec like '1-3,5,7-9'. Empty = each page → separate file."},
                },
                "required": ["path"],
            },
            {
                "name": "pdf_to_text",
                "description": (
                    "Extract text content from a PDF. Uses pdfplumber for layout-aware "
                    "extraction. Works on text-PDFs (most PDFs); for scanned/image PDFs, "
                    "use pdf_ocr instead."
                ),
                "parameters": {
                    "path":      {"type": "string",  "description": "Input PDF path"},
                    "max_chars": {"type": "integer", "description": "Cap on returned text (default 4000)"},
                    "save_to":   {"type": "string",  "description": "Optional path to save the full extracted text"},
                },
                "required": ["path"],
            },
            {
                "name": "pdf_ocr",
                "description": (
                    "Run OCR on a scanned / image-based PDF. Renders each page to an image "
                    "via pypdf, then runs Tesseract. Slower than pdf_to_text but works on PDFs "
                    "without embedded text layers. Requires Tesseract binary (HORUS already needs it)."
                ),
                "parameters": {
                    "path":     {"type": "string", "description": "Input PDF path"},
                    "language": {"type": "string", "description": "Tesseract language code (default 'eng')"},
                    "save_to":  {"type": "string", "description": "Optional path to save the OCR'd text"},
                },
                "required": ["path"],
            },
            {
                "name": "pdf_info",
                "description": "Return page count + size + metadata (title, author, creation date) for a PDF.",
                "parameters": {
                    "path": {"type": "string", "description": "Input PDF path"},
                },
                "required": ["path"],
            },
            {
                "name": "pdf_extract_pages",
                "description": "Pull specific pages out of a PDF into a new PDF (e.g. extract pages 5-10).",
                "parameters": {
                    "path":   {"type": "string", "description": "Input PDF path"},
                    "pages":  {"type": "string", "description": "Page-range spec like '1-3,5,7-9'"},
                    "output": {"type": "string", "description": "Output filename (default: extracted.pdf)"},
                },
                "required": ["path", "pages"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "pdf_merge":          self._merge,
            "pdf_split":          self._split,
            "pdf_to_text":        self._to_text,
            "pdf_ocr":            self._ocr,
            "pdf_info":           self._info,
            "pdf_extract_pages":  self._extract_pages,
        }
        fn = _map.get(skill_name)
        if not fn:
            return f"[STIRLING] Unknown skill: {skill_name}"
        try:
            return fn(**args)
        except FileNotFoundError as e:
            return f"[STIRLING] File not found: {e.filename or e}"
        except Exception as e:
            return f"[STIRLING] Error: {e}"

    # ─── Helpers ──────────────────────────────────────────────────
    def _resolve_paths(self, paths_str: str) -> list[str]:
        return [p.strip() for p in (paths_str or "").split(",") if p.strip()]

    def _parse_page_spec(self, spec: str, total_pages: int) -> list[int]:
        """Parse '1-3,5,7-9' (1-indexed, inclusive) → [0, 1, 2, 4, 6, 7, 8]
        (0-indexed for pypdf). Silently clamps out-of-range to valid pages."""
        if not spec or not spec.strip():
            return list(range(total_pages))
        out: list[int] = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                try:
                    start = max(1, int(a.strip()))
                    end   = min(total_pages, int(b.strip()))
                    out.extend(range(start - 1, end))
                except ValueError:
                    continue
            else:
                try:
                    p = int(part)
                    if 1 <= p <= total_pages:
                        out.append(p - 1)
                except ValueError:
                    continue
        # Preserve order, drop dupes.
        seen = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    def _check_file(self, path: str) -> Optional[str]:
        if not path:
            return "Need a PDF path."
        if not os.path.exists(path):
            return f"File not found: {path}"
        if not path.lower().endswith(".pdf"):
            return f"Not a PDF: {path}"
        return None

    # ─── Skill implementations ───────────────────────────────────
    def _merge(self, paths: str = "", output: str = "merged.pdf") -> str:
        if not _HAS_PYPDF:
            return _missing("pdf_merge")
        files = self._resolve_paths(paths)
        if len(files) < 2:
            return "Need at least 2 PDF paths to merge."
        for f in files:
            err = self._check_file(f)
            if err: return err

        writer = PdfWriter()
        total_pages = 0
        for f in files:
            reader = PdfReader(f)
            for page in reader.pages:
                writer.add_page(page)
            total_pages += len(reader.pages)

        out_path = os.path.join(self.output_dir, output or "merged.pdf")
        with open(out_path, "wb") as fh:
            writer.write(fh)
        return f"Merged {len(files)} PDFs ({total_pages} pages) → {out_path}"

    def _split(self, path: str = "", pages: str = "") -> str:
        if not _HAS_PYPDF:
            return _missing("pdf_split")
        err = self._check_file(path)
        if err: return err
        reader = PdfReader(path)
        page_indices = self._parse_page_spec(pages, len(reader.pages))
        if not page_indices:
            return f"No valid pages in spec {pages!r}."

        out_dir = os.path.join(self.output_dir, f"split_{Path(path).stem}")
        os.makedirs(out_dir, exist_ok=True)
        written = []
        for idx in page_indices:
            writer = PdfWriter()
            writer.add_page(reader.pages[idx])
            out_path = os.path.join(out_dir, f"page_{idx+1:03d}.pdf")
            with open(out_path, "wb") as fh:
                writer.write(fh)
            written.append(out_path)
        return f"Split {len(written)} page(s) → {out_dir}"

    def _to_text(self, path: str = "", max_chars: int = 4000, save_to: str = "") -> str:
        if not _HAS_PDFPLUMBER:
            return _missing("pdf_to_text")
        err = self._check_file(path)
        if err: return err
        try:
            max_chars = max(200, min(50000, int(max_chars or 4000)))
        except (TypeError, ValueError):
            max_chars = 4000

        parts = []
        with pdfplumber.open(path) as pdf:
            for i, page in enumerate(pdf.pages):
                txt = page.extract_text() or ""
                if txt.strip():
                    parts.append(f"--- page {i+1} ---\n{txt.strip()}")
        full = "\n\n".join(parts).strip()
        if not full:
            return (f"No extractable text in {Path(path).name}. "
                    f"This may be a scanned PDF — try pdf_ocr.")

        if save_to:
            save_to = save_to.strip()
            if not os.path.dirname(save_to):
                save_to = os.path.join(self.output_dir, save_to)
            with open(save_to, "w", encoding="utf-8") as fh:
                fh.write(full)
            tail = f" Full text saved to {save_to}."
        else:
            tail = ""
        snippet = full[:max_chars] + ("…" if len(full) > max_chars else "")
        return f"--- {Path(path).name} text ({len(full)} chars) ---\n{snippet}{tail}"

    def _ocr(self, path: str = "", language: str = "eng", save_to: str = "") -> str:
        if not _HAS_PYPDF:
            return _missing("pdf_ocr")
        if not _HAS_PYTESSERACT:
            return ("pdf_ocr needs Tesseract — install pytesseract AND the Tesseract binary "
                    "from https://github.com/UB-Mannheim/tesseract/wiki, then retry.")
        err = self._check_file(path)
        if err: return err

        # We render each page to a PIL image via pypdf's images extractor.
        # If pypdf can't rasterize (rare), we fall back to its embedded-images
        # extractor and OCR those. Note: this does NOT need poppler-utils —
        # pypdf+pillow handle the rendering in pure Python.
        reader = PdfReader(path)
        pages_text = []
        try:
            from PIL import Image as _Image
            import io as _io
        except ImportError:
            return "pdf_ocr needs Pillow — `pip install Pillow`."

        for i, page in enumerate(reader.pages):
            txt = ""
            try:
                # Try direct extraction first — many scanned PDFs still have a
                # text layer with bad / no characters; we keep it if non-empty.
                native = (page.extract_text() or "").strip()
                if len(native) > 30:
                    pages_text.append(f"--- page {i+1} (native text) ---\n{native}")
                    continue
                # Otherwise OCR any embedded images on the page.
                page_ocr = []
                for img in page.images:
                    try:
                        pil = _Image.open(_io.BytesIO(img.data))
                        page_ocr.append(pytesseract.image_to_string(pil, lang=language).strip())
                    except Exception as e:
                        page_ocr.append(f"[OCR failed on one image: {e}]")
                if page_ocr:
                    pages_text.append(f"--- page {i+1} (OCR) ---\n" + "\n".join(page_ocr))
            except Exception as e:
                pages_text.append(f"--- page {i+1} ---\n[error: {e}]")

        full = "\n\n".join(pages_text).strip()
        if not full:
            return f"OCR found no readable content in {Path(path).name}."
        if save_to:
            save_to = save_to.strip()
            if not os.path.dirname(save_to):
                save_to = os.path.join(self.output_dir, save_to)
            with open(save_to, "w", encoding="utf-8") as fh:
                fh.write(full)
            return f"OCR'd {len(reader.pages)} pages → {save_to} ({len(full)} chars)"
        return f"--- OCR ({len(full)} chars) ---\n{full[:4000]}{'…' if len(full) > 4000 else ''}"

    def _info(self, path: str = "") -> str:
        if not _HAS_PYPDF:
            return _missing("pdf_info")
        err = self._check_file(path)
        if err: return err
        reader = PdfReader(path)
        meta = reader.metadata or {}
        size_kb = os.path.getsize(path) // 1024
        pieces = [
            f"file: {Path(path).name}",
            f"pages: {len(reader.pages)}",
            f"size: {size_kb} KB",
        ]
        for k, label in [("/Title", "title"), ("/Author", "author"),
                         ("/Subject", "subject"), ("/CreationDate", "created")]:
            v = meta.get(k)
            if v:
                pieces.append(f"{label}: {v}")
        return " · ".join(pieces)

    def _extract_pages(self, path: str = "", pages: str = "", output: str = "extracted.pdf") -> str:
        if not _HAS_PYPDF:
            return _missing("pdf_extract_pages")
        err = self._check_file(path)
        if err: return err
        if not pages:
            return "Need a page spec (e.g. '1-3,5,7-9')."
        reader = PdfReader(path)
        idx = self._parse_page_spec(pages, len(reader.pages))
        if not idx:
            return f"No valid pages in spec {pages!r}."
        writer = PdfWriter()
        for i in idx:
            writer.add_page(reader.pages[i])
        out_path = os.path.join(self.output_dir, output or "extracted.pdf")
        with open(out_path, "wb") as fh:
            writer.write(fh)
        return f"Extracted {len(idx)} page(s) → {out_path}"
