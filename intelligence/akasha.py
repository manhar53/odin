# AKASHA — Sanskrit / Hindu — the ether, the cosmic record that holds all
# knowledge. In ODIN she is the gateway to free-tier paid APIs that fetch
# facts but are not AI themselves: Google Custom Search, NewsAPI, YouTube
# Data, etc. These supplement DuckDuckGo / Wikipedia (already in ATHENA)
# when the user has keys.
#
# Each provider degrades gracefully when its key is missing — ODIN remains
# fully functional with zero AKASHA keys configured. Quota tracking is
# light (just a counter); the upstream APIs enforce their own daily limits.

import os
from urllib.parse import quote_plus
import requests
from core.marduk import OdinModule

try:
    from firecrawl import Firecrawl
    _HAS_FIRECRAWL = True
except ImportError:
    _HAS_FIRECRAWL = False

try:
    from crawl4ai import AsyncWebCrawler
    _HAS_CRAWL4AI = True
except ImportError:
    _HAS_CRAWL4AI = False


class Akasha(OdinModule):
    MODULE_NAME = "AKASHA"
    LAYER = "INTELLIGENCE"

    def __init__(self, config: dict):
        super().__init__(config)
        cfg = config.get("akasha", {})
        # Google Programmable Search Engine — 100 free queries/day per key.
        # Needs BOTH the API key AND the search-engine CX id.
        self.google_key = cfg.get("google_search_api_key") or os.environ.get("GOOGLE_SEARCH_API_KEY", "")
        self.google_cx  = cfg.get("google_search_engine_id") or os.environ.get("GOOGLE_SEARCH_CX", "")
        # NewsAPI.org — 100 free requests/day on the developer tier.
        self.news_key   = cfg.get("news_api_key") or os.environ.get("NEWS_API_KEY", "")
        # YouTube Data API — 10,000 units/day (each search costs 100 units = 100 searches/day).
        # Uses the same Google API key; just needs YouTube Data API v3 enabled.
        self.youtube_key = cfg.get("youtube_api_key") or os.environ.get("YOUTUBE_API_KEY", self.google_key)
        # Firecrawl — converts any webpage to clean Markdown for LLM ingestion.
        # Free tier ~500 pages/month. Massive upgrade over snippet-only search:
        # SARASWATI.deep_research can now pull whole source pages, not summaries.
        self.firecrawl_key = (cfg.get("firecrawl_api_key")
                              or os.environ.get("FIRECRAWL_API_KEY", "")).strip()
        self.firecrawl = None
        if _HAS_FIRECRAWL and self.firecrawl_key:
            try:
                self.firecrawl = Firecrawl(api_key=self.firecrawl_key)
            except Exception as e:
                print(f"[AKASHA] Firecrawl init failed: {e}")
        # Scraper preference order — fetch_page walks this list and uses the
        # first available backend. Default: crawl4ai (free, local, no quota)
        # → firecrawl (paid, slightly faster) → http (offline fallback).
        # Strip whitespace/duplicates while preserving order, drop unknowns.
        raw_order = cfg.get("scraper_order", ["crawl4ai", "firecrawl", "http"])
        if isinstance(raw_order, str):
            raw_order = [s.strip() for s in raw_order.split(",")]
        valid = {"crawl4ai", "firecrawl", "http"}
        seen = set()
        self.scraper_order = []
        for b in raw_order:
            b = (b or "").strip().lower()
            if b in valid and b not in seen:
                self.scraper_order.append(b)
                seen.add(b)
        if not self.scraper_order:
            self.scraper_order = ["http"]

        active = [name for name, present in [
            ("google_search", bool(self.google_key and self.google_cx)),
            ("news_api",      bool(self.news_key)),
            ("youtube",       bool(self.youtube_key)),
            ("crawl4ai",      bool(_HAS_CRAWL4AI)),
            ("firecrawl",     bool(self.firecrawl)),
        ] if present]
        if active:
            print(f"[AKASHA] Free APIs online: {', '.join(active)}.")
        else:
            print("[AKASHA] No free API keys configured. Set GOOGLE_SEARCH_API_KEY / "
                  "NEWS_API_KEY / YOUTUBE_API_KEY (env or config) to enable.")

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "google_search",
                "description": "Search Google for current results (better than DuckDuckGo for many topics). Requires a free Google Programmable Search Engine key.",
                "parameters": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Number of results (default 5, max 10)"}
                },
                "required": ["query"]
            },
            {
                "name": "youtube_search",
                "description": "Search YouTube for videos by topic. Returns title, channel, and URL.",
                "parameters": {
                    "query": {"type": "string", "description": "Video topic"},
                    "limit": {"type": "integer", "description": "Number of results (default 5)"}
                },
                "required": ["query"]
            },
            {
                "name": "news_headlines",
                "description": "Get the latest top news headlines.",
                "parameters": {
                    "country": {"type": "string", "description": "ISO country code (us, gb, in...). Default 'us'."},
                    "category": {"type": "string", "description": "Category filter: business, technology, sports, etc. Default mixed."}
                },
                "required": []
            },
            {
                "name": "news_about",
                "description": "Search news articles about a specific topic.",
                "parameters": {
                    "topic": {"type": "string", "description": "Topic to find news about"}
                },
                "required": ["topic"]
            },
            {
                "name": "http_request",
                "description": (
                    "Make an arbitrary HTTP request to any public API. Use this "
                    "when no built-in skill covers the endpoint you need (the "
                    "n8n-style escape hatch). Returns the response body, "
                    "truncated to 8 KB. SAFETY: localhost / private IPs are "
                    "blocked (SSRF prevention). Non-GET methods (POST/PUT/"
                    "DELETE/PATCH) require 'confirmed' in the caller's intent. "
                    "Auth header values are masked in logs."
                ),
                "parameters": {
                    "url":     {"type": "string", "description": "Full URL with scheme"},
                    "method":  {"type": "string", "description": "GET / POST / PUT / DELETE / PATCH (default GET)"},
                    "headers": {"type": "string", "description": "Comma-separated 'Key: Value' pairs (optional)"},
                    "body":    {"type": "string", "description": "Request body — JSON string for application/json (optional)"},
                    "params":  {"type": "string", "description": "Comma-separated 'k=v' query parameters (optional)"},
                    "confirmed": {"type": "boolean", "description": "Must be true for POST/PUT/DELETE/PATCH"},
                },
                "required": ["url"],
            },
            {
                "name": "fetch_page",
                "description": (
                    "Fetch a webpage and return its main content as clean Markdown via "
                    "Firecrawl. Use this when you have a specific URL and need the FULL "
                    "page (article body, docs, gated content), not just a search snippet. "
                    "Falls back to a simple HTTP+strip when no Firecrawl key is configured."
                ),
                "parameters": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Trim output to this many chars (default 4000)"},
                },
                "required": ["url"],
            },
            {
                "name": "web_search_deep",
                "description": (
                    "Web search that returns full-page Markdown bodies, not snippets. "
                    "Use for research where snippets aren't enough. Requires Firecrawl key."
                ),
                "parameters": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Number of pages to fetch (default 3, max 5)"},
                },
                "required": ["query"],
            },
            {
                "name": "find_api",
                "description": (
                    "Search the public-apis catalog (github.com/public-apis/public-apis, "
                    "~1400 free APIs) by keyword or category — e.g. 'weather', 'currency', "
                    "'jokes', 'books'. Returns name, description, auth requirement, and URL "
                    "for the top matches. Use together with http_request to actually call "
                    "the API. Catalog is cached locally after first fetch, so this works "
                    "offline. Pass query='categories' to list all categories."
                ),
                "parameters": {
                    "query": {"type": "string", "description": "Keyword or category to search for ('categories' lists all)"},
                    "limit": {"type": "integer", "description": "Max results (default 6, max 15)"},
                    "refresh": {"type": "boolean", "description": "Force re-download of the catalog (default false)"},
                },
                "required": ["query"],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "google_search":  self._google_search,
            "youtube_search": self._youtube_search,
            "news_headlines": self._news_headlines,
            "news_about":     self._news_about,
            "fetch_page":     self._fetch_page,
            "web_search_deep": self._web_search_deep,
            "http_request":   self._http_request,
            "find_api":       self._find_api,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[AKASHA] Error: {e}"
        return f"[AKASHA] Unknown skill: {skill_name}"

    # === Google Programmable Search ====================================
    def _google_search(self, query: str = "", limit: int = 5) -> str:
        if not self.google_key or not self.google_cx:
            return "Google Search not configured. Need GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_CX (or akasha.google_search_engine_id)."
        try:
            limit = max(1, min(10, int(limit or 5)))
        except (TypeError, ValueError):
            limit = 5
        try:
            r = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": self.google_key, "cx": self.google_cx,
                        "q": query, "num": limit},
                timeout=6,
            )
            if r.status_code == 429:
                return "Google Search daily quota exceeded (100/day on free tier)."
            r.raise_for_status()
            items = r.json().get("items", []) or []
            if not items:
                return f"No Google results for: {query}"
            parts = [f"{it['title']}: {it.get('snippet','')[:200]}" for it in items]
            out = " | ".join(parts)
            self._cache(f"google:{query}", out[:400])
            return out
        except Exception as e:
            return f"Google Search failed: {e}"

    # === YouTube Data API ==============================================
    def _youtube_search(self, query: str = "", limit: int = 5) -> str:
        if not self.youtube_key:
            return "YouTube API not configured. Need YOUTUBE_API_KEY."
        try:
            limit = max(1, min(10, int(limit or 5)))
        except (TypeError, ValueError):
            limit = 5
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params={"key": self.youtube_key, "part": "snippet",
                        "q": query, "type": "video", "maxResults": limit},
                timeout=6,
            )
            if r.status_code == 403:
                return "YouTube API quota exceeded or key invalid."
            r.raise_for_status()
            items = r.json().get("items", []) or []
            if not items:
                return f"No YouTube results for: {query}"
            parts = []
            for it in items:
                vid = it["id"]["videoId"]
                title = it["snippet"]["title"]
                channel = it["snippet"]["channelTitle"]
                parts.append(f"{title} ({channel}) https://youtu.be/{vid}")
            out = " | ".join(parts)
            self._cache(f"youtube:{query}", out[:400])
            return out
        except Exception as e:
            return f"YouTube search failed: {e}"

    # === NewsAPI =======================================================
    def _news_headlines(self, country: str = "us", category: str = "") -> str:
        if not self.news_key:
            return "NewsAPI not configured. Need NEWS_API_KEY."
        try:
            params = {"apiKey": self.news_key, "country": (country or "us")[:2].lower(),
                      "pageSize": 5}
            if category:
                params["category"] = category.lower()
            r = requests.get("https://newsapi.org/v2/top-headlines", params=params, timeout=6)
            r.raise_for_status()
            articles = r.json().get("articles", []) or []
            if not articles:
                return "No headlines returned."
            parts = [f"{a['title']} — {a.get('source',{}).get('name','')}" for a in articles[:5]]
            out = " | ".join(parts)
            self._cache(f"news:headlines:{country}:{category}", out[:400])
            return out
        except Exception as e:
            return f"News fetch failed: {e}"

    def _news_about(self, topic: str = "") -> str:
        if not self.news_key:
            return "NewsAPI not configured. Need NEWS_API_KEY."
        try:
            r = requests.get(
                "https://newsapi.org/v2/everything",
                params={"apiKey": self.news_key, "q": topic, "pageSize": 5,
                        "sortBy": "publishedAt", "language": "en"},
                timeout=6,
            )
            r.raise_for_status()
            articles = r.json().get("articles", []) or []
            if not articles:
                return f"No recent news on '{topic}'."
            parts = [f"{a['title']} ({a.get('source',{}).get('name','')})" for a in articles[:5]]
            out = " | ".join(parts)
            self._cache(f"news:{topic}", out[:400])
            return out
        except Exception as e:
            return f"News search failed: {e}"

    # === Firecrawl: full-page Markdown extraction ======================
    def _fetch_page(self, url: str = "", max_chars: int = 4000) -> str:
        url = (url or "").strip()
        if not url:
            return "Need a URL."
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            max_chars = max(500, min(20000, int(max_chars or 4000)))
        except (TypeError, ValueError):
            max_chars = 4000

        # Scraper order from config (default: crawl4ai → firecrawl → http).
        # crawl4ai is free + local (no quota); firecrawl is paid (~500/mo free)
        # but a touch faster per page. HTTP is the offline last resort.
        order = self.scraper_order

        for backend in order:
            if backend == "crawl4ai" and _HAS_CRAWL4AI:
                md = self._fetch_via_crawl4ai(url)
                if md:
                    self._cache(f"crawl4ai:{url}", md[:400])
                    return md[:max_chars]
            elif backend == "firecrawl" and self.firecrawl:
                md = self._fetch_via_firecrawl(url)
                if md:
                    self._cache(f"firecrawl:{url}", md[:400])
                    return md[:max_chars]
            elif backend == "http":
                text = self._fetch_via_http(url)
                if text:
                    return text[:max_chars] + ("..." if len(text) > max_chars else "")

        return f"Could not extract content from {url} via any configured scraper ({', '.join(order)})."

    # ── Scraper backends ─────────────────────────────────────────────
    def _fetch_via_crawl4ai(self, url: str) -> str:
        """Local Playwright-driven scraper. Returns Markdown or empty string.
        Logs a setup hint the first time the Chromium browsers are missing."""
        if not _HAS_CRAWL4AI:
            return ""
        try:
            import asyncio
            async def _go():
                async with AsyncWebCrawler(verbose=False) as c:
                    r = await c.arun(url=url)
                    return (r.markdown or "").strip()
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Nested loop — rare in ODIN but possible inside async
                    # contexts. asyncio.run would crash; fall through.
                    return ""
            except RuntimeError:
                pass
            return asyncio.run(_go())
        except Exception as e:
            err = str(e).lower()
            if "playwright" in err or "browser" in err or "executable" in err:
                self._log.warning(
                    "crawl4ai needs Chromium — run `python setup_crawl4ai.py install` once "
                    "(~300 MB). Falling back to firecrawl/HTTP for now."
                )
            else:
                self._log.warning(f"crawl4ai failed for {url}: {e}")
            return ""

    def _fetch_via_firecrawl(self, url: str) -> str:
        if not self.firecrawl:
            return ""
        try:
            doc = self.firecrawl.scrape(url=url, formats=["markdown"], only_main_content=True)
            md = (getattr(doc, "markdown", None)
                  or (doc.get("markdown") if isinstance(doc, dict) else None)
                  or "")
            return (md or "").strip()
        except Exception as e:
            self._log.warning(f"firecrawl failed for {url}: {e}")
            return ""

    def _fetch_via_http(self, url: str) -> str:
        try:
            r = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0 (compatible; ODIN/1.0)",
            })
            r.raise_for_status()
            import re as _re
            text = _re.sub(r"<script\b[^>]*>.*?</script>", " ", r.text, flags=_re.S | _re.I)
            text = _re.sub(r"<style\b[^>]*>.*?</style>",  " ", text, flags=_re.S | _re.I)
            text = _re.sub(r"<[^>]+>", " ", text)
            text = _re.sub(r"\s+", " ", text).strip()
            return text
        except Exception as e:
            self._log.warning(f"http fallback failed for {url}: {e}")
            return ""

    def _web_search_deep(self, query: str = "", limit: int = 3) -> str:
        query = (query or "").strip()
        if not query:
            return "Need a query."
        try:
            limit = max(1, min(5, int(limit or 3)))
        except (TypeError, ValueError):
            limit = 3
        if not self.firecrawl:
            return ("Firecrawl not configured — set akasha.firecrawl_api_key or "
                    "FIRECRAWL_API_KEY env. For snippet-only search, use google_search "
                    "or search_web instead.")
        try:
            result = self.firecrawl.search(
                query=query, limit=limit,
                scrape_options={"formats": ["markdown"], "only_main_content": True},
            )
            # Firecrawl v4 returns a result object with .web (list of items).
            items = (getattr(result, "web", None)
                     or (result.get("web") if isinstance(result, dict) else None)
                     or [])
            if not items:
                return f"No deep-search results for: {query}"
            chunks = []
            for it in items[:limit]:
                title = getattr(it, "title", None) or it.get("title", "") if hasattr(it, "get") else getattr(it, "title", "")
                url = getattr(it, "url", None) or (it.get("url", "") if hasattr(it, "get") else "")
                md = getattr(it, "markdown", None) or (it.get("markdown", "") if hasattr(it, "get") else "") or ""
                chunks.append(f"## {title}\n{url}\n\n{md[:1500].strip()}")
            out = "\n\n---\n\n".join(chunks)
            self._cache(f"firecrawl_search:{query}", out[:400])
            return out
        except Exception as e:
            return f"Deep search failed: {e}"

    def _cache(self, topic: str, summary: str):
        try:
            self.send("NABU", "cache_lookup", topic=topic, summary=summary)
        except Exception:
            pass

    # === Universal HTTP — n8n-style escape hatch =======================
    # Lets the LLM hit ANY public API without us having to pre-build a
    # skill for it. With SSRF + destructive-method guards.
    def _http_request(self, url: str = "", method: str = "GET",
                       headers: str = "", body: str = "", params: str = "",
                       confirmed: bool = False) -> str:
        url = (url or "").strip()
        method = (method or "GET").strip().upper()
        if not url:
            return "Need a URL."
        if not url.startswith(("http://", "https://")):
            return f"Refusing non-http(s) scheme: {url[:40]}"
        # Block local/private IP ranges — SSRF / loopback / link-local /
        # cloud metadata service. The bound LLM can't drive ODIN to scan
        # the user's intranet.
        bad, reason = _check_ssrf(url)
        if bad:
            return f"Refusing private/localhost URL: {reason}"
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            return f"Unsupported method: {method}"
        # Destructive-method guard. POST/PUT/DELETE/PATCH require explicit
        # user consent (cascades from the caller's destructive-intent check,
        # if there is one — for direct LLM dispatch through MARDUK, we
        # require an explicit confirmed=true).
        if method in ("POST", "PUT", "DELETE", "PATCH") and not confirmed:
            return (f"{method} requires confirmed=true. Re-issue with explicit "
                    f"consent if you really want to send a {method}.")
        # Parse headers / params (best-effort tolerant).
        hdrs: dict[str, str] = {"User-Agent": "ODIN/1.0"}
        for line in (headers or "").split(","):
            line = line.strip()
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip()] = v.strip()
        param_dict: dict[str, str] = {}
        for kv in (params or "").split(","):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                param_dict[k.strip()] = v.strip()
        # JSON body auto-detect: if body looks like JSON and no Content-Type
        # is set, add application/json.
        req_kwargs: dict = {"timeout": 8, "headers": hdrs}
        if param_dict:
            req_kwargs["params"] = param_dict
        if body and method in ("POST", "PUT", "PATCH"):
            body = body.strip()
            if (body.startswith("{") or body.startswith("[")) and "content-type" not in {k.lower() for k in hdrs}:
                hdrs["Content-Type"] = "application/json"
                try:
                    import json as _json
                    req_kwargs["json"] = _json.loads(body)
                except Exception:
                    req_kwargs["data"] = body
            else:
                req_kwargs["data"] = body
        # Masked log line
        masked_hdrs = {k: ("***" if k.lower() in ("authorization", "x-api-key", "cookie") else v)
                       for k, v in hdrs.items()}
        try:
            r = requests.request(method, url, **req_kwargs)
            text = (r.text or "")[:8192]
            status = f"{r.status_code} {r.reason}"
            self._log.info(f"http_request {method} {url} headers={masked_hdrs} → {status}")
            return f"{status}\n\n{text}"
        except requests.exceptions.Timeout:
            return f"HTTP {method} {url} timed out after 8s."
        except requests.exceptions.RequestException as e:
            return f"HTTP {method} {url} failed: {e}"

    # === public-apis catalog ============================================
    # github.com/public-apis/public-apis — community list of ~1400 free
    # APIs. We parse the README markdown tables into a JSON catalog cached
    # at data/knowledge/public_apis.json, so search is instant and offline
    # after the first fetch. Refreshed only on demand (refresh=true).
    _PUBLIC_APIS_README = "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
    _PUBLIC_APIS_CACHE = "data/knowledge/public_apis.json"

    def _load_api_catalog(self, refresh: bool = False) -> list[dict] | str:
        """Returns the catalog list, or an error string."""
        import json as _json
        if not refresh and os.path.exists(self._PUBLIC_APIS_CACHE):
            try:
                with open(self._PUBLIC_APIS_CACHE, "r", encoding="utf-8") as f:
                    return _json.load(f)
            except Exception:
                pass  # corrupt cache — re-fetch below
        try:
            r = requests.get(self._PUBLIC_APIS_README, timeout=15)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            return (f"public-apis catalog not cached yet and download failed ({e}). "
                    "Retry when online.")
        import re as _re
        catalog, category = [], ""
        row = _re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|")
        for line in r.text.splitlines():
            if line.startswith("###"):
                category = line.lstrip("#").strip()
                continue
            m = row.match(line)
            if m and category:
                catalog.append({
                    "name": m.group(1).strip(),
                    "url": m.group(2).strip(),
                    "description": m.group(3).strip(),
                    "auth": m.group(4).strip().strip("`") or "No",
                    "https": m.group(5).strip(),
                    "category": category,
                })
        if not catalog:
            return "Downloaded public-apis README but parsed 0 entries — format may have changed."
        try:
            with open(self._PUBLIC_APIS_CACHE, "w", encoding="utf-8") as f:
                _json.dump(catalog, f, ensure_ascii=False)
        except Exception as e:
            self._log.warning(f"public_apis cache write failed: {e}")
        return catalog

    def _find_api(self, query: str = "", limit: int = 6, refresh: bool = False) -> str:
        catalog = self._load_api_catalog(refresh=bool(refresh))
        if isinstance(catalog, str):
            return catalog
        try:
            limit = max(1, min(15, int(limit or 6)))
        except (TypeError, ValueError):
            limit = 6
        q = (query or "").strip().lower()
        if not q:
            return "Give me a keyword — e.g. find_api('weather')."
        if q in ("categories", "category", "list categories"):
            cats = sorted({e["category"] for e in catalog})
            return f"{len(cats)} categories: " + ", ".join(cats)
        # Rank: category match > name match > description match
        scored = []
        for e in catalog:
            score = 0
            if q in e["category"].lower():    score += 3
            if q in e["name"].lower():        score += 2
            if q in e["description"].lower(): score += 1
            if score:
                scored.append((score, e))
        if not scored:
            return f"No APIs matching '{query}' in the {len(catalog)}-entry catalog. Try 'categories' to browse."
        scored.sort(key=lambda t: -t[0])
        lines = [f"Top {min(limit, len(scored))} of {len(scored)} matches for '{query}':"]
        for _, e in scored[:limit]:
            auth = "no auth" if e["auth"].lower() in ("no", "") else f"auth: {e['auth']}"
            lines.append(f"- {e['name']} ({e['category']}, {auth}) — {e['description']} → {e['url']}")
        lines.append("Use http_request to call any of these.")
        return "\n".join(lines)


def _check_ssrf(url: str) -> tuple[bool, str]:
    """SSRF guard. Returns (is_blocked, reason). Blocks:
      - localhost / 127.x / ::1
      - RFC1918 private ranges
      - link-local / multicast / metadata services
    Resolves the host to an IP first so 'http://localtest.me' (resolves to
    127.0.0.1) is also caught."""
    from urllib.parse import urlparse
    import socket, ipaddress
    try:
        host = (urlparse(url).hostname or "").strip()
        if not host:
            return (True, "no host in URL")
        if host.lower() in ("localhost", "ip6-localhost", "ip6-loopback"):
            return (True, "localhost")
        # AWS / GCP metadata service IPs
        if host in ("169.254.169.254", "fd00:ec2::254", "metadata.google.internal"):
            return (True, "cloud metadata service")
        # Resolve to IP and check ranges
        try:
            ip_str = socket.gethostbyname(host)
            ip = ipaddress.ip_address(ip_str)
            if (ip.is_loopback or ip.is_private or ip.is_link_local
                    or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
                return (True, f"{host} resolves to private/reserved {ip_str}")
        except (socket.gaierror, ValueError):
            return (True, f"hostname {host!r} did not resolve")
    except Exception as e:
        return (True, f"URL parse error: {e}")
    return (False, "")
