# AURORA — Roman — goddess of dawn, herald of each new day.
# Morning briefing: aggregates the user's "what does today look like" view
# from every other module (CHRONOS time + reminders, FUJIN weather, SAINT
# health, SHERLOCK recent failures, AKASHA news headlines if configured)
# and either synthesizes via Gemini (warm 3-paragraph briefing) or formats
# plainly (no cloud). Inspired by OpenJarvis's `morning_digest` agent.

from core.marduk import OdinModule


_DIGEST_PROMPT = """Compose a short "good morning" briefing for the user based on the data below. Style: warm, concise, in second-person, three short paragraphs. Don't mention the data sources by name; just present the briefing as if you already knew everything. No markdown, no bullet points — just plain spoken prose.

Data:
{data}

Write the briefing now."""


class Aurora(OdinModule):
    MODULE_NAME = "AURORA"
    LAYER = "MANAGEMENT"

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "morning_digest",
                "description": "Compose today's morning briefing: time, weather, active reminders, recent system issues, news headlines, and health log — synthesized into a 3-paragraph spoken summary. Aggregates from CHRONOS, FUJIN, SAINT, SHERLOCK, AKASHA. Uses Gemini for synthesis if available, otherwise plain concatenation.",
                "parameters": {},
                "required": []
            }
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        if skill_name == "morning_digest":
            try:
                return self._morning_digest()
            except Exception as e:
                return f"[AURORA] Error: {e}"
        return f"[AURORA] Unknown skill: {skill_name}"

    def _morning_digest(self) -> str:
        if not self.marduk:
            return "Cannot compose digest — MARDUK not connected."
        sources = self._gather_sources()
        # If SARASWATI cloud is available, synthesize a natural briefing.
        # Otherwise fall back to a plain concatenation — still useful, just
        # less natural-sounding.
        sara = self.marduk.get_module("SARASWATI")
        if sara and getattr(sara, "providers", None):
            briefing = self._synthesize_via_cloud(sources, sara)
            if briefing and briefing.strip() and not briefing.startswith("No cloud"):
                return briefing
        return self._format_plain(sources)

    def _gather_sources(self) -> dict:
        """Pull each data source. Each is wrapped in try/except — a single
        misbehaving module shouldn't kill the whole digest."""
        out = {}
        def _try(module_name, skill, args=None):
            mod = self.marduk.get_module(module_name)
            if not mod:
                return ""
            try:
                r = mod.execute(skill, args or {})
                if isinstance(r, str) and r.strip() and not r.startswith(("[", "MARDUK", "No ", "Empty")):
                    return r.strip()
            except Exception:
                pass
            return ""
        out["time"]      = _try("CHRONOS",  "get_time")
        out["weather"]   = _try("FUJIN",    "get_weather_here")
        out["reminders"] = _try("CHRONOS",  "list_reminders")
        out["health"]    = _try("SAINT",    "get_health_summary")
        out["failures"]  = _try("SHERLOCK", "weekly_digest", {"days": 1})
        out["news"]      = _try("AKASHA",   "news_headlines")
        return out

    @staticmethod
    def _synthesize_via_cloud(sources: dict, sara) -> str:
        # Format sources into a labelled block for the LLM prompt.
        # Skip empty sources entirely so the LLM doesn't reference absent data.
        labelled = []
        for key, value in sources.items():
            if value:
                labelled.append(f"- {key}: {value}")
        if not labelled:
            return ""
        prompt = _DIGEST_PROMPT.format(data="\n".join(labelled))
        return sara.execute("ask_ai", {"question": prompt})

    @staticmethod
    def _format_plain(sources: dict) -> str:
        """Fallback when no cloud provider is configured. Concatenate each
        non-empty source with a natural connective."""
        parts = []
        if sources.get("time"):
            parts.append(f"Good morning. {sources['time']}")
        if sources.get("weather"):
            parts.append(f"Outside, it's {sources['weather']}.")
        if sources.get("reminders") and "no reminder" not in sources["reminders"].lower():
            parts.append(f"On your reminders list: {sources['reminders']}")
        if sources.get("health"):
            parts.append(sources["health"])
        if sources.get("news"):
            parts.append(f"In the news: {sources['news']}")
        if sources.get("failures") and "ran clean" not in sources["failures"].lower() \
                and "no failure" not in sources["failures"].lower():
            parts.append(f"A note on yesterday: {sources['failures']}")
        if not parts:
            return "Nothing to brief about right now."
        return " ".join(parts)
