# MERCURY — Roman — messenger god, communication and speed
# Communication: emails, messages, notifications
#
# Direct messaging without LLM-per-step:
#   - WhatsApp: pywhatkit for phone-number sends (5-10s, deterministic).
#     For name-based contacts, falls back to ARGUS (slower but flexible).
#   - Email: real SMTP via send_email (was already here); draft_email is
#     the safer default — opens Gmail compose, user clicks Send.
#   - Instagram: hostile to automation; routes through ARGUS with caveat.
#   - Telegram outbound: via the NARADA bot token if configured.
# Pattern lifted from openclaw's "multi-channel" idea, built ODIN's way:
# one MERCURY module covers everything instead of 23 per-platform modules.

import json
import os
import re
import smtplib
import subprocess
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from core.marduk import OdinModule

try:
    import pywhatkit as _pwk
    _HAS_PYWHATKIT = True
except ImportError:
    _HAS_PYWHATKIT = False

try:
    # pyautogui ships with pywhatkit, but we use it directly for the native
    # WhatsApp Windows app (sending Enter / typing into the focused window).
    import pyautogui as _pag
    _pag.FAILSAFE = True   # move mouse to corner to abort
    _HAS_PYAUTOGUI = True
except ImportError:
    _HAS_PYAUTOGUI = False

try:
    # Windows UI Automation — reads the WhatsApp Desktop UI tree so we can
    # find the chat item whose visible text EXACTLY matches the contact name
    # (after stripping emojis), instead of trusting WhatsApp's substring
    # search + pyautogui Enter (which previously sent to "Sam (Alex)"
    # when the user asked for "Alex").
    import uiautomation as _uia
    _HAS_UIA = True
except ImportError:
    _HAS_UIA = False

# OCR fallback for WhatsApp's WebView2 chat list. Imports are LAZY (inside
# the function below) — last attempt at module-scope import hung ODIN's
# boot, likely a PIL/pygame conflict at startup. With lazy imports, boot
# is fast and OCR only loads when actually needed.
_HAS_OCR = None   # tri-state: None=unchecked, True=ok, False=failed


def _ensure_ocr_loaded() -> bool:
    """First-use OCR loader. Imports pytesseract + PIL ImageGrab only when
    a WhatsApp send actually needs OCR. Caches the result."""
    global _HAS_OCR, _tess, _ImageGrab, _Image
    if _HAS_OCR is not None:
        return _HAS_OCR
    try:
        import pytesseract as _tess_mod
        from PIL import ImageGrab as _ImageGrab_mod, Image as _Image_mod
        _tess = _tess_mod
        _ImageGrab = _ImageGrab_mod
        _Image = _Image_mod
        _HAS_OCR = True
    except Exception as e:
        print(f"[MERCURY] OCR not available ({e}); WhatsApp name-send relies on UIA only.")
        _HAS_OCR = False
    return _HAS_OCR

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


# Phone-vs-name distinguisher. Phones can be raw digits, +91…, (123) 456-7890,
# 123 456 7890, etc. We strip non-digit/plus chars and check if what remains
# is 7+ digits. Names are anything else.
_PHONE_RE = re.compile(r"^[\+\s\(\)\-\.\d]{7,20}$")


def _looks_like_phone(s: str) -> bool:
    if not s or not _PHONE_RE.match(s):
        return False
    digits = re.sub(r"\D", "", s)
    return 7 <= len(digits) <= 15


# Emoji + variation-selector + ZWJ ranges. Stripping these from contact names
# makes "Alex 💜" and "Alex" compare equal — which is what users want
# when they say "Alex" out loud.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"     # most pictographs
    "☀-➿"              # misc symbols / dingbats
    "⌀-⏿"              # misc technical (clocks, etc.)
    "⃐-⃿"              # combining marks
    "︀-️"              # variation selectors
    "‍"                     # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _strip_emojis(s: str) -> str:
    """Remove emojis + variation selectors. Collapse internal whitespace."""
    if not s:
        return ""
    no_emoji = _EMOJI_RE.sub("", s)
    return re.sub(r"\s+", " ", no_emoji).strip()


# Field separators inside a UIA Name. WhatsApp Desktop concatenates the
# contact name, last message preview, and timestamp into a single
# accessible name like "Alex🎀❤️, 11:34 am, At hotel". Splitting on these
# delimiters lets us pull out the FIRST field — the contact name — to
# match against. Includes em/en dashes which Windows uses in some labels.
_NAME_FIELD_SEP = re.compile(r"\s*[\n\r,;|·•—–-]\s*")


def _primary_field(clean_name: str) -> str:
    """Return the first delimited field of a UIA-collected name. If there
    are no delimiters, returns the whole string."""
    return _NAME_FIELD_SEP.split(clean_name, maxsplit=1)[0].strip()


def _rect_area(node) -> int:
    """Area of a UIA node's bounding rect (0 if unreadable). Larger area
    usually means an outer container element vs. inner text span."""
    try:
        r = node.BoundingRectangle
        if r:
            w = max(0, r.right - r.left)
            h = max(0, r.bottom - r.top)
            return w * h
    except Exception:
        pass
    return 0


def _find_exact_contact_in_whatsapp(name: str, timeout: float = 3.5,
                                     prefer_raw_name: str = "") -> tuple[str, object]:
    """Find the WhatsApp Desktop element whose visible text — AFTER stripping
    emojis — exactly equals `name` (case-insensitive). Returns one of:
        ("clicked",  clean_name)         — exact match found + clicked
        ("no_match", [partial_candidates]) — only substring-matches exist
        ("ui_error", reason_str)         — couldn't read the UI tree

    Key design choices forced by the screenshot WhatsApp showed:
    1. WhatsApp Desktop does NOT use ListItemControl — its chat rows are
       custom Button/Pane controls with the contact name in .Name. So we
       walk EVERY UIA node, not just ListItems.
    2. WhatsApp's search shows TWO sections under the same query: 'Chats'
       (the contact rows you want) AND 'Messages' (message-body previews
       containing the word). Both can produce exact-text matches for a
       common name like 'Alex'. We always prefer the TOPMOST exact
       match by BoundingRectangle Y — the Chats section sits above the
       Messages section, always.

    Safety: NEVER auto-types/sends. Only clicks the verified row."""
    if not _HAS_UIA:
        return ("ui_error", "uiautomation library missing")
    target = _strip_emojis(name).lower()
    if not target:
        return ("no_match", [])

    # Find WhatsApp window
    window = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        for root_child in _uia.GetRootControl().GetChildren():
            try:
                title = root_child.Name or ""
                if title and "whatsapp" in title.lower():
                    window = root_child
                    break
            except Exception:
                continue
        if window:
            break
        time.sleep(0.25)
    if window is None:
        return ("ui_error", "WhatsApp window not found in UI tree")

    try:
        window.SetActive()
    except Exception:
        pass

    # Walk EVERY node, collect (node, clean_name, raw_name, top_y).
    # CRITICAL: WhatsApp Desktop is built on WebView2 — the chat list lives
    # inside a DocumentControl[WebView] at depth ~22, and rows are 5-10
    # levels deeper. Depth 50 covers the entire tree comfortably.
    candidates: list[tuple[object, str, str, int]] = []
    seen_raw_names: set[str] = set()

    # Also collect text-pattern strings as a last-ditch fallback: UIA's
    # TextPattern can sometimes read text content even when the .Name
    # property is empty (common for WebView2 elements).
    def _node_text(child) -> str:
        """Best-effort text extraction. Tries .Name, then TextPattern, then
        LegacyIAccessiblePattern."""
        for attr in ("Name",):
            try:
                v = getattr(child, attr, None)
                if v: return str(v)
            except Exception:
                pass
        try:
            tp = child.GetTextPattern()
            if tp:
                r = tp.DocumentRange.GetText(200)
                if r: return r
        except Exception:
            pass
        try:
            la = child.GetLegacyIAccessiblePattern()
            if la:
                v = la.CurrentName
                if v: return v
        except Exception:
            pass
        return ""

    def _walk(node, depth=0):
        if depth > 50:
            return
        try:
            children = node.GetChildren()
        except Exception:
            return
        for child in children:
            try:
                # CRITICAL: skip EditControl (the search box). It echoes the
                # user-typed query into its Name, which then exact-matches
                # itself — the previous "Sam Alx" bug: user said "Sam
                # Alx" → search box's Name became "Sam Alx" → picker
                # exact-matched the search box → "clicked" it (no-op since
                # already focused) → message went to whatever chat was
                # already open in the right pane (Alex from prior turn).
                ctype = getattr(child, "ControlTypeName", "")
                if ctype in ("EditControl", "ComboBoxControl"):
                    _walk(child, depth + 1)
                    continue
                raw = _node_text(child).strip()
                if raw and raw not in seen_raw_names:
                    clean = _strip_emojis(raw)
                    if clean and any(c.isalpha() for c in clean):
                        try:
                            r = child.BoundingRectangle
                            y = int(r.top) if r else 999_999
                        except Exception:
                            y = 999_999
                        candidates.append((child, clean, raw, y))
                        seen_raw_names.add(raw)
                _walk(child, depth + 1)
            except Exception:
                continue

    _walk(window)

    if not candidates:
        _dump_uia_tree(window)
        return ("ui_error",
                "UIA tree had no named nodes — dumped tree to data/logs/whatsapp_uia.txt")

    # Favorite priority: if the caller passed a previously-favorited
    # raw_name for this contact, look for it FIRST. The favorite was set
    # after a successful send, so the raw_name should be a stable identifier
    # (matches the WhatsApp contact's accessible name, with emojis).
    if prefer_raw_name:
        fav_clean = _strip_emojis(prefer_raw_name).lower().strip()
        fav_matches = [c for c in candidates
                       if c[2] == prefer_raw_name              # exact raw match
                       or c[1].lower() == fav_clean]           # clean fallback
        if fav_matches:
            # Largest-area first, then topmost (same heuristic as main path).
            fav_matches.sort(key=lambda c: (-_rect_area(c[0]), c[3]))
            chosen = fav_matches[0]
            try:
                rect = chosen[0].BoundingRectangle
                if rect and rect.right > rect.left and rect.bottom > rect.top:
                    cx = (int(rect.left) + int(rect.right)) // 2
                    cy = (int(rect.top) + int(rect.bottom)) // 2
                    _pag.click(cx, cy)
                    return ("clicked",
                            f"{chosen[1]} (favorite-matched {prefer_raw_name!r})")
            except Exception:
                pass
            # Fall through to standard matching if favorite click failed

    # Two-pass match. Pass 1: whole cleaned name equals target.
    # Pass 2: PRIMARY FIELD (text before first comma/newline/dash) equals
    # target. WhatsApp Desktop's UIA labels often concatenate "contact,
    # last-message, timestamp" so the whole string is e.g.
    # "Alex, 11:34 am, At hotel" — pass 1 misses, pass 2 catches.
    # Among equal-name candidates: prefer LARGER bounding rectangle (entire
    # row > inner text span > tiny label), then topmost-Y as tiebreaker.
    # Click via pyautogui (real OS mouse click) not UIA's Click — text-only
    # spans don't accept InvokePattern but pixel clicks always transfer
    # focus, which is what we need for WhatsApp's chat list.
    def _pick(matches: list) -> tuple[str, object]:
        # Sort: largest area first, then topmost Y. Largest area picks the
        # outer chat-row container over inner text spans nested in it.
        matches.sort(key=lambda c: (-_rect_area(c[0]), c[3]))
        chosen = matches[0]
        try:
            rect = chosen[0].BoundingRectangle
            if not rect or rect.right <= rect.left or rect.bottom <= rect.top:
                return ("ui_error", "matched node has no clickable bounding rect")
            cx = (int(rect.left) + int(rect.right)) // 2
            cy = (int(rect.top) + int(rect.bottom)) // 2
            # Real OS mouse click — transfers focus to the chat, dismisses
            # the search dropdown, opens the conversation.
            _pag.click(cx, cy)
            note = ""
            if len(matches) > 1:
                note = (f" (chose largest of {len(matches)} matches at "
                        f"({cx},{cy}) — outer rows rank above inner spans)")
            return ("clicked", chosen[1] + note)
        except Exception as e:
            return ("ui_error", f"click failed: {e}")

    # Pass 1: whole-string equality
    exact_whole = [c for c in candidates if c[1].lower() == target]
    if exact_whole:
        return _pick(exact_whole)

    # Pass 2: primary-field equality
    exact_field = [c for c in candidates
                   if _primary_field(c[1]).lower() == target]
    if exact_field:
        return _pick(exact_field)

    # No exact match anywhere. Dump the UIA tree so we can debug what was
    # actually exposed by WhatsApp Desktop. Return shortest substring
    # candidates (whole-name OR primary-field) as suggestions.
    _dump_uia_tree(window)
    partial_set = set()
    for c in candidates:
        cl = c[1].lower()
        pf = _primary_field(c[1]).lower()
        if target in cl or target in pf:
            partial_set.add(c[1])
    partial = sorted(partial_set, key=lambda n: (len(n), n.lower()))[:5]
    return ("no_match", partial)


def _find_exact_contact_via_ocr(name: str) -> tuple[str, object]:
    """Fallback when UIA can't read the WebView2 DOM: screenshot the
    WhatsApp chat-list panel, tesseract-OCR it, find a row whose text
    exactly equals the target (after emoji stripping + primary-field
    extraction), click its pixel center.

    Returns the same status tuples as _find_exact_contact_in_whatsapp."""
    if not _ensure_ocr_loaded():
        return ("ui_error", "OCR not loadable (pytesseract/PIL import failed)")
    if not _HAS_UIA:
        return ("ui_error", "UIA library missing — can't get window bounds")
    target = _strip_emojis(name).lower().strip()
    if not target:
        return ("no_match", [])

    # Use UIA only to locate the WhatsApp window bounding rectangle.
    window = None
    for root_child in _uia.GetRootControl().GetChildren():
        try:
            if "whatsapp" in (root_child.Name or "").lower():
                window = root_child
                break
        except Exception:
            continue
    if not window:
        return ("ui_error", "WhatsApp window not found")
    try:
        r = window.BoundingRectangle
        win_left, win_top = int(r.left), int(r.top)
        win_right, win_bottom = int(r.right), int(r.bottom)
    except Exception:
        return ("ui_error", "couldn't read window bounds")

    # From the UIA dump: the chat-list panel sits roughly at
    # x=144..660, y=top+200..bottom (below the title bar + search input
    # + filter tabs). These coords are pixels relative to the desktop.
    # Add a small margin and clip to window bounds.
    x1 = max(win_left, win_left + 144)
    y1 = max(win_top,  win_top + 200)
    x2 = min(win_right,  win_left + 660)
    y2 = min(win_bottom, win_bottom)
    if x2 <= x1 or y2 <= y1:
        return ("ui_error", "chat-list region degenerate")

    try:
        img = _ImageGrab.grab(bbox=(x1, y1, x2, y2), all_screens=True)
        # Tesseract reads small UI text better when upscaled 2x.
        img = img.resize((img.width * 2, img.height * 2), _Image.LANCZOS)
    except Exception as e:
        return ("ui_error", f"screenshot failed: {e}")

    try:
        data = _tess.image_to_data(img, output_type=_tess.Output.DICT)
    except Exception as e:
        return ("ui_error", f"OCR failed: {e}")

    # Group OCR words into lines by their `line_num` field — tesseract
    # already groups same-row words. For each line, accumulate words and
    # find the y-center to click later.
    lines: dict[tuple[int, int], dict] = {}   # (block, line) → row info
    n = len(data["text"])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        key = (data["block_num"][i], data["line_num"][i])
        row = lines.setdefault(key, {"words": [], "tops": [], "lefts": [],
                                     "heights": [], "widths": []})
        row["words"].append(text)
        row["tops"].append(data["top"][i])
        row["lefts"].append(data["left"][i])
        row["heights"].append(data["height"][i])
        row["widths"].append(data["width"][i])

    # Build a sortable list of (y_top, clean_line, click_x_local, click_y_local)
    candidates = []
    for key, row in lines.items():
        line_text = " ".join(row["words"])
        clean = _strip_emojis(line_text).strip()
        if not clean or not any(c.isalpha() for c in clean):
            continue
        # Click point: middle-left area of the row (avoids accidentally
        # clicking the avatar or the right-side timestamp).
        cy = sum(row["tops"]) // len(row["tops"]) + max(row["heights"]) // 2
        # Click in the NAME column, ~80px from left edge of region (after 2x
        # upscale — divide by 2 to map back).
        click_x_local = 160          # 80 * 2 (upscaled coords)
        click_y_local = cy
        candidates.append((click_y_local, clean, click_x_local))

    if not candidates:
        return ("ui_error", "OCR found no text rows in chat-list region")

    # Pass 1: exact match of the WHOLE line (after emoji strip).
    exact_whole = [c for c in candidates if c[1].lower() == target]
    # Pass 2: primary-field match (split on common separators).
    exact_field = [c for c in candidates
                   if _primary_field(c[1]).lower() == target]
    chosen_list = exact_whole or exact_field

    if chosen_list:
        # Topmost wins — chat rows above message-preview rows.
        chosen_list.sort(key=lambda c: c[0])
        cy_local, clean, cx_local = chosen_list[0]
        # Map upscaled-image coords back to screen coords.
        screen_x = x1 + cx_local // 2
        screen_y = y1 + cy_local // 2
        try:
            _pag.moveTo(screen_x, screen_y, duration=0.05)
            _pag.click(screen_x, screen_y)
            note = ""
            if len(chosen_list) > 1:
                note = f" (chose topmost of {len(chosen_list)} OCR matches)"
            return ("clicked", f"{clean}{note}  [OCR]")
        except Exception as e:
            return ("ui_error", f"OCR pixel click failed: {e}")

    # No exact match — return suggestions (shortest primary-field matches).
    partial = sorted(
        {c[1] for c in candidates
         if target in c[1].lower() or target in _primary_field(c[1]).lower()},
        key=lambda n: (len(n), n.lower())
    )[:5]
    return ("no_match", partial)


def _dump_uia_tree(window, path: str = "data/logs/whatsapp_uia.txt") -> None:
    """Write the WhatsApp UIA tree to a debug file so we can diagnose why
    the picker missed. Only runs when the picker finds zero candidates.
    Non-fatal — silently swallows errors."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        return
    lines: list[str] = []

    def _walk(node, depth=0):
        if depth > 60:
            return
        try:
            raw = (node.Name or "")[:120].replace("\n", " ")
            ct = getattr(node, "ControlTypeName", "?")
            cls = (getattr(node, "ClassName", "") or "")[:50]
            try:
                r = node.BoundingRectangle
                rect = f"({r.left},{r.top}|{r.right - r.left}x{r.bottom - r.top})" if r else ""
            except Exception:
                rect = ""
            lines.append(f"{'  ' * depth}{ct}[{cls}] {rect} name={raw!r}")
            for child in node.GetChildren():
                _walk(child, depth + 1)
        except Exception:
            return

    _walk(window)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("WhatsApp UIA tree dump — generated when contact picker found nothing\n")
            f.write("=" * 80 + "\n")
            f.write("\n".join(lines))
    except Exception:
        pass


class Mercury(OdinModule):
    MODULE_NAME = "MERCURY"
    LAYER = "OUTPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("mercury", {})
        self.smtp_server = cfg.get("smtp_server", "smtp.gmail.com")
        self.smtp_port = cfg.get("smtp_port", 587)
        # Secrets prefer env vars over config so config.yaml stays committable.
        # Spaces in Google app passwords are display-only — strip them.
        self.sender_email = (cfg.get("sender_email")
                             or os.environ.get("GMAIL_SENDER", "")).strip()
        raw_pw = (cfg.get("sender_password")
                  or os.environ.get("GMAIL_APP_PASSWORD", ""))
        self.sender_password = raw_pw.replace(" ", "").strip()
        # Last successful send — used by 'send the same message to X' /
        # 'forward to Y' / 'do same on Z'. Also captures the EXACT raw name
        # of the matched contact (with emojis) so 'favorite that' can
        # promote it to favorites with a stable identifier.
        self._last_sent: dict = {
            "platform": "",   # 'whatsapp' / 'telegram' / 'instagram' / 'email'
            "to": "",         # what the user said (e.g. "Alex")
            "raw_name": "",   # what ODIN actually clicked (e.g. "Alex🎀❤️")
            "message": "",
            "subject": "",
        }
        # Aliases: explicit user-named shortcuts ("bestie" → "Alex").
        self.aliases_path = cfg.get(
            "aliases_path", "data/knowledge/mercury_aliases.json"
        )
        self._aliases: dict = self._load_aliases()
        # Favorites: auto-tracked DISAMBIGUATION memory. Keyed by canonical
        # name (lowercase, emoji-stripped). Stores the raw_name of the
        # specific chat row the user prefers when there are multiple
        # matches. Set via 'favorite that contact' after a successful send.
        # Different concept from aliases — aliases are explicit nicknames
        # the user invents; favorites are "when I say X I mean THIS X,
        # not the other X". Together they form a layered routing system:
        # alias → favorite → exact-match → topmost-largest.
        self.favorites_path = cfg.get(
            "favorites_path", "data/knowledge/mercury_favorites.json"
        )
        self._favorites: dict = self._load_favorites()
        # Send log — bounded history for "what did I send to X" / forensics.
        self.send_log_path = cfg.get(
            "send_log_path", "data/knowledge/mercury_send_log.jsonl"
        )
        self.send_log_cap = int(cfg.get("send_log_cap", 200))

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "send_email",
                "description": "Send an email to someone",
                "parameters": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body"}
                },
                "required": ["to", "subject", "body"]
            },
            {
                "name": "open_gmail",
                "description": "Open Gmail in the browser",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "gmail_ingest_recent",
                "description": (
                    "Fetch recent Gmail messages via IMAP, summarize each via "
                    "SARASWATI, write to vault at Brain/ODIN/inbox/. Uses the "
                    "same Gmail credentials configured for send_email (sender_email + "
                    "GMAIL_APP_PASSWORD). Called by SELENE on its 20-min loop; "
                    "can also be invoked on demand for an immediate inbox catch-up."
                ),
                "parameters": {
                    "days":  {"type": "integer", "description": "Look-back window (default 1)"},
                    "limit": {"type": "integer", "description": "Max messages per pass (default 8, hard cap 25)"},
                    "unread_only": {"type": "boolean", "description": "Only ingest unread mail (default true). When false, fetches recent regardless of read state — useful for first-run catch-up."},
                },
                "required": [],
                "internal_only": True,
            },
            {
                "name": "open_whatsapp",
                "description": "Open WhatsApp Web in the browser",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "draft_email",
                "description": "Open Gmail's compose window in the browser with recipient, subject, and body pre-filled. The user reviews and clicks Send manually. PREFER this over send_email — it requires no SMTP setup, works immediately, and gives the user a chance to fix anything before it actually sends.",
                "parameters": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject (default 'From ODIN')"},
                    "body": {"type": "string", "description": "Email body"}
                },
                "required": ["to", "body"]
            },
            {
                "name": "whatsapp_send",
                "description": (
                    "Send a WhatsApp message using the NATIVE Windows app "
                    "(Microsoft Store WhatsApp), NOT WhatsApp Web. Two paths: "
                    "if 'contact' is a phone number, opens the app via "
                    "whatsapp:// URL scheme with the message pre-filled and "
                    "auto-presses Enter (~4s). If 'contact' is a name, opens "
                    "the app, presses Ctrl+N for new chat, types the name, "
                    "selects first match, types message, sends (~6s). The user "
                    "must already be signed in to the native WhatsApp app."
                ),
                "parameters": {
                    "contact": {"type": "string", "description": "Phone number (with country code) or contact name"},
                    "message": {"type": "string", "description": "Message text to send"},
                },
                "required": ["contact", "message"],
            },
            {
                "name": "telegram_send",
                "description": (
                    "Send a Telegram message via the NARADA bot. Reuses "
                    "narada.telegram_bot_token. Recipient is a chat_id or "
                    "@username (must have started a chat with the bot first)."
                ),
                "parameters": {
                    "chat": {"type": "string", "description": "Telegram chat_id (number) or @username"},
                    "message": {"type": "string", "description": "Message text"},
                },
                "required": ["chat", "message"],
            },
            {
                "name": "set_contact_alias",
                "description": (
                    "Save a contact under a short alias so the user can refer "
                    "to them by nickname. e.g. 'save Alex as bestie on "
                    "WhatsApp' → alias 'bestie' resolves to 'Alex' on "
                    "WhatsApp. Aliases are persistent across restarts. "
                    "Common aliases ('this', 'fav', 'favorite') work for "
                    "'msg this' / 'WhatsApp fav saying X' style commands."
                ),
                "parameters": {
                    "alias":    {"type": "string", "description": "Short nickname (e.g. 'bestie', 'mom', 'fav')"},
                    "contact":  {"type": "string", "description": "Actual contact (phone, name, handle, email)"},
                    "platform": {"type": "string", "description": "'whatsapp' / 'telegram' / 'instagram' / 'email' (default 'whatsapp')"},
                },
                "required": ["alias", "contact"],
            },
            {
                "name": "remove_contact_alias",
                "description": "Remove a saved contact alias.",
                "parameters": {
                    "alias": {"type": "string", "description": "Alias to remove"},
                },
                "required": ["alias"],
            },
            {
                "name": "list_contact_aliases",
                "description": "List all saved contact aliases.",
                "parameters": {},
                "required": [],
            },
            {
                "name": "favorite_last",
                "description": (
                    "Promote the LAST successfully-messaged contact to favorites. "
                    "Stores the exact chat-row identifier ODIN clicked, so future "
                    "ambiguous matches with the same name (e.g. multiple 'Alex's) "
                    "resolve to this specific one. Use immediately after a "
                    "successful send: 'favorite that contact' / 'save that as "
                    "favorite' / 'favorite this person'."
                ),
                "parameters": {
                    "name": {"type": "string", "description": "Optional override of the canonical name (defaults to what was just used)"},
                },
                "required": [],
            },
            {
                "name": "unfavorite",
                "description": "Remove a favorited contact so future matches use default disambiguation.",
                "parameters": {
                    "name":     {"type": "string", "description": "Canonical name (e.g. 'Alex')"},
                    "platform": {"type": "string", "description": "Platform (default 'whatsapp')"},
                },
                "required": ["name"],
            },
            {
                "name": "list_favorites",
                "description": "Show all favorited contacts (auto-disambiguation memory). Separate from contact aliases.",
                "parameters": {},
                "required": [],
            },
            {
                "name": "send_history",
                "description": "Show the most recent messages MERCURY sent (last N entries). Useful for 'what did I send to X' / forensics.",
                "parameters": {
                    "limit": {"type": "integer", "description": "How many recent sends to show (default 10, max 50)"},
                },
                "required": [],
            },
            {
                "name": "resend_last",
                "description": (
                    "Re-send the LAST message MERCURY sent (within this "
                    "session) to a new recipient — possibly via a different "
                    "platform. Use for 'send the same message to X' / 'forward "
                    "to X' / 'do the same for X on Y'. If platform is omitted, "
                    "uses the same platform as the original send. If no "
                    "previous send exists in this session, returns an error."
                ),
                "parameters": {
                    "to":       {"type": "string", "description": "New recipient (name, phone, handle, or email)"},
                    "platform": {"type": "string", "description": "Optional: 'whatsapp' / 'telegram' / 'instagram' / 'email' (default: same as original)"},
                },
                "required": ["to"],
            },
            {
                "name": "instagram_send",
                "description": (
                    "Send an Instagram DM. Honest caveat: the 'Instagram "
                    "Windows app' from Microsoft Store is a PWA wrapping the "
                    "same web view as instagram.com, and Instagram exposes NO "
                    "native URL scheme for direct messages. Two paths: default "
                    "uses ARGUS browser automation for full automated send "
                    "(slower, ~50% success rate due to Instagram anti-bot). "
                    "open_in_app=true opens the native app on the user's "
                    "profile and copies the message to clipboard — user clicks "
                    "Message and pastes (manual but reliable)."
                ),
                "parameters": {
                    "handle":      {"type": "string", "description": "Instagram handle (without @)"},
                    "message":     {"type": "string", "description": "Message text"},
                    "open_in_app": {"type": "boolean", "description": "Open native app + copy message to clipboard for manual paste (default: false → full ARGUS auto-send)"},
                },
                "required": ["handle", "message"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "send_email":     self._send_email,
            "open_gmail":     self._open_gmail,
            "open_whatsapp":  self._open_whatsapp,
            "draft_email":    self._draft_email,
            "whatsapp_send":  self._whatsapp_send,
            "telegram_send":  self._telegram_send,
            "instagram_send": self._instagram_send,
            "resend_last":         self._resend_last,
            "set_contact_alias":   self._set_contact_alias,
            "remove_contact_alias": self._remove_contact_alias,
            "list_contact_aliases": self._list_contact_aliases,
            "favorite_last":       self._favorite_last,
            "unfavorite":          self._unfavorite,
            "list_favorites":      self._list_favorites,
            "send_history":        self._send_history,
            "gmail_ingest_recent": self._gmail_ingest_recent,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[MERCURY] Error: {e}"
        return f"[MERCURY] Unknown skill: {skill_name}"

    def _send_email(self, to: str = "", subject: str = "", body: str = "") -> str:
        if not self.sender_email or not self.sender_password:
            return "Email not configured. Set sender_email and sender_password in config.yaml."
        msg = MIMEMultipart()
        msg["From"] = self.sender_email
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
            server.starttls()
            server.login(self.sender_email, self.sender_password)
            server.sendmail(self.sender_email, to, msg.as_string())
        self._record_send("email", to, body, subject=subject)
        return f"Email sent to {to}."

    def _open_gmail(self) -> str:
        webbrowser.open("https://mail.google.com")
        return "Opening Gmail."

    def _open_whatsapp(self) -> str:
        webbrowser.open("https://web.whatsapp.com")
        return "Opening WhatsApp Web."

    def _draft_email(self, to: str = "", subject: str = "", body: str = "") -> str:
        # Opens Gmail compose with the URL query params Google supports.
        # Works without ANY SMTP setup — the user clicks Send themselves
        # after reviewing. Safer for AI-generated drafts.
        to = (to or "").strip()
        if not to or "@" not in to:
            return "Need a recipient email address with an @."
        subject = subject.strip() or "From ODIN"
        url = (
            "https://mail.google.com/mail/u/0/?fs=1&tf=cm"
            f"&to={quote(to)}&su={quote(subject)}&body={quote(body or '')}"
        )
        webbrowser.open(url)
        self._record_send("email", to, body or "", subject=subject)
        return f"Opened a draft email to {to}. Review and click Send when ready."

    # ── Favorites (disambiguation memory) ────────────────────────────
    def _favorite_last(self, name: str = "") -> str:
        """Promote the last sent recipient to favorites for disambiguation."""
        last = self._last_sent
        if not last.get("to"):
            return ("No previous send to favorite. Send a message first, then "
                    "say 'favorite that contact'.")
        platform = last["platform"]
        # Canonical name: use the user-supplied override, else what the user
        # actually said last time (the 'to' field), emoji-stripped + lower.
        canon_raw = (name or last["to"]).strip()
        canon = re.sub(r"\s+", " ", canon_raw).lower().strip()
        if not canon:
            return "Need a name to favorite under."
        raw_name = last.get("raw_name", "") or last["to"]
        self._favorites.setdefault(canon, {})[platform] = {
            "raw_name": raw_name,
            "favorited_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_favorites()
        return (f"Got it. When you say '{canon_raw}' on {platform}, I'll "
                f"send to '{raw_name}' from now on.")

    def _unfavorite(self, name: str = "", platform: str = "") -> str:
        canon = re.sub(r"\s+", " ", (name or "").strip().lower())
        if not canon:
            return "Need a name to unfavorite."
        platform = (platform or "whatsapp").lower()
        bucket = self._favorites.get(canon, {})
        if platform in bucket:
            removed = bucket.pop(platform)
            if not bucket:
                self._favorites.pop(canon, None)
            self._save_favorites()
            return f"Removed favorite '{canon}' on {platform} (was '{removed['raw_name']}')."
        return f"No favorite for '{canon}' on {platform}."

    def _list_favorites(self) -> str:
        if not self._favorites:
            return ("No favorited contacts yet. After a successful send, say "
                    "'favorite that contact' to remember which X you meant.")
        lines = []
        for canon, by_platform in self._favorites.items():
            for plat, info in by_platform.items():
                lines.append(f"'{canon}' on {plat} → '{info['raw_name']}'")
        return "Favorites: " + "; ".join(lines)

    def _lookup_favorite(self, target_clean: str, platform: str) -> str:
        """Returns the raw_name of the favorited contact for this target,
        or empty string if none. Used by the UIA picker for disambiguation.

        TWO-PASS lookup:
        1. Exact canonical match (target == favorite_key).
        2. FUZZY match against favorite keys (SequenceMatcher ratio ≥ 0.75)
           — so 'Karthik' matches a favorite stored under 'Kartik', and
           'Alx' matches one stored under 'Alex'. Picks the highest-
           scoring favorite as long as it cleared the threshold."""
        canon = re.sub(r"\s+", " ", (target_clean or "").strip().lower())
        if not canon:
            return ""
        # Pass 1: exact.
        entry = self._favorites.get(canon, {}).get(platform)
        if entry:
            return entry.get("raw_name", "")
        # Pass 2: fuzzy against the WHOLE favorite key. Only considers
        # entries on the same platform.
        import difflib
        best_ratio = 0.0
        best_raw = ""
        for fav_key, by_platform in self._favorites.items():
            plat_entry = by_platform.get(platform)
            if not plat_entry:
                continue
            ratio = difflib.SequenceMatcher(None, canon, fav_key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_raw = plat_entry.get("raw_name", "")
        # 0.75 ≈ allows 1-2 char differences for short names (Karthik↔Kartik).
        if best_ratio >= 0.75:
            return best_raw
        # Pass 3: fuzzy against INDIVIDUAL WORDS of favorite keys. Handles
        # the case where a favorite was saved with extra heard-mishear
        # tokens prepended (e.g. 'car thick kartik') and the user later
        # says just the clean name ('Kartik'). Tighter threshold here
        # since word-level matches are more easily incidental.
        for fav_key, by_platform in self._favorites.items():
            plat_entry = by_platform.get(platform)
            if not plat_entry:
                continue
            for word in fav_key.split():
                if len(word) < 3:
                    continue
                wratio = difflib.SequenceMatcher(None, canon, word).ratio()
                if wratio >= 0.85:
                    return plat_entry.get("raw_name", "")
        return ""

    def _load_favorites(self) -> dict:
        if os.path.exists(self.favorites_path):
            try:
                with open(self.favorites_path) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _save_favorites(self):
        try:
            os.makedirs(os.path.dirname(self.favorites_path), exist_ok=True)
            with open(self.favorites_path, "w") as f:
                json.dump(self._favorites, f, indent=2)
        except OSError:
            pass

    # ── Send history (forensics) ─────────────────────────────────────
    def _send_history(self, limit: int = 10) -> str:
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 10
        if not os.path.exists(self.send_log_path):
            return "No send history yet."
        try:
            with open(self.send_log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
        except OSError:
            return "Couldn't read send log."
        if not lines:
            return "No sends in the log."
        out = []
        for line in lines:
            try:
                e = json.loads(line.strip())
                ts = e.get("ts", "")[:19].replace("T", " ")
                msg = (e.get("message", "") or "")[:60]
                out.append(f"  {ts}  [{e.get('platform','?')}]  → {e.get('to','?')}: {msg}")
            except json.JSONDecodeError:
                continue
        return "Recent sends:\n" + "\n".join(out)

    def _append_send_log(self, entry: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.send_log_path), exist_ok=True)
            with open(self.send_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            # Trim log if it exceeds cap (cheap: read all, keep tail)
            with open(self.send_log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > self.send_log_cap:
                with open(self.send_log_path, "w", encoding="utf-8") as f:
                    f.writelines(lines[-self.send_log_cap:])
        except OSError:
            pass

    # ── Contact aliases ──────────────────────────────────────────────
    def _set_contact_alias(self, alias: str = "", contact: str = "",
                           platform: str = "") -> str:
        alias = (alias or "").strip().lower()
        contact = (contact or "").strip()
        platform = (platform or "whatsapp").strip().lower()
        if not alias or not contact:
            return "Need both an alias and a contact."
        if platform in ("insta",): platform = "instagram"
        if platform in ("wa",):    platform = "whatsapp"
        if platform not in ("whatsapp", "telegram", "instagram", "email"):
            return f"Unknown platform '{platform}'. Use whatsapp/telegram/instagram/email."
        self._aliases[alias] = {"contact": contact, "platform": platform}
        self._save_aliases()
        return f"Got it. '{alias}' = '{contact}' on {platform}."

    def _remove_contact_alias(self, alias: str = "") -> str:
        alias = (alias or "").strip().lower()
        if alias in self._aliases:
            entry = self._aliases.pop(alias)
            self._save_aliases()
            return f"Removed alias '{alias}' (was '{entry['contact']}' on {entry['platform']})."
        return f"No alias named '{alias}'."

    def _list_contact_aliases(self) -> str:
        if not self._aliases:
            return "No contact aliases saved."
        pairs = "; ".join(
            f"'{a}' → '{e['contact']}' on {e['platform']}"
            for a, e in self._aliases.items()
        )
        return f"Contact aliases: {pairs}"

    def _resolve_alias(self, contact: str, platform: str) -> str:
        """If `contact` matches a saved alias for the right platform, expand
        it. Otherwise return `contact` unchanged. Case-insensitive lookup."""
        if not contact:
            return contact
        entry = self._aliases.get(contact.strip().lower())
        if not entry:
            return contact
        # Only expand if the alias was saved for this platform OR for
        # platform-agnostic ("any") — currently we always tag a platform,
        # so the same alias on different platforms needs distinct names.
        if entry.get("platform") == platform:
            return entry["contact"]
        return contact

    def _load_aliases(self) -> dict:
        if os.path.exists(self.aliases_path):
            try:
                with open(self.aliases_path) as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError):
                return {}
        return {}

    def _save_aliases(self):
        try:
            os.makedirs(os.path.dirname(self.aliases_path), exist_ok=True)
            with open(self.aliases_path, "w") as f:
                json.dump(self._aliases, f, indent=2)
        except OSError:
            pass

    # ── Send-state cache + resend_last ───────────────────────────────
    def _record_send(self, platform: str, to: str, message: str,
                     subject: str = "", raw_name: str = "") -> None:
        """Cache the most recent successful send + append to send log.
        raw_name is the EXACT chat-row identifier ODIN matched (with emojis
        etc.) — used later by 'favorite that' to lock in disambiguation."""
        self._last_sent = {
            "platform": platform, "to": to,
            "raw_name": raw_name or to,
            "message": message, "subject": subject,
        }
        self._append_send_log({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "platform": platform, "to": to,
            "raw_name": raw_name or to,
            "message": message[:500],
            "subject": subject,
        })

    def _resend_last(self, to: str = "", platform: str = "") -> str:
        to = (to or "").strip()
        if not to:
            return "Need a new recipient for the resend."
        last = self._last_sent
        if not last.get("message"):
            return ("No previous message in this session to resend. Issue an "
                    "explicit 'send … saying …' first, then 'send the same to X'.")
        platform = (platform or last["platform"] or "").strip().lower()
        msg = last["message"]
        subj = last.get("subject", "") or "From ODIN"
        # Dispatch via the same skills the LLM/fast-routes use — guarantees
        # platform-specific logic (pywhatkit / UIA / Bot API / SMTP) runs.
        if platform in ("whatsapp", "wa"):
            return self._whatsapp_send(to, msg)
        if platform == "telegram":
            return self._telegram_send(to, msg)
        if platform in ("instagram", "insta"):
            return self._instagram_send(to.lstrip("@"), msg)
        if platform == "email":
            if "@" not in to:
                return "Email resend needs a full address."
            return self._draft_email(to, subj, msg)
        return (f"Don't know how to resend on platform '{platform}'. "
                f"Last send was via {last['platform']} — say 'send the same on "
                f"{last['platform']} to X' to reuse that path.")

    # ── WhatsApp — NATIVE APP FIRST ──────────────────────────────────
    # The user's machine has the Microsoft Store WhatsApp app installed.
    # We prefer it over WhatsApp Web because:
    #   1. They asked for it: "send wa msg from app available on system".
    #   2. The native app is already signed in via QR scan — no per-session
    #      browser login dance.
    #   3. Faster — no Chrome launch.
    # Path A (phone): whatsapp:// URL scheme pre-fills the message; pyautogui
    #                 presses Enter to send.
    # Path B (name):  open native app, Ctrl+N to focus search, type the name,
    #                 Enter to pick first match, type message, Enter to send.
    # If pyautogui / native app fails, fall back to ARGUS web automation.
    def _whatsapp_send(self, contact: str = "", message: str = "") -> str:
        contact = (contact or "").strip()
        message = (message or "").strip()
        if not contact:
            return "Need a contact (phone number with country code, or name)."
        if not message:
            return "Need a message to send."
        # Resolve aliases — "bestie" → "Alex", "fav" → "+919876543210", etc.
        contact = self._resolve_alias(contact, "whatsapp")
        if _looks_like_phone(contact):
            return self._whatsapp_native_phone(contact, message)
        return self._whatsapp_native_name(contact, message)

    def _whatsapp_native_phone(self, phone: str, message: str) -> str:
        """Path A — phone via whatsapp:// URL scheme + pyautogui Enter.
        Opens the native Windows app (NOT WhatsApp Web), pre-fills the text,
        sends. ~3-5s end-to-end. No browser involved."""
        digits = re.sub(r"\D", "", phone)
        if not phone.startswith("+") and len(digits) == 10:
            digits = "91" + digits   # India default — match user's locale
        # whatsapp:// URL scheme is registered by the Microsoft Store app.
        # `start` shells out via ShellExecute → URL handler → native app.
        url = f"whatsapp://send?phone={digits}&text={quote(message)}"
        try:
            # subprocess.Popen with shell=True so 'start' is parsed by cmd.
            subprocess.Popen(f'start "" "{url}"', shell=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return self._whatsapp_web_fallback_phone(phone, message,
                hint=f"native app launch failed: {e}")
        # Native app needs time to open, find the contact, render the chat.
        # 4s is comfortable on most machines; bump if your laptop is slower.
        time.sleep(4.0)
        if not _HAS_PYAUTOGUI:
            return (f"Opened native WhatsApp with message pre-filled for +{digits}. "
                    f"Press Enter to send. (Install pyautogui to auto-send.)")
        try:
            _pag.press("enter")
            self._record_send("whatsapp", "+" + digits, message, raw_name="+" + digits)
            return f"WhatsApp sent to +{digits} via native app."
        except Exception as e:
            return (f"Opened WhatsApp with message pre-filled for +{digits}, "
                    f"but couldn't auto-press Enter: {e}. Press it yourself.")

    def _whatsapp_native_name(self, name: str, message: str) -> str:
        """Path B — name via native app + UIA-verified exact match.

        Flow:
          1. Open WhatsApp Desktop (whatsapp:// URL scheme).
          2. Ctrl+N to focus the search input.
          3. Type the contact name.
          4. UIA reads the chat-list items, strips emojis, finds EXACT match.
          5. If unique exact match → UIA clicks it, type message, Enter.
          6. If ambiguous / no match → ABORT and report what was found.

        Critical safety: we NEVER press Enter to send unless UIA confirmed an
        exact text match. The previous version pressed Enter on whatever
        WhatsApp ranked first — that's how 'Alex' sent to 'Sam (Alex)'."""
        if not _HAS_PYAUTOGUI:
            return self._whatsapp_argus_fallback(name, message,
                hint="pyautogui not installed; pip install pyautogui.")

        # Open native app
        try:
            subprocess.Popen('start "" "whatsapp://"', shell=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return self._whatsapp_argus_fallback(name, message,
                hint=f"native app launch failed: {e}")
        time.sleep(3.5)   # let the app come to focus

        try:
            _pag.hotkey("ctrl", "n")     # focus search box
            time.sleep(0.6)
            # Type the name without emojis (in case the user said something
            # like 'mom heart emoji' — strip just in case).
            search_text = _strip_emojis(name) or name
            _pag.write(search_text, interval=0.04)
            time.sleep(1.4)              # let results populate
        except Exception as e:
            return self._whatsapp_argus_fallback(name, message,
                hint=f"keyboard automation failed: {e}")

        # UIA exact-match pick — favorites consulted first for disambiguation.
        # If multiple chats match a name, the favorited raw_name wins.
        fav_raw = self._lookup_favorite(name, "whatsapp")
        status, info = _find_exact_contact_in_whatsapp(
            name, timeout=3.0, prefer_raw_name=fav_raw
        )
        if status != "clicked":
            ocr_status, ocr_info = _find_exact_contact_via_ocr(name)
            if ocr_status == "clicked":
                status, info = ocr_status, ocr_info
            elif ocr_status == "no_match" and status == "ui_error":
                # OCR ran but didn't find — promote that diagnostic up.
                status, info = "no_match", ocr_info

        if status == "clicked":
            # Contact opened. Belt-and-braces: explicitly click the message-
            # input area BEFORE typing. Without this, if the row click didn't
            # transfer focus from the search box, the message gets typed into
            # the search bar instead (the 'alexhello this is Odin...' bug).
            time.sleep(1.0)              # let the chat panel render
            try:
                self._focus_whatsapp_message_input()
                time.sleep(0.3)
                _pag.write(message, interval=0.025)
                time.sleep(0.3)
                _pag.press("enter")
                # info is "RawName" or "RawName (chose ... matches)".
                # Strip the parenthetical note to get the raw name for
                # send-log + future favoriting.
                matched_raw = str(info).split(" (")[0].split("  [OCR]")[0]
                self._record_send("whatsapp", name, message, raw_name=matched_raw)
                return f"WhatsApp sent to '{info}' via native app."
            except Exception as e:
                return f"Found '{info}' but couldn't type the message: {e}. Type and send manually."

        if status == "no_match":
            partial = info or []
            if partial:
                hint = " Closest names containing your text: " + ", ".join(f"'{p}'" for p in partial) + "."
            else:
                hint = ""
            return (f"No contact named EXACTLY '{name}' in WhatsApp.{hint} "
                    f"Message NOT sent — re-issue with the exact name as it "
                    f"appears in your contacts.")

        # status == "ui_error" — couldn't read the WhatsApp UI tree.
        # Safest move: don't press Enter. Tell user the message is sitting in
        # the search box; they can manually pick the contact and send.
        reason = info if isinstance(info, str) else "unknown"
        return (f"Typed '{search_text}' into WhatsApp search but couldn't "
                f"verify the contact via UI automation ({reason}). Pick the "
                f"right chat manually and press Enter. Message text: '{message}'")

    def _whatsapp_web_fallback_phone(self, phone: str, message: str, hint: str = "") -> str:
        """Last resort: WhatsApp Web via pywhatkit. Only used if the native
        URL scheme didn't launch (native app missing / unregistered)."""
        if not _HAS_PYWHATKIT:
            return (f"Native WhatsApp send failed and pywhatkit isn't installed. {hint}")
        digits = re.sub(r"\D", "", phone)
        if not phone.startswith("+") and len(digits) == 10:
            digits = "91" + digits
        try:
            _pwk.sendwhatmsg_instantly(
                "+" + digits, message,
                wait_time=15, tab_close=True, close_time=3,
            )
            self._record_send("whatsapp", "+" + digits, message, raw_name="+" + digits)
            return f"WhatsApp sent to +{digits} via web fallback. ({hint})"
        except Exception as e:
            return f"WhatsApp send failed: {e}. {hint}"

    def _focus_whatsapp_message_input(self) -> None:
        """Click on the WhatsApp message-input area to guarantee focus is
        there before we type the message. Without this step, if the row-
        click didn't transfer focus, the message goes into the search bar.

        Strategy: find the WhatsApp window via UIA, compute the right-pane
        bottom-center (where the message input lives), click that pixel."""
        if not _HAS_UIA or not _HAS_PYAUTOGUI:
            return
        try:
            window = None
            for root_child in _uia.GetRootControl().GetChildren():
                if "whatsapp" in ((root_child.Name or "").lower()):
                    window = root_child
                    break
            if not window:
                return
            r = window.BoundingRectangle
            if not r:
                return
            # Right pane begins at window-left + ~666 (per UIA dump).
            # Message input sits at the bottom of that pane.
            right_pane_left = int(r.left) + 666
            right_pane_right = int(r.right)
            msg_x = (right_pane_left + right_pane_right) // 2
            msg_y = int(r.bottom) - 60       # ~60px above bottom edge
            # If window is narrow / no right pane, fall back to the center
            # of whatever's right of the chat list.
            if msg_x <= right_pane_left or msg_x >= int(r.right):
                msg_x = (int(r.left) + int(r.right)) // 2
            _pag.click(msg_x, msg_y)
        except Exception:
            pass   # Best-effort; if it fails, typing-into-search is the fallback bug

    def _whatsapp_argus_fallback(self, name: str, message: str, hint: str = "") -> str:
        """ARGUS browser automation as the absolute fallback for names.
        Slow (30-60s) and LLM-driven. Only used if native-app pyautogui
        couldn't run — usually means pyautogui missing or display blocked."""
        if not self.marduk:
            return f"Native WhatsApp failed and ARGUS not reachable. {hint}"
        argus = self.marduk.get_module("ARGUS")
        if not argus or not getattr(argus, "enabled", False):
            return (f"Native WhatsApp failed and ARGUS not online. {hint} "
                    f"Install/configure browser-use, or pass a phone number.")
        task = (
            f"Open https://web.whatsapp.com in Chrome. Search the contact "
            f"{name!r}. Click the first matching chat. Type EXACTLY: \"{message}\". "
            f"Press Enter to send. Report when delivered. confirmed."
        )
        return argus.execute("browse", {"task": task})

    # ── Telegram via NARADA bot ──────────────────────────────────────
    def _telegram_send(self, chat: str = "", message: str = "") -> str:
        chat = (chat or "").strip()
        message = (message or "").strip()
        if not chat or not message:
            return "Need both chat (id or @username) and message."
        chat = self._resolve_alias(chat, "telegram")
        if not _HAS_REQUESTS:
            return "requests library missing — can't reach Telegram API."
        narada_cfg = self.config.get("narada", {}) or {}
        token = (narada_cfg.get("telegram_bot_token")
                 or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
        if not token:
            return ("Telegram outbound needs a bot token. Set "
                    "narada.telegram_bot_token in config.yaml (same token "
                    "used by the NARADA inbound gateway).")
        try:
            r = _requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": message[:4000]},
                timeout=8,
            )
            if r.ok and r.json().get("ok"):
                self._record_send("telegram", chat, message)
                return f"Telegram message sent to {chat}."
            return f"Telegram API said: {r.text[:200]}"
        except Exception as e:
            return f"Telegram send failed: {e}"

    # ── Instagram ────────────────────────────────────────────────────
    # Honest constraint: the "Instagram Windows app" from Microsoft Store
    # is a PWA wrapping the same web view. Sending a DM through it goes
    # through the same UI as instagram.com — there's no native URL scheme
    # for direct messages (instagram:// schemes only open the camera or a
    # user profile). Two paths:
    #   1. NATIVE-FIRST: launch the Store app focused on the target user
    #      via instagram://user?username=X. User must press the message
    #      button manually — no further automation hooks exist.
    #   2. ARGUS: drive the web UI end-to-end. Slower, brittle, but does
    #      send.
    # Default: ARGUS for fully-automated send. Add an open_in_app flag
    # for users who prefer the native window and don't mind clicking
    # 'Message' themselves.
    def _instagram_send(self, handle: str = "", message: str = "",
                        open_in_app: bool = False) -> str:
        handle = (handle or "").strip().lstrip("@")
        message = (message or "").strip()
        if not handle:
            return "Need an Instagram handle (without @)."
        if not message:
            return "Need a message."
        handle = self._resolve_alias(handle, "instagram").lstrip("@")

        # Native-first path: just open the user's profile in the app and
        # paste the message to clipboard. User clicks Message → paste.
        if open_in_app:
            url = f"instagram://user?username={handle}"
            try:
                subprocess.Popen(f'start "" "{url}"', shell=True,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                # Copy the message to clipboard for one-click paste in the app.
                if _HAS_PYAUTOGUI:
                    try:
                        import pyperclip
                        pyperclip.copy(message)
                        clip_note = " Message copied to clipboard."
                    except ImportError:
                        clip_note = ""
                else:
                    clip_note = ""
                return (f"Opened Instagram app on @{handle}.{clip_note} "
                        f"Click 'Message' and paste — Instagram's app has no "
                        f"DM URL scheme so this last step is manual.")
            except Exception as e:
                return f"Couldn't open Instagram app: {e}"

        # Default: full automated send via ARGUS.
        if not self.marduk:
            return "ARGUS not reachable for Instagram send."
        argus = self.marduk.get_module("ARGUS")
        if not argus or not getattr(argus, "enabled", False):
            return "ARGUS not online — can't send Instagram DM."
        task = (
            f"Open https://www.instagram.com/direct/new/ in Chrome. In the "
            f"'To:' search box, type {handle!r} and click the matching user. "
            f"Click Next. In the message input, type EXACTLY: \"{message}\". "
            f"Press Send. Report success or any error you see. Note: Instagram "
            f"often shows login challenges or 2FA — if that happens, report "
            f"what you see and stop. confirmed."
        )
        return argus.execute("browse", {"task": task})

    # ─────────────────────────────────────────────────────────────────
    # Gmail auto-ingest (IMAP read side). Uses the same Gmail credentials
    # configured for send_email. Pulls recent messages, summarizes each
    # via SARASWATI, stores in the vault at Brain/ODIN/inbox/. SELENE
    # fires this every 20 min on its ingest loop; can also be invoked
    # on demand. Dedup by Gmail Message-ID in a small ledger.
    # ─────────────────────────────────────────────────────────────────
    def _gmail_ingest_recent(self, days: int = 1, limit: int = 8,
                             unread_only: bool = True) -> str:
        import imaplib, email, hashlib, json as _json
        from email.header import decode_header
        from datetime import datetime, timedelta

        if not self.sender_email or not self.sender_password:
            return "[MERCURY] Gmail ingest needs sender_email + GMAIL_APP_PASSWORD configured."
        if not self.marduk:
            return "[MERCURY] Gmail ingest needs MARDUK access."
        nabu = self.marduk.get_module("NABU")
        sara = self.marduk.get_module("SARASWATI")
        if not nabu or not nabu._enabled:
            return "[MERCURY] Gmail ingest needs NABU vault active."
        if not sara or not getattr(sara, "providers", None):
            return "[MERCURY] Gmail ingest needs a SARASWATI cloud provider."

        try:
            days  = max(1, min(14, int(days or 1)))
            limit = max(1, min(25, int(limit or 8)))
        except (TypeError, ValueError):
            days, limit = 1, 8

        ledger_path = os.path.join("data", "knowledge", "mercury_gmail_ledger.json")
        try:
            os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
            if os.path.exists(ledger_path):
                with open(ledger_path, "r", encoding="utf-8") as f:
                    ledger = _json.load(f)
            else:
                ledger = {}
        except (OSError, _json.JSONDecodeError):
            ledger = {}

        # Connect.
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(self.sender_email, self.sender_password)
            mail.select("INBOX", readonly=True)   # readonly so we don't mark as read
        except imaplib.IMAP4.error as e:
            return f"[MERCURY] Gmail IMAP login failed: {e}. Check GMAIL_APP_PASSWORD."
        except Exception as e:
            return f"[MERCURY] Gmail connect failed: {e}"

        try:
            # Build the search query. Gmail IMAP uses RFC3501 SEARCH syntax;
            # SINCE filters by INTERNALDATE.
            since_date = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
            search_terms = ["SINCE", since_date]
            if unread_only:
                search_terms.append("UNSEEN")
            search_str = " ".join(search_terms)
            status, data = mail.search(None, search_str)
            if status != "OK" or not data or not data[0]:
                return "[MERCURY] Gmail ingest: no new messages."

            uids = data[0].split()
            # Newest first — IMAP returns ascending UIDs by default.
            uids.reverse()
            targets = uids[:limit]

            vault_dir = os.path.join(nabu.odin_dir, "inbox")
            try:
                os.makedirs(vault_dir, exist_ok=True)
            except OSError as e:
                return f"[MERCURY] could not create inbox vault dir: {e}"

            from core.token_diet import diet
            ingested = 0
            skipped = 0
            for uid in targets:
                status, msg_data = mail.fetch(uid, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                try:
                    msg = email.message_from_bytes(raw)
                except Exception:
                    continue
                # Dedup by Message-ID, fallback to a hash of subject + date.
                mid = (msg.get("Message-ID") or "").strip()
                if not mid:
                    mid = hashlib.sha1(
                        ((msg.get("Subject") or "") + (msg.get("Date") or "")).encode()
                    ).hexdigest()
                if mid in ledger:
                    skipped += 1
                    continue

                # Decode headers properly (RFC 2047 encoded subjects).
                def _hdr(name):
                    raw = msg.get(name, "")
                    if not raw:
                        return ""
                    out = []
                    for part, enc in decode_header(raw):
                        if isinstance(part, bytes):
                            out.append(part.decode(enc or "utf-8", errors="replace"))
                        else:
                            out.append(str(part))
                    return " ".join(out).strip()
                subject = _hdr("Subject") or "(no subject)"
                sender  = _hdr("From")    or "(unknown sender)"
                date    = _hdr("Date")    or ""

                # Extract plain-text body. Prefer text/plain; fall back to text/html.
                body_text = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        ctype = part.get_content_type()
                        if part.get_content_disposition() == "attachment":
                            continue
                        if ctype == "text/plain":
                            payload = part.get_payload(decode=True)
                            if payload:
                                body_text = payload.decode(
                                    part.get_content_charset() or "utf-8",
                                    errors="replace",
                                )
                                break
                    if not body_text:
                        for part in msg.walk():
                            if part.get_content_type() == "text/html":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body_text = payload.decode(
                                        part.get_content_charset() or "utf-8",
                                        errors="replace",
                                    )
                                    break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        body_text = payload.decode(
                            msg.get_content_charset() or "utf-8",
                            errors="replace",
                        )
                clean = diet(body_text or "", max_chars=6000)
                if len(clean) < 40:
                    # Boilerplate-only / image-only mails — record + skip summary.
                    ledger[mid] = {"subject": subject, "sender": sender,
                                   "ingested_at": datetime.now().isoformat(timespec="seconds"),
                                   "skipped": True}
                    continue

                # Summarize via SARASWATI.
                try:
                    summary = sara.execute("ask_ai", {
                        "question": (
                            f"Summarize this email in 3-6 lines of Markdown. "
                            f"Front-load WHO sent it and WHY (1 line), then KEY POINTS as bullets. "
                            f"Preserve specific dates, amounts, addresses, names. End with one bullet "
                            f"flagging if it looks like spam/promotional/personal/work.\n\n"
                            f"FROM: {sender}\nSUBJECT: {subject}\n\n{clean}"
                        )
                    })
                except Exception as e:
                    summary = f"(summary failed: {e})"
                slug = re.sub(r"[^A-Za-z0-9_-]+", "_", subject)[:80] or "email"
                # Avoid collisions on identical subjects (newsletters etc.) by
                # tagging with the mid hash short.
                short_id = hashlib.sha1(mid.encode()).hexdigest()[:6]
                out_path = os.path.join(vault_dir, f"{slug}-{short_id}.md")
                try:
                    with open(out_path, "w", encoding="utf-8") as f:
                        f.write("---\n")
                        f.write(f"source: gmail\n")
                        f.write(f"from: {sender}\n")
                        f.write(f"subject: {subject}\n")
                        f.write(f"date: {date}\n")
                        f.write(f"message_id: {mid}\n")
                        f.write(f"ingested: {datetime.now().isoformat(timespec='seconds')}\n")
                        f.write("---\n\n")
                        f.write(f"# {subject}\n\n*from* `{sender}`  ·  *date* `{date}`\n\n")
                        f.write(summary.strip() + "\n")
                    ingested += 1
                    ledger[mid] = {"subject": subject, "sender": sender,
                                   "ingested_at": datetime.now().isoformat(timespec="seconds"),
                                   "vault_path": out_path}
                except OSError as e:
                    print(f"[MERCURY] write {out_path} failed: {e}")
            try:
                mail.close()
                mail.logout()
            except Exception:
                pass
        except Exception as e:
            try: mail.logout()
            except Exception: pass
            return f"[MERCURY] Gmail ingest error: {e}"

        try:
            with open(ledger_path, "w", encoding="utf-8") as f:
                _json.dump(ledger, f, indent=2)
        except OSError:
            pass

        return f"[MERCURY] gmail ingest: {ingested} new, {skipped} already-seen"
