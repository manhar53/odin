# BRIHASPATI — Hindu — Brihaspati (Jupiter), guru of the gods: lord of
# wisdom, eloquence, and political counsel (Niti, the science of statecraft).
# He is ODIN's geopolitics mind: he watches the world's news, holds the
# context of who-wants-what and why, and counsels — answering questions and
# arguing both sides of a question at a high level.
#
# How he satisfies the three constraints:
#   - News aggregation uses FREE public RSS feeds (no AI, no key) — allowed
#     and unrestricted per the cloud-AI policy. Works offline-degraded:
#     unreachable feeds are skipped; the last good digest is cached to the
#     vault and returned when the network is down.
#   - DEEP analysis / debate routes through SARASWATI (the only module
#     permitted cloud AI), whose results are cached to the vault — so the
#     same geopolitical question answers offline next time. If SARASWATI's
#     whole provider chain is down, BRIHASPATI returns the grounded news
#     digest plus a clear note rather than failing.
#
# Feeds are a curated default map by category; override/extend in config
# under `brihaspati.feeds`.

import os
import json
import time
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.marduk import OdinModule

try:
    import feedparser
    _HAS_FEEDPARSER = True
except ImportError:
    _HAS_FEEDPARSER = False


# Curated free RSS feeds. Reputable, broad, and stable. The "world" set
# doubles as the geopolitics base; category feeds layer on specifics.
_DEFAULT_FEEDS = {
    "world": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://www.theguardian.com/world/rss",
        "https://feeds.npr.org/1004/rss.xml",
        "https://rss.dw.com/rdf/rss-en-world",
        "https://www.france24.com/en/rss",
    ],
    "geopolitics": [
        "http://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.aljazeera.com/xml/rss/all.xml",
        "https://thediplomat.com/feed/",
        "https://rss.dw.com/rdf/rss-en-world",
        "https://www.france24.com/en/rss",
    ],
    "india": [
        "https://www.thehindu.com/news/national/feeder/default.rss",
        "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
        "https://www.thehindu.com/news/international/feeder/default.rss",
    ],
    "finance": [
        "https://www.theguardian.com/uk/business/rss",
        "https://feeds.npr.org/1006/rss.xml",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
    ],
    "science": [
        "https://www.theguardian.com/science/rss",
        "https://feeds.npr.org/1007/rss.xml",
    ],
}


class Brihaspati(OdinModule):
    MODULE_NAME = "BRIHASPATI"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("brihaspati", {})
        self.feeds = cfg.get("feeds") or _DEFAULT_FEEDS
        self.max_per_feed = int(cfg.get("max_per_feed", 12))
        self.max_items = int(cfg.get("max_items", 40))
        self.feed_timeout = float(cfg.get("feed_timeout", 6.0))
        self.analysis_items = int(cfg.get("analysis_items", 28))
        vault_root = os.path.expanduser(config.get("nabu", {}).get("vault_root", "~/Brain"))
        self.cache_dir = os.path.join(vault_root, "ODIN", "news")

    @property
    def skills(self) -> list[dict]:
        cats = ", ".join(sorted(self.feeds.keys()))
        return [
            {
                "name": "world_news",
                "description": (
                    "Get a briefing of the latest real headlines from across the "
                    "world's major news sources. Use when the user asks what's "
                    "happening, for the news, or for an update on current events. "
                    f"Optional category, one of: {cats}."
                ),
                "parameters": {
                    "category": {"type": "string",
                                 "description": f"Which feed set to pull. One of: {cats}. Defaults to world."},
                },
                "required": [],
            },
            {
                "name": "geopolitics",
                "description": (
                    "Deep geopolitical analysis of a topic, region, or event, "
                    "grounded in the latest news. Returns context, the actors and "
                    "their interests, what's driving it, and what to watch next. "
                    "Use for 'what's going on with X', 'explain the situation in Y', "
                    "'why is Z happening'."
                ),
                "parameters": {
                    "topic": {"type": "string",
                              "description": "The region, country, conflict, or event to analyze."},
                },
                "required": ["topic"],
            },
            {
                "name": "debate",
                "description": (
                    "Argue a proposition at a high level: steelman both sides with "
                    "evidence drawn from current events, then give a reasoned "
                    "synthesis. Use when the user wants a debate, the case for/against "
                    "something, or to pressure-test a geopolitical claim."
                ),
                "parameters": {
                    "proposition": {"type": "string",
                                    "description": "The claim or question to debate."},
                },
                "required": ["proposition"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        try:
            if skill_name == "world_news":
                return self._world_news(args.get("category", "world"))
            if skill_name == "geopolitics":
                return self._geopolitics(args.get("topic", ""))
            if skill_name == "debate":
                return self._debate(args.get("proposition", ""))
        except Exception as e:
            return f"[BRIHASPATI] Error: {e}"
        return f"[BRIHASPATI] Unknown skill: {skill_name}"

    # === News aggregation (free RSS, offline-degraded) ===================

    def _fetch_feed(self, url: str) -> list[dict]:
        if not _HAS_FEEDPARSER:
            return []
        items = []
        try:
            # feedparser has no timeout arg; fetch the bytes ourselves with one.
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "ODIN/1.0 (+local assistant)"})
            with urllib.request.urlopen(req, timeout=self.feed_timeout) as r:
                raw = r.read()
            parsed = feedparser.parse(raw)
            source = (parsed.feed.get("title") or url).strip()
            for e in parsed.entries[: self.max_per_feed]:
                ts = None
                for key in ("published_parsed", "updated_parsed"):
                    if e.get(key):
                        ts = time.mktime(e[key])
                        break
                items.append({
                    "title": (e.get("title") or "").strip(),
                    "summary": _clean(e.get("summary", ""))[:280],
                    "source": source,
                    "link": e.get("link", ""),
                    "ts": ts or 0.0,
                })
        except Exception:
            return []   # a single dead feed must never sink the briefing
        return items

    def _gather(self, category: str) -> list[dict]:
        category = (category or "world").lower().strip()
        urls = self.feeds.get(category) or self.feeds.get("world", [])
        items = []
        with ThreadPoolExecutor(max_workers=min(8, len(urls) or 1)) as ex:
            futs = [ex.submit(self._fetch_feed, u) for u in urls]
            for f in as_completed(futs):
                items.extend(f.result())
        # Dedupe by title; newest first.
        seen, deduped = set(), []
        for it in sorted(items, key=lambda x: x["ts"], reverse=True):
            key = it["title"].lower()[:80]
            if key and key not in seen:
                seen.add(key)
                deduped.append(it)
        return deduped[: self.max_items]

    def _world_news(self, category: str) -> str:
        category = (category or "world").lower().strip()
        items = self._gather(category)
        if not items:
            cached = self._load_cache(f"digest_{category}")
            if cached:
                return f"(Network looks down — last cached {category} briefing.)\n\n{cached}"
            return ("I couldn't reach the news feeds and have no cached briefing yet. "
                    "Check the connection and try again.")
        lines = [f"Here's the latest from {category}:", ""]
        for i, it in enumerate(items[:12], 1):
            when = _ago(it["ts"])
            lines.append(f"{i}. {it['title']} — {it['source']}{when}")
        digest = "\n".join(lines)
        self._save_cache(f"digest_{category}", digest)
        return digest

    # === Deep analysis & debate (cloud via SARASWATI, vault-cached) =======

    def _news_context(self, category: str, keywords: str = "") -> tuple[str, list[dict]]:
        """Build a compact, dated headline block to ground the cloud model."""
        items = self._gather(category)
        kw = [w for w in keywords.lower().split() if len(w) > 2]
        if kw:
            filtered = [it for it in items
                        if any(w in (it["title"] + " " + it["summary"]).lower() for w in kw)]
            # Fall back to the broad set if the topic isn't in today's headlines.
            items = filtered or items
        items = items[: self.analysis_items]
        block = "\n".join(f"- {it['title']} ({it['source']}{_ago(it['ts'])})"
                          + (f"\n  {it['summary']}" if it["summary"] else "")
                          for it in items)
        return block, items

    def _ask_saraswati(self, system_hint: str, question: str) -> str:
        """Route reasoning through SARASWATI so all cloud calls + vault caching
        stay in one place. Returns '' if the provider chain is unavailable."""
        try:
            result = self.send("SARASWATI", "ask_ai", question=f"{system_hint}\n\n{question}")
        except Exception as e:
            return f""
        result = str(result or "").strip()
        # SARASWATI's offline/no-key message starts with a known phrase; treat
        # any "can't reach" style answer as a degrade signal.
        low = result.lower()
        if (not result or low.startswith("marduk")
                or "can't reach" in low or "cannot reach" in low
                or "no cloud" in low or "offline" in low):
            return ""
        return result

    def _geopolitics(self, topic: str) -> str:
        topic = (topic or "").strip()
        if not topic:
            return "Which region, country, or event should I analyze?"
        block, items = self._news_context("geopolitics", topic)
        if not items:
            cached = self._load_cache(f"analysis_{_slug(topic)}")
            if cached:
                return f"(Offline — last cached analysis of {topic}.)\n\n{cached}"
        question = (
            f"Analyze the current situation regarding: {topic}.\n\n"
            f"Recent headlines for grounding:\n{block or '(no fresh headlines retrieved)'}\n\n"
            "Give: (1) what is happening now, (2) the key actors and what each "
            "actually wants, (3) the deeper drivers and historical context, "
            "(4) likely scenarios and what to watch. Be concrete and even-handed."
        )
        analysis = self._ask_saraswati(
            "You are a seasoned geopolitical analyst. Concise, factual, multi-sided.",
            question,
        )
        if not analysis:
            if block:
                return (f"I can't reach my deep-analysis providers right now, but here are "
                        f"the latest headlines on {topic}:\n\n{block}")
            cached = self._load_cache(f"analysis_{_slug(topic)}")
            return cached or (f"I can't reach my analysis providers and have no cached read "
                              f"on {topic} yet.")
        self._save_cache(f"analysis_{_slug(topic)}", f"# {topic}\n\n{analysis}\n\n## Sources\n" +
                         "\n".join(f"- {it['title']} — {it['link']}" for it in items[:15]))
        return analysis

    def _debate(self, proposition: str) -> str:
        proposition = (proposition or "").strip()
        if not proposition:
            return "What proposition would you like me to debate?"
        block, items = self._news_context("world", proposition)
        question = (
            f"Proposition to debate: \"{proposition}\".\n\n"
            f"Relevant current events:\n{block or '(no fresh headlines retrieved)'}\n\n"
            "Steelman the case FOR, then the case AGAINST — each with the strongest "
            "real arguments and evidence. Then give a reasoned synthesis and where "
            "you land, acknowledging uncertainty. Keep it high-level and rigorous."
        )
        result = self._ask_saraswati(
            "You are a sharp, fair debate partner who argues both sides honestly.",
            question,
        )
        if not result:
            return ("Debating this well needs my cloud reasoning, which I can't reach "
                    "right now. Here are the current events bearing on it:\n\n" + (block or
                    "(no headlines retrieved either — check the connection)"))
        self._save_cache(f"debate_{_slug(proposition)}", f"# {proposition}\n\n{result}")
        return result

    # === Vault cache ======================================================

    def _save_cache(self, name: str, text: str):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(os.path.join(self.cache_dir, f"{name}.md"), "w", encoding="utf-8") as f:
                f.write(f"<!-- BRIHASPATI · {stamp} -->\n\n{text}\n")
        except Exception as e:
            print(f"[BRIHASPATI] cache write failed: {e}")

    def _load_cache(self, name: str) -> str:
        try:
            path = os.path.join(self.cache_dir, f"{name}.md")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass
        return ""


# === helpers ==============================================================

def _clean(html: str) -> str:
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", text).strip()


def _ago(ts: float) -> str:
    if not ts:
        return ""
    try:
        delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, timezone.utc)
        mins = int(delta.total_seconds() // 60)
        if mins < 1:
            return ", just now"
        if mins < 60:
            return f", {mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f", {hours}h ago"
        return f", {hours // 24}d ago"
    except Exception:
        return ""


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:60] or "topic"
