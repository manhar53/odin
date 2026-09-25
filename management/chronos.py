# CHRONOS — Greek — god of time itself
# Planning: calendar, scheduling, reminders, time awareness

import json
import os
import re
import threading
import time
import requests
from datetime import datetime, timedelta
from core.marduk import OdinModule


# Weekday-name → weekday index (Monday=0..Sunday=6) for recurring rules.
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}


# Country-name → ISO-2 lookup. Voice users say "India" / "United States" /
# "U K", not "IN" / "US" / "GB". Covers the countries the user is likely to
# ask about; unknown names fall through to a 2-letter strip.
_COUNTRY_NAME_TO_ISO = {
    "india": "IN", "bharat": "IN",
    "united states": "US", "usa": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "britain": "GB", "england": "GB",
    "canada": "CA", "australia": "AU", "new zealand": "NZ",
    "germany": "DE", "france": "FR", "italy": "IT", "spain": "ES",
    "netherlands": "NL", "holland": "NL", "belgium": "BE", "switzerland": "CH",
    "austria": "AT", "ireland": "IE", "portugal": "PT", "poland": "PL",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "japan": "JP", "china": "CN", "south korea": "KR", "korea": "KR",
    "singapore": "SG", "indonesia": "ID", "thailand": "TH", "vietnam": "VN",
    "philippines": "PH", "malaysia": "MY", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "nepal": "NP", "uae": "AE", "saudi arabia": "SA",
    "russia": "RU", "ukraine": "UA", "turkey": "TR",
    "brazil": "BR", "mexico": "MX", "argentina": "AR", "chile": "CL",
    "south africa": "ZA", "egypt": "EG", "nigeria": "NG", "kenya": "KE",
    "israel": "IL",
}


def _resolve_country_code(s: str) -> str:
    """Accept ISO-2 codes or country names. Returns uppercase 2-letter code,
    or strips to first 2 chars uppercased as a last-ditch fallback."""
    s = (s or "").strip().lower().strip(".,!?")
    if s in _COUNTRY_NAME_TO_ISO:
        return _COUNTRY_NAME_TO_ISO[s]
    if len(s) == 2 and s.isalpha():
        return s.upper()
    return (s.replace(" ", "")[:2] or "IN").upper()


def _parse_recurrence(when: str) -> tuple[str, str] | None:
    """If the 'when' phrase describes a RECURRING schedule, return a
    (rule_kind, rule_payload) tuple suitable for storing on the reminder.
    Returns None for one-shot reminders.

    Supported rule kinds:
      - ("daily",  "HH:MM")        — every day at HH:MM
      - ("hourly", "")             — every hour on the hour
      - ("weekly", "WD@HH:MM")     — every <weekday> at HH:MM
      - ("every_n_minutes", "N")   — every N minutes
    """
    s = (when or "").strip().lower()
    # "every hour" / "hourly"
    if re.search(r"\b(?:every\s+hour|hourly)\b", s):
        return ("hourly", "")
    # "every N minutes"
    m = re.search(r"\bevery\s+(\d+)\s+minute", s)
    if m:
        return ("every_n_minutes", m.group(1))
    # "every day at HH[:MM][am|pm]" / "daily at ..."
    m = re.search(r"\b(?:every\s+day|daily)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        hh, mm, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and hh < 12: hh += 12
        if ampm == "am" and hh == 12: hh = 0
        return ("daily", f"{hh:02d}:{mm:02d}")
    # "every <weekday> at HH:MM"
    weekday_alt = "|".join(_WEEKDAYS.keys())
    m = re.search(rf"\bevery\s+({weekday_alt})(?:\s+at)?\s+(\d{{1,2}})(?::(\d{{2}}))?\s*(am|pm)?", s)
    if m:
        wd = _WEEKDAYS[m.group(1)]
        hh, mm, ampm = int(m.group(2)), int(m.group(3) or 0), m.group(4)
        if ampm == "pm" and hh < 12: hh += 12
        if ampm == "am" and hh == 12: hh = 0
        return ("weekly", f"{wd}@{hh:02d}:{mm:02d}")
    # "every <weekday>" with no time → default 9am
    m = re.search(rf"\bevery\s+({weekday_alt})\b", s)
    if m:
        wd = _WEEKDAYS[m.group(1)]
        return ("weekly", f"{wd}@09:00")
    return None


def _next_fire(rule_kind: str, rule_payload: str, now: datetime | None = None) -> datetime | None:
    """Compute the NEXT fire time for a recurring rule, strictly after `now`."""
    now = now or datetime.now()
    if rule_kind == "hourly":
        nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        return nxt
    if rule_kind == "every_n_minutes":
        try:
            n = int(rule_payload)
        except ValueError:
            return None
        return now + timedelta(minutes=max(1, n))
    if rule_kind == "daily":
        try:
            hh, mm = (int(x) for x in rule_payload.split(":"))
        except (ValueError, AttributeError):
            return None
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if rule_kind == "weekly":
        try:
            wd_part, time_part = rule_payload.split("@")
            wd = int(wd_part)
            hh, mm = (int(x) for x in time_part.split(":"))
        except (ValueError, AttributeError):
            return None
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        days_ahead = (wd - target.weekday()) % 7
        target += timedelta(days=days_ahead)
        if target <= now:
            target += timedelta(days=7)
        return target
    return None


def _parse_when(when: str, now: datetime | None = None) -> datetime | None:
    """Best-effort parse of the natural-language 'when' field. Pure stdlib —
    no external dep — so this stays offline. Recognises:

      - 'in 5 minutes', 'in 2 hours', 'in 30 seconds'
      - '3pm', '3:30pm', '15:00'
      - 'tomorrow 9am', 'tomorrow at 9:30'
      - ISO timestamps ('2026-05-05T15:00:00')

    Returns the absolute datetime to fire, or None if it can't tell."""
    if not when:
        return None
    now = now or datetime.now()
    s = when.strip().lower()

    # ISO first — the 'created' field round-trips through this
    try:
        return datetime.fromisoformat(when.strip())
    except (ValueError, TypeError):
        pass

    # 'in N (seconds|minutes|hours|days)'
    m = re.match(r"in\s+(\d+)\s*(second|minute|hour|day)s?", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "second": timedelta(seconds=n),
            "minute": timedelta(minutes=n),
            "hour":   timedelta(hours=n),
            "day":    timedelta(days=n),
        }[unit]
        return now + delta

    # 'tomorrow [at] HH[:MM][am|pm]' or 'today [at] ...'
    m = re.match(r"(tomorrow|today)(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", s)
    if m:
        which, hh, mm, ampm = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
        if ampm == "pm" and hh < 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0
        base = now if which == "today" else (now + timedelta(days=1))
        return base.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # bare 'HH[:MM][am|pm]' — interpreted as today, or next day if already past
    m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if m:
        hh, mm, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ampm == "pm" and hh < 12:
            hh += 12
        if ampm == "am" and hh == 12:
            hh = 0
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    return None


class Chronos(OdinModule):
    MODULE_NAME = "CHRONOS"
    LAYER = "MANAGEMENT"

    def __init__(self, config: dict):
        super().__init__(config)
        self._reminders_path = "data/knowledge/reminders.json"
        self._reminders: list = self._load()
        # Backfill 'fire_at' on any legacy reminders that pre-date the scheduler.
        for r in self._reminders:
            if "fire_at" not in r:
                fire = _parse_when(r.get("when", ""))
                r["fire_at"] = fire.isoformat() if fire else None
        self._save()
        self._stop = threading.Event()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name="CHRONOS-scheduler"
        )
        self._scheduler_thread.start()

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "get_time",
                "description": "Get the current date and time",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "set_reminder",
                "description": "Set a reminder. Supports one-shot ('at 3pm', 'tomorrow 9am', 'in 2 hours') AND recurring ('every day at 7am', 'every Monday at 9am', 'every hour', 'every 30 minutes', 'daily at 8pm'). Recurring reminders re-arm automatically after firing.",
                "parameters": {
                    "text": {"type": "string", "description": "What to be reminded about"},
                    "when": {"type": "string", "description": "When (e.g. 3pm, tomorrow 9am, in 2 hours, every day at 7am, every Monday at 9am, hourly)"}
                },
                "required": ["text", "when"]
            },
            {
                "name": "list_reminders",
                "description": "List all saved reminders",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "delete_reminder",
                "description": "Delete reminder(s) whose text contains the given phrase. Case-insensitive substring match.",
                "parameters": {
                    "text": {"type": "string", "description": "Substring of the reminder text to delete (e.g. 'water', 'meds')"}
                },
                "required": ["text"],
            },
            {
                "name": "clear_reminders",
                "description": "Delete ALL reminders (one-shot AND recurring). Voice: 'cancel all reminders' / 'delete all reminders' / 'stop all reminders'.",
                "parameters": {},
                "required": [],
            },
            {
                "name": "open_calendar",
                "description": "Open Google Calendar in the browser",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "public_holidays",
                "description": "List upcoming public holidays for a country this year. Uses nager.date (free, no key).",
                "parameters": {
                    "country": {"type": "string", "description": "Two-letter country code (IN, US, GB, etc.). Defaults to IN."},
                    "year": {"type": "integer", "description": "Year (default current year)"}
                },
                "required": []
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "get_time":         self._get_time,
            "set_reminder":     self._set_reminder,
            "list_reminders":   self._list_reminders,
            "delete_reminder":  self._delete_reminder,
            "clear_reminders":  self._clear_reminders,
            "open_calendar":    self._open_calendar,
            "public_holidays":  self._public_holidays,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[CHRONOS] Error: {e}"
        return f"[CHRONOS] Unknown skill: {skill_name}"

    def _get_time(self) -> str:
        now = datetime.now()
        return now.strftime("It's %I:%M %p on %A, %B %d, %Y.")

    def _set_reminder(self, text: str = "", when: str = "") -> str:
        # First try recurrence — "every Monday at 9am" wins over one-shot parsing.
        recur = _parse_recurrence(when)
        if recur:
            rule_kind, rule_payload = recur
            fire = _next_fire(rule_kind, rule_payload)
            reminder = {
                "text": text,
                "when": when,
                "fire_at": fire.isoformat() if fire else None,
                "fired": False,
                "recurrence_kind": rule_kind,
                "recurrence_payload": rule_payload,
                "created": datetime.now().isoformat(),
            }
            self._reminders.append(reminder)
            self._save()
            if fire is None:
                return f"Recurring reminder noted: '{text}' ({when}). Couldn't compute the next fire time."
            when_pretty = fire.strftime("%I:%M %p on %A").lstrip("0")
            return f"Recurring reminder set: '{text}', first firing {when_pretty}. Will re-arm automatically."
        # One-shot path (existing behaviour)
        fire = _parse_when(when)
        reminder = {
            "text": text,
            "when": when,
            "fire_at": fire.isoformat() if fire else None,
            "fired": False,
            "created": datetime.now().isoformat(),
        }
        self._reminders.append(reminder)
        self._save()
        if fire is None:
            return f"Reminder noted: '{text}' at {when}. (Couldn't parse the time, won't fire automatically.)"
        when_pretty = fire.strftime("%I:%M %p on %A").lstrip("0")
        return f"Reminder set: '{text}' for {when_pretty}."

    def _list_reminders(self) -> str:
        if not self._reminders:
            return "No reminders set."
        lines = [f"{i+1}. {r['text']} — {r['when']}" for i, r in enumerate(self._reminders)]
        return "Reminders: " + "; ".join(lines)

    def _delete_reminder(self, text: str = "") -> str:
        text = (text or "").strip()
        if not text:
            return "Need a substring of the reminder text to delete (or use clear_reminders for all)."
        original = len(self._reminders)
        self._reminders = [r for r in self._reminders if text.lower() not in r["text"].lower()]
        self._save()
        removed = original - len(self._reminders)
        if removed == 0:
            return f"No reminders matched '{text}'."
        return f"Removed {removed} reminder(s) matching '{text}'."

    def _clear_reminders(self) -> str:
        n = len(self._reminders)
        if n == 0:
            return "No reminders to clear."
        self._reminders = []
        self._save()
        return f"Cleared all {n} reminder(s)."

    def _open_calendar(self) -> str:
        import webbrowser
        webbrowser.open("https://calendar.google.com")
        return "Opening Google Calendar."

    def _public_holidays(self, country: str = "IN", year: int = 0) -> str:
        country = _resolve_country_code(country or "IN")
        try:
            year = int(year) if year else datetime.now().year
        except (TypeError, ValueError):
            year = datetime.now().year
        # Try nager.date first — covers 122 countries, no key.
        holidays = self._fetch_nager(country, year)
        if holidays is None:
            # nager doesn't support this country (notably India). Try
            # Calendarific if a key is configured; otherwise explain.
            cal_key = (self.config.get("chronos", {}).get("calendarific_api_key")
                       or os.environ.get("CALENDARIFIC_API_KEY", ""))
            if cal_key:
                holidays = self._fetch_calendarific(country, year, cal_key)
            if holidays is None:
                return (f"'{country}' is not covered by the free holidays API (nager.date). "
                        f"For India + other Asian countries, set CALENDARIFIC_API_KEY "
                        f"(free 1000/month at calendarific.com).")
        if not holidays:
            return f"No public holidays found for {country} in {year}."
        # Filter to upcoming-only when querying the current year.
        today = datetime.now().date()
        upcoming = [h for h in holidays
                    if datetime.fromisoformat(h["date"]).date() >= today]
        chosen = upcoming if year == today.year else holidays
        # Voice-friendly: next 5.
        lines = []
        for h in chosen[:5]:
            d = datetime.fromisoformat(h["date"]).strftime("%b %d")
            lines.append(f"{d} - {h['name']}")
        prefix = f"Next public holidays in {country}, {year}: " if year == today.year else f"Holidays in {country}, {year}: "
        return prefix + "; ".join(lines) + ("." if lines else "")

    def _fetch_nager(self, country: str, year: int) -> list | None:
        try:
            r = requests.get(
                f"https://date.nager.at/api/v3/PublicHolidays/{year}/{country}",
                timeout=5,
            )
            # 204 = unsupported country. Any non-2xx with body = real error.
            if r.status_code == 204:
                return None
            r.raise_for_status()
            return r.json() or []
        except Exception as e:
            self._log.warning(f"nager.date fetch failed: {e}")
            return None

    def _fetch_calendarific(self, country: str, year: int, key: str) -> list | None:
        try:
            r = requests.get(
                "https://calendarific.com/api/v2/holidays",
                params={"api_key": key, "country": country, "year": year},
                timeout=6,
            )
            r.raise_for_status()
            data = r.json()
            raw = (data.get("response") or {}).get("holidays", []) or []
            # Normalize to nager's shape so the caller doesn't care.
            return [{
                "date": h["date"]["iso"][:10],
                "name": h["name"],
            } for h in raw]
        except Exception as e:
            self._log.warning(f"calendarific fetch failed: {e}")
            return None

    def _load(self) -> list:
        if os.path.exists(self._reminders_path):
            with open(self._reminders_path) as f:
                return json.load(f)
        return []

    def _save(self):
        os.makedirs(os.path.dirname(self._reminders_path), exist_ok=True)
        with open(self._reminders_path, "w") as f:
            json.dump(self._reminders, f, indent=2)

    def _scheduler_loop(self):
        """Polls every 20s for due reminders and fires them through IRIS +
        CASSANDRA toast. Behaviour depends on recurrence:
          - One-shot reminders: marked 'fired=True' so they don't fire again.
          - Recurring reminders: stay armed; fire_at advances to the next
            occurrence after the rule (so a daily 7am reminder fires daily)."""
        while not self._stop.wait(20):
            try:
                now = datetime.now()
                changed = False
                for r in self._reminders:
                    if r.get("fired") or not r.get("fire_at"):
                        continue
                    try:
                        fire_at = datetime.fromisoformat(r["fire_at"])
                    except (ValueError, TypeError):
                        continue
                    if fire_at <= now:
                        self._fire(r)
                        recur_kind = r.get("recurrence_kind")
                        if recur_kind:
                            # Re-arm: compute the next fire time after now.
                            nxt = _next_fire(recur_kind, r.get("recurrence_payload", ""), now)
                            r["fire_at"] = nxt.isoformat() if nxt else None
                            if not nxt:
                                # Couldn't compute next — disable to avoid loop
                                r["fired"] = True
                        else:
                            r["fired"] = True
                        changed = True
                if changed:
                    self._save()
            except Exception as e:
                self._log.warning(f"scheduler tick failed: {e}")

    def _fire(self, reminder: dict):
        text = reminder.get("text", "reminder")
        message = f"Reminder: {text}"
        # Speak it
        if self.marduk:
            iris = self.marduk.get_module("IRIS")
            if iris:
                try:
                    iris.speak(message)
                except Exception:
                    pass
            # Also raise a Windows toast via CASSANDRA so the user sees it if mute.
            cassandra = self.marduk.get_module("CASSANDRA")
            if cassandra:
                try:
                    cassandra.execute("notify", {"message": message, "title": "ODIN reminder"})
                except Exception:
                    pass
