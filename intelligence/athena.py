# ATHENA — Greek — goddess of wisdom and strategy
# Search: web search, research, fact finding

import os
import re
import requests
# `duckduckgo-search` was renamed to `ddgs` upstream; same DDGS API.
# Fall back to the old import if the user hasn't updated yet.
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from core.marduk import OdinModule

# Wikipedia REST API — free, no key, no library needed. The `wikipedia`
# pip package broke after Wikipedia's API change and returns JSON parse
# errors. Direct REST is more reliable and has lower latency anyway.
_WIKI_REST = "https://en.wikipedia.org/api/rest_v1/page/summary/"

# Kiwix offline Wikipedia. The user drops a .zim archive (download from
# https://library.kiwix.org/) at the path in config; ATHENA reads it via
# libzim when the REST API is unreachable. Lazy-loaded — module-level
# import only checks availability, archive opens on first offline call.
try:
    from libzim.reader import Archive as _ZimArchive
    from libzim.search import Searcher as _ZimSearcher, Query as _ZimQuery
    _HAS_LIBZIM = True
except ImportError:
    _HAS_LIBZIM = False


class Athena(OdinModule):
    MODULE_NAME = "ATHENA"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        # Tracks the most recent wiki topic so the "tell me more" follow-up
        # ("continue", "go on", "elaborate") can re-fetch with a longer extract
        # without the user re-stating the subject.
        self._last_wiki_topic: str = ""

        # Kiwix offline-Wikipedia fallback. User drops a .zim archive at the
        # configured path (download from https://library.kiwix.org/).
        # Examples:
        #   wikipedia_en_simple_all_2024-05.zim   ~9 GB    Simple English
        #   wikipedia_en_all_nopic_2024-05.zim    ~50 GB   Full English, no images
        #   wikipedia_en_all_maxi_2024-05.zim     ~100 GB  Full English with images
        # ATHENA tries the live REST API first; if it's unreachable (offline,
        # quota, transient outage), we fall through to the local .zim.
        acfg = config.get("athena", {})
        self.kiwix_zim_path = acfg.get("kiwix_zim_path", "")
        self.kiwix_first = bool(acfg.get("kiwix_first", False))
        self._zim_archive = None    # lazy-opened on first use

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "search_web",
                "description": "Search the web for information, news, or facts",
                "parameters": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            },
            {
                "name": "search_news",
                "description": "Search for recent news on a topic",
                "parameters": {
                    "topic": {"type": "string", "description": "News topic to search"}
                },
                "required": ["topic"]
            },
            {
                "name": "get_definition",
                "description": "Get a quick definition or explanation of a term",
                "parameters": {
                    "term": {"type": "string", "description": "Word or concept to define"}
                },
                "required": ["term"],
                "internal_only": True
            },
            {
                "name": "wiki_lookup",
                "description": "Look up a topic on Wikipedia and return a 2-3 sentence summary. Use for factual questions about real-world things ('what is quantum entanglement', 'who is Marie Curie', 'tell me about Norse mythology'). Sub-second; bypasses the LLM entirely. Result is also cached to the vault for offline recall.",
                "parameters": {
                    "topic": {"type": "string", "description": "Topic to look up"}
                },
                "required": ["topic"]
            },
            {
                "name": "book_lookup",
                "description": "Search Open Library for a book by title or title + author. Returns top 3 matches with author, year, and edition count. Free, no key.",
                "parameters": {
                    "title": {"type": "string", "description": "Book title (optionally + author name)"}
                },
                "required": ["title"]
            },
            {
                "name": "tell_me_more",
                "description": "Follow-up: re-fetches the LAST wiki topic with an expanded ~4-sentence summary. Triggered by 'tell me more', 'continue', 'elaborate', 'go on'. No args — the topic is remembered from the previous wiki_lookup.",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "search_web": self._search,
            "search_news": self._news,
            "get_definition": self._define,
            "wiki_lookup": self._wiki_lookup,
            "book_lookup": self._book_lookup,
            "tell_me_more": self._tell_me_more,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[ATHENA] Search error: {e}"
        return f"[ATHENA] Unknown skill: {skill_name}"

    def _search(self, query: str = "") -> str:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=3))
        if not results:
            return "No results found."
        parts = []
        for r in results:
            body = r.get("body", "")[:200]
            parts.append(f"{r['title']}: {body}")
        out = " | ".join(parts)
        self._cache(query, out[:400])
        return out

    def _news(self, topic: str = "") -> str:
        with DDGS() as ddgs:
            results = list(ddgs.news(topic, max_results=3))
        if not results:
            return f"No recent news found for: {topic}"
        parts = []
        for r in results:
            parts.append(f"{r['title']} ({r.get('source', '')})")
        out = "Recent news: " + "; ".join(parts)
        self._cache(f"news:{topic}", out[:400])
        return out

    def _define(self, term: str = "") -> str:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"define {term}", max_results=1))
        if results:
            body = results[0].get("body", "No definition found.")[:300]
            self._cache(f"definition:{term}", body)
            return body
        return f"Could not find a definition for '{term}'."

    def _cache(self, topic: str, summary: str):
        # Memoize the lookup to NABU so future questions on the same topic
        # can be answered offline. Failure is non-fatal — never break the search.
        try:
            self.send("NABU", "cache_lookup", topic=topic, summary=summary)
        except Exception:
            pass

    def _wiki_lookup(self, topic: str = "", sentences_max: int = 2) -> str:
        topic = (topic or "").strip()
        if not topic:
            return "Need a topic to look up."

        # Order of attempts: if kiwix_first AND a .zim is loaded, try offline
        # first (instant, fully local). Otherwise try the live REST API; on
        # network failure, fall back to .zim. This honors the "fully offline
        # core" rule — wiki_lookup keeps working with no internet.
        attempts = []
        if self.kiwix_first:
            attempts = [self._wiki_via_kiwix, self._wiki_via_rest]
        else:
            attempts = [self._wiki_via_rest, self._wiki_via_kiwix]

        last_msg = ""
        for attempt in attempts:
            summary, msg = attempt(topic)
            if summary:
                sentences = re.split(r'(?<=[.!?])\s+', summary)
                short = " ".join(sentences[:max(1, sentences_max)]).strip()
                self._cache(f"wikipedia:{topic}", short[:600])
                self._last_wiki_topic = topic
                return short
            if msg:
                last_msg = msg

        # Both paths failed — queue for cloud research and report the most
        # informative failure message.
        self._queue_for_research(topic)
        return last_msg or f"No Wikipedia entry found for '{topic}'. Queued for background research."

    def _wiki_via_rest(self, topic: str):
        """Returns (summary_text, user_facing_msg_on_failure). summary_text is
        empty when the call didn't yield usable content."""
        try:
            url = _WIKI_REST + requests.utils.quote(topic.replace(" ", "_"))
            r = requests.get(url, timeout=5,
                             headers={"User-Agent": "ODIN/1.0 (local voice assistant)"})
            if r.status_code == 404:
                return "", f"No Wikipedia page for '{topic}'. I'll research it in the background."
            if not r.ok:
                return "", f"Wikipedia returned status {r.status_code}."
            data = r.json()
            if data.get("type") == "disambiguation":
                return "", f"'{topic}' is ambiguous on Wikipedia. Queued for deeper research."
            extract = (data.get("extract") or "").strip()
            return extract, ""
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError):
            # Offline / DNS down — let the caller try the .zim path.
            return "", ""
        except Exception as e:
            return "", f"Wikipedia lookup failed: {e}"

    def _wiki_via_kiwix(self, topic: str):
        """Pull a summary from a local .zim archive. Returns empty string
        when no archive is configured or the topic isn't in the archive."""
        if not _HAS_LIBZIM or not self.kiwix_zim_path:
            return "", ""
        if not os.path.exists(self.kiwix_zim_path):
            return "", ""
        try:
            if self._zim_archive is None:
                from pathlib import Path as _Path
                self._zim_archive = _ZimArchive(_Path(self.kiwix_zim_path))
            archive = self._zim_archive
            # Full-text search via libzim — returns ranked entry paths.
            searcher = _ZimSearcher(archive)
            search = searcher.search(_ZimQuery().set_query(topic))
            paths = list(search.getResults(0, 1))
            if not paths:
                return "", f"No entry for '{topic}' in the offline archive."
            entry = archive.get_entry_by_path(paths[0])
            item = entry.get_item()
            content = bytes(item.content).decode("utf-8", errors="replace")
            # The article is HTML — strip to plain text using the same
            # logic the AKASHA scraper falls back to.
            text = re.sub(r"<script\b[^>]*>.*?</script>", " ", content, flags=re.S | re.I)
            text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:2000], ""
        except Exception as e:
            return "", f"Offline Wikipedia read failed: {e}"

    def _tell_me_more(self) -> str:
        """Follow-up after a wiki_lookup. Re-runs the lookup on the remembered
        topic with a 4-sentence cap (vs 2 for the first hit). If no topic has
        been looked up this session, prompts the user to name one."""
        if not self._last_wiki_topic:
            return "More about what? Ask me about someone or something first."
        return self._wiki_lookup(self._last_wiki_topic, sentences_max=4)

    def _queue_for_research(self, topic: str):
        """Hand a topic to SARASWATI's queue so SELENE researches it during
        the next idle window. Fire-and-forget — no spoken impact."""
        try:
            self.send("SARASWATI", "queue_research", topic=topic)
        except Exception:
            pass

    def _book_lookup(self, title: str = "") -> str:
        title = (title or "").strip()
        if not title:
            return "Need a book title."
        try:
            r = requests.get(
                "https://openlibrary.org/search.json",
                params={"q": title, "limit": 3, "fields": "title,author_name,first_publish_year,edition_count"},
                timeout=5,
            )
            r.raise_for_status()
            data = r.json()
            docs = data.get("docs") or []
        except Exception as e:
            return f"Book lookup failed: {e}"
        if not docs:
            return f"No books found for '{title}'."
        parts = []
        for d in docs[:3]:
            t = d.get("title", "Unknown")
            authors = ", ".join(d.get("author_name", [])[:2])
            year = d.get("first_publish_year", "?")
            editions = d.get("edition_count", 1)
            parts.append(f"'{t}' by {authors} ({year}, {editions} edition(s))")
        out = "; ".join(parts)
        self._cache(f"book:{title}", out[:400])
        return out
