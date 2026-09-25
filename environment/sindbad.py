# SINDBAD — Arabian hero — greatest navigator, crossed seven seas
# Navigation: maps, directions, routing, location awareness

import time
import webbrowser
import requests
from core.marduk import OdinModule


# ── Shared IP-geolocation cache ──────────────────────────────────────
# 15-min TTL: short enough that travel (Bangalore → Ambala) is picked up
# within the same session, long enough that we don't hammer ipinfo on
# every "where am I" / weather follow-up. FUJIN imports the same helper
# so both stay in sync.
_GEO_TTL_SECONDS = 15 * 60
_geo_cache = {"data": None, "fetched_at": 0.0}


def fetch_ip_geo(timeout: float = 4.0) -> dict | None:
    """Returns ipinfo.io payload (city, region, country, loc) with TTL caching.
    None on network failure — callers fall through to PROMETHEUS preference."""
    now = time.monotonic()
    if _geo_cache["data"] is not None and (now - _geo_cache["fetched_at"]) < _GEO_TTL_SECONDS:
        return _geo_cache["data"]
    try:
        resp = requests.get("https://ipinfo.io/json", timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        _geo_cache["data"] = data
        _geo_cache["fetched_at"] = now
        return data
    except Exception:
        return None


def invalidate_geo_cache():
    """Force the next fetch_ip_geo to hit the network. Used when the user
    explicitly says 'refresh my location' / 'I just moved'."""
    _geo_cache["data"] = None
    _geo_cache["fetched_at"] = 0.0


class Sindbad(OdinModule):
    MODULE_NAME = "SINDBAD"
    LAYER = "ENVIRONMENT"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "get_directions",
                "description": "Get directions from one place to another",
                "parameters": {
                    "origin": {"type": "string", "description": "Starting location"},
                    "destination": {"type": "string", "description": "Destination location"},
                    "mode": {"type": "string", "description": "Travel mode: driving, walking, transit"}
                },
                "required": ["origin", "destination"]
            },
            {
                "name": "search_nearby",
                "description": "Search for places near a location (e.g. restaurants, hospitals)",
                "parameters": {
                    "query": {"type": "string", "description": "What to search for"},
                    "location": {"type": "string", "description": "Near this location"}
                },
                "required": ["query", "location"],
                "internal_only": True
            },
            {
                "name": "open_maps",
                "description": "Open Google Maps in the browser",
                "parameters": {
                    "query": {"type": "string", "description": "Search query or location"}
                },
                "required": [],
                "internal_only": True
            },
            {
                "name": "get_my_location",
                "description": "Get the approximate current location based on IP",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "track_flight",
                "description": "Track a live flight by callsign or flight number (e.g. 'AI132', 'UAL245'). Returns current position, altitude, ground speed if airborne. Uses OpenSky Network — free, no key.",
                "parameters": {
                    "callsign": {"type": "string", "description": "Airline callsign or flight number"}
                },
                "required": ["callsign"]
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "get_directions": self._directions,
            "search_nearby": self._nearby,
            "open_maps": self._open_maps,
            "get_my_location": self._my_location,
            "track_flight": self._track_flight,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[SINDBAD] Error: {e}"
        return f"[SINDBAD] Unknown skill: {skill_name}"

    def _directions(self, origin: str = "", destination: str = "", mode: str = "driving") -> str:
        o = origin.replace(" ", "+")
        d = destination.replace(" ", "+")
        url = f"https://www.google.com/maps/dir/{o}/{d}/?travelmode={mode}"
        webbrowser.open(url)
        return f"Opening directions from {origin} to {destination} by {mode}."

    def _nearby(self, query: str = "", location: str = "") -> str:
        search = f"{query} near {location}".replace(" ", "+")
        url = f"https://www.google.com/maps/search/{search}"
        webbrowser.open(url)
        return f"Searching for {query} near {location} on Maps."

    def _open_maps(self, query: str = "") -> str:
        if query:
            url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        else:
            url = "https://www.google.com/maps"
        webbrowser.open(url)
        return "Opening Google Maps."

    def _my_location(self) -> str:
        # Live IP geolocation first — the user is mobile and a stale PROMETHEUS
        # preference ("Bangalore" while they're in Ambala) was misreporting.
        # 15-min cache keeps it cheap. PROMETHEUS preference is now ONLY a
        # fallback when ipinfo is unreachable.
        data = fetch_ip_geo()
        if data:
            city = data.get("city", "").strip()
            region = data.get("region", "").strip()
            country = data.get("country", "").strip()
            parts = [p for p in (city, region, country) if p]
            if parts:
                return f"You are in {', '.join(parts)}."
        known = self._known_location()
        if known:
            return f"You are in {known} (offline preference — ipinfo unreachable)."
        return "Could not determine location: ipinfo unreachable and no saved preference."

    def _known_location(self) -> str:
        if not self.marduk:
            return ""
        prom = self.marduk.get_module("PROMETHEUS")
        if not prom or not hasattr(prom, "_prefs"):
            return ""
        loc = prom._prefs.get("location", {}).get("value", "")
        if loc:
            return loc.strip().strip(".,;:!? ").lstrip("the ")
        return ""

    def _track_flight(self, callsign: str = "") -> str:
        # OpenSky's /states/all returns ALL ~10,000 currently-airborne flights
        # as a big array. We filter client-side because the API doesn't expose
        # a callsign-search endpoint. Free, no key, anonymous rate-limited.
        target = (callsign or "").strip().upper().replace(" ", "")
        if not target:
            return "Need a callsign or flight number."
        try:
            r = requests.get(
                "https://opensky-network.org/api/states/all",
                timeout=10,
                headers={"User-Agent": "ODIN/1.0"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            return f"OpenSky lookup failed: {e}"
        # State vector format: [icao24, callsign, country, time_pos, last_contact,
        # longitude, latitude, baro_altitude, on_ground, velocity, true_track,
        # vertical_rate, sensors, geo_altitude, squawk, spi, position_source]
        match = None
        for s in data.get("states", []) or []:
            cs = (s[1] or "").strip().upper()
            if cs == target or cs.replace(" ", "") == target:
                match = s
                break
        if not match:
            return f"No live flight found for callsign '{target}'. It may be on the ground, between flights, or use a different callsign than expected."
        cs       = (match[1] or "").strip()
        country  = match[2] or "?"
        lon, lat = match[5], match[6]
        alt_m    = match[7] or match[13]
        on_gnd   = match[8]
        velo_ms  = match[9]
        if on_gnd:
            return f"{cs} ({country}) is on the ground."
        # Convert m to feet and m/s to knots for typical aviation units.
        alt_ft   = int((alt_m or 0) * 3.28084)
        speed_kt = int((velo_ms or 0) * 1.94384)
        return (f"{cs} ({country}) at {alt_ft:,} ft, {speed_kt} knots, "
                f"position {lat:.2f}, {lon:.2f}.")
