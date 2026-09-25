"""Set up crawl4ai's runtime (Playwright Chromium + crawl4ai assets).

crawl4ai itself is just a pip install. But to actually scrape pages it
needs:
  • Playwright's Chromium binary  (~300 MB, one-time)
  • crawl4ai-setup which initializes a few internal caches

This script runs both. Safe to re-run — both commands are idempotent.

  python setup_crawl4ai.py install     # do both steps
  python setup_crawl4ai.py status      # show what's installed

Once both run successfully, AKASHA.fetch_page uses crawl4ai as the primary
scraper (free, local, no quota), with firecrawl as the secondary path.
Order is configurable via akasha.scraper_order in config.yaml.
"""

import os
import sys
import subprocess
from pathlib import Path


def _check_crawl4ai() -> bool:
    try:
        import crawl4ai  # noqa
        return True
    except ImportError:
        return False


def _check_chromium() -> bool:
    """Look for Playwright's Chromium executable. The exact path varies by
    OS + version, so we just sniff the standard install directory."""
    candidates = [
        Path(os.path.expanduser("~")) / "AppData" / "Local" / "ms-playwright",
        Path("/root/.cache/ms-playwright"),
        Path(os.path.expanduser("~/.cache/ms-playwright")),
    ]
    for root in candidates:
        if root.is_dir():
            for entry in root.iterdir():
                if entry.is_dir() and entry.name.startswith("chromium"):
                    return True
    return False


def install():
    if not _check_crawl4ai():
        print("crawl4ai not installed — running pip install first.")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "crawl4ai"],
            check=True,
        )
    else:
        print("crawl4ai already installed.")

    print()
    print("Installing Playwright Chromium (~300 MB, one-time)...")
    rc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
    ).returncode
    if rc != 0:
        print(f"  playwright install exited {rc} — check your network / Chromium download.")
        return

    print()
    print("Running crawl4ai-setup (initialises caches)...")
    rc = subprocess.run(
        [sys.executable, "-m", "crawl4ai.cli", "doctor"],
    ).returncode
    # crawl4ai's CLI module name changed across versions; fall back to direct command.
    if rc != 0:
        rc = subprocess.run(["crawl4ai-setup"]).returncode
    if rc != 0:
        print(f"  crawl4ai-setup non-zero exit ({rc}). Often harmless — try fetching a page anyway.")

    print()
    print("Done. AKASHA.fetch_page will now use crawl4ai as the primary scraper.")


def status():
    print("crawl4ai runtime status:")
    print(f"  crawl4ai package:      {'INSTALLED' if _check_crawl4ai() else 'missing (pip install crawl4ai)'}")
    print(f"  Playwright Chromium:   {'PRESENT' if _check_chromium() else 'missing (run install)'}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "status"):
        print(__doc__)
        sys.exit(1)
    {"install": install, "status": status}[sys.argv[1]]()


if __name__ == "__main__":
    main()
