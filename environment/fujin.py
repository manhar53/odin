# FUJIN — Japanese — god of wind and atmosphere
# Weather: current conditions, forecast, atmosphere, surroundings

import requests
from core.marduk import OdinModule


class Fujin(OdinModule):
    MODULE_NAME = "FUJIN"
    LAYER = "ENVIRONMENT"

    def __init__(self, config: dict):
        super().__init__(config)

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "get_weather",
                "description": "Get current weather for a city or location",
                "parameters": {
                    "city": {"type": "string", "description": "City name or location"}
                },
                "required": ["city"]
            },
            {
                "name": "get_forecast",
                "description": "Get a 3-day weather forecast for a location",
                "parameters": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"],
                "internal_only": True
            },
            {
                "name": "get_weather_here",
                "description": "Get weather at the current location (based on IP)",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "air_quality",
                "description": "Get current air quality (PM2.5, PM10) for a city. Uses Open-Meteo air-quality API (free, no key).",
                "parameters": {
                    "city": {"type": "string", "description": "City name"}
                },
                "required": ["city"]
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "get_weather": self._weather,
            "get_forecast": self._forecast,
            "get_weather_here": self._weather_here,
            "air_quality": self._air_quality,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[FUJIN] Error: {e}"
        return f"[FUJIN] Unknown skill: {skill_name}"

    def _weather(self, city: str = "") -> str:
        resp = requests.get(
            f"https://wttr.in/{city.replace(' ', '+')}?format=3",
            timeout=5,
            headers={"User-Agent": "ODIN/1.0"}
        )
        return resp.text.strip() if resp.ok else f"Could not fetch weather for {city}."

    def _forecast(self, city: str = "") -> str:
        resp = requests.get(
            f"https://wttr.in/{city.replace(' ', '+')}?format=%l:+%C,+%t.+Tomorrow:+%w",
            timeout=5,
            headers={"User-Agent": "ODIN/1.0"}
        )
        if resp.ok:
            return resp.text.strip()
        return f"Could not fetch forecast for {city}."

    def _weather_here(self) -> str:
        # Live IP geolocation first (15-min cache). The user is mobile and a
        # stale PROMETHEUS preference ("Bangalore" while they're in Ambala)
        # was returning weather for the wrong city. PROMETHEUS is now a
        # fallback only when ipinfo is unreachable.
        try:
            from environment.sindbad import fetch_ip_geo
        except ImportError:
            fetch_ip_geo = None   # SINDBAD missing — degrade to stored pref
        if fetch_ip_geo:
            data = fetch_ip_geo()
            if data:
                city = (data.get("city") or "").strip()
                if city:
                    return self._weather(city)
        city = self._known_location()
        if city:
            return self._weather(city)
        return "Could not determine current location (ipinfo unreachable, no saved preference)."

    def _known_location(self) -> str:
        """Return the user's self-declared city if PROMETHEUS has it."""
        if not self.marduk:
            return ""
        prom = self.marduk.get_module("PROMETHEUS")
        if not prom or not hasattr(prom, "_prefs"):
            return ""
        # PROMETHEUS auto-learn stores "I live in X" as `location: X`.
        loc = prom._prefs.get("location", {}).get("value", "")
        if loc:
            # Strip articles / trailing punctuation common in spoken phrasings.
            return loc.strip().strip(".,;:!? ").lstrip("the ")
        return ""

    def _air_quality(self, city: str = "") -> str:
        """Geocode the city → lat/lon via Open-Meteo's free geocoder, then
        hit the air-quality endpoint. No key. PM2.5 < 12 = good; 12-35 =
        moderate; 35-55 = unhealthy for sensitive; 55+ = unhealthy."""
        city = (city or "").strip()
        if not city:
            return "Need a city name."
        try:
            # Step 1: geocode the city name.
            geo = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1},
                timeout=5,
            ).json()
            results = geo.get("results") or []
            if not results:
                return f"Could not find city '{city}'."
            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            resolved = results[0].get("name", city)
            # Step 2: pull current PM2.5 / PM10.
            aq = requests.get(
                "https://air-quality-api.open-meteo.com/v1/air-quality",
                params={
                    "latitude": lat, "longitude": lon,
                    "current": "pm2_5,pm10,us_aqi",
                },
                timeout=5,
            ).json()
            cur = aq.get("current") or {}
            pm25 = cur.get("pm2_5")
            pm10 = cur.get("pm10")
            aqi  = cur.get("us_aqi")
            if pm25 is None:
                return f"No air quality data available for {resolved}."
            # Plain-English verdict on the PM2.5 reading (the most meaningful)
            if   pm25 < 12:  verdict = "good"
            elif pm25 < 35:  verdict = "moderate"
            elif pm25 < 55:  verdict = "unhealthy for sensitive groups"
            elif pm25 < 150: verdict = "unhealthy"
            else:            verdict = "hazardous"
            return (f"{resolved}: PM2.5 {pm25:.0f}, PM10 {pm10:.0f}, "
                    f"US AQI {aqi:.0f}. Air quality is {verdict}.")
        except Exception as e:
            return f"Air quality lookup failed: {e}"
