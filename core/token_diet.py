"""TokenDiet — pre-LLM text sanitizer for tool-call results.

Inspired by OpenHuman's TokenJuice idea: every tool result that's about to
go into the LLM context window gets normalized first. The compression is
mundane (HTML→text, URL shortening, dedup, whitespace collapse), but the
*discipline* of always running it is what saves tokens.

Wire-in points in ODIN:
  • AKASHA web_search / fetch_page  → results go to GIL after this
  • CHITRA drive_fetch              → file content before GIL ingests
  • HERMES vault_lookup             → cached notes before GIL ingests
  • THOTH session restore           → past turns before GIL re-sees them
  • SARASWATI deep_research         → cloud-research notes before vault store

Honest scope: this is a low-leverage, low-risk win. Don't expect 80 %
reductions on already-text inputs (chat replies, JSON responses). The big
wins are on HTML scrapes and noisy file dumps.
"""

import re
import unicodedata
from typing import Optional


# ── HTML → text ─────────────────────────────────────────────────────
# Conservative regex strip — drops tags, decodes a handful of entities,
# preserves block-level breaks. We don't pull in BeautifulSoup to avoid
# adding a hot-path dep for what's effectively a sanitizer pass.
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_RE_BR           = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_BLOCK_END    = re.compile(r"</(p|div|section|article|li|h[1-6]|tr)\s*>", re.IGNORECASE)
_RE_TAG          = re.compile(r"<[^>]+>")
_RE_ENTITIES = [
    (re.compile(r"&nbsp;"),  " "),
    (re.compile(r"&amp;"),   "&"),
    (re.compile(r"&lt;"),    "<"),
    (re.compile(r"&gt;"),    ">"),
    (re.compile(r"&quot;"),  '"'),
    (re.compile(r"&#39;"),   "'"),
    (re.compile(r"&apos;"),  "'"),
]


def looks_like_html(text: str) -> bool:
    """Cheap sniff — does this text plausibly contain HTML?"""
    if not text:
        return False
    sample = text[:2048].lower()
    return ("<html" in sample or "<body" in sample or "<div" in sample
            or "<p>" in sample or "<table" in sample or "</a>" in sample)


def html_to_text(text: str) -> str:
    """Strip HTML to plain text, preserving paragraph breaks."""
    if not text:
        return ""
    out = _RE_SCRIPT_STYLE.sub(" ", text)
    out = _RE_BR.sub("\n", out)
    out = _RE_BLOCK_END.sub("\n", out)
    out = _RE_TAG.sub(" ", out)
    for pat, repl in _RE_ENTITIES:
        out = pat.sub(repl, out)
    return out


# ── URL shortening ──────────────────────────────────────────────────
# Long URLs (especially with tracking params, session tokens, signed
# image URLs) are pure token waste — the LLM almost never needs them
# verbatim. We replace the query string with a placeholder and cap path
# length so the URL still reads as a URL but stays under ~60 chars.
_RE_URL = re.compile(r"https?://[^\s<>\"']+")
_URL_MAX_LEN = 60


def shorten_urls(text: str) -> str:
    def _short(m):
        url = m.group(0)
        if len(url) <= _URL_MAX_LEN:
            return url
        # Keep scheme + host + first path segment; signal truncation.
        cut = url.find("?")
        if cut > 0 and cut < _URL_MAX_LEN:
            return url[:cut] + "?…"
        return url[:_URL_MAX_LEN] + "…"
    return _RE_URL.sub(_short, text)


# ── Non-ASCII pruning ───────────────────────────────────────────────
# Strip noise like ZWSP, BOM, smart-quotes, decorative emoji from
# tool-result blobs. We KEEP plain Unicode letters (so non-English text
# still works) — we only drop control-class and format-class characters.
def strip_noise_chars(text: str) -> str:
    if not text:
        return ""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        # Cc=control, Cf=format, Co=private-use, Cs=surrogate
        if cat in ("Cc", "Cf", "Co", "Cs") and ch not in ("\n", "\t"):
            continue
        out.append(ch)
    return "".join(out)


# ── Whitespace + dedup ─────────────────────────────────────────────
_RE_MULTISPACE = re.compile(r"[ \t]{2,}")
_RE_MULTINEWLINE = re.compile(r"\n{3,}")


def collapse_whitespace(text: str) -> str:
    out = _RE_MULTISPACE.sub(" ", text)
    out = _RE_MULTINEWLINE.sub("\n\n", out)
    return out.strip()


def dedup_lines(text: str) -> str:
    """Drop adjacent duplicate lines. Common in HTML scrapes (repeated nav
    links, footer boilerplate that appears on every page section)."""
    seen_prev = None
    keep = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped == seen_prev:
            continue
        keep.append(line)
        if stripped:
            seen_prev = stripped
    return "\n".join(keep)


# ── Top-level entry point ──────────────────────────────────────────
def diet(text: str, *,
         html: Optional[bool] = None,
         max_chars: int = 0) -> str:
    """Run the full sanitizer chain.

    html       — force HTML mode on (True), off (False), or auto-detect (None)
    max_chars  — if > 0, truncate the cleaned result to this many chars and
                 append a marker. Callers usually want a real cap so the LLM
                 doesn't get a 200KB dump.
    """
    if not text:
        return ""
    is_html = html if html is not None else looks_like_html(text)
    out = html_to_text(text) if is_html else text
    out = shorten_urls(out)
    out = strip_noise_chars(out)
    out = collapse_whitespace(out)
    out = dedup_lines(out)
    if max_chars > 0 and len(out) > max_chars:
        out = out[:max_chars].rstrip() + f"\n… [truncated, was {len(out)} chars]"
    return out


def stats(original: str, cleaned: str) -> dict:
    """Diagnostic — what did the sanitizer actually save?"""
    return {
        "original_chars":  len(original),
        "cleaned_chars":   len(cleaned),
        "ratio":           round(len(cleaned) / max(1, len(original)), 3),
        "saved_chars":     len(original) - len(cleaned),
    }
