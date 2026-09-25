"""Download a Kiwix Wikipedia .zim archive for ATHENA's offline path.

Why: ATHENA's wiki_lookup currently hits Wikipedia's REST API. With a .zim
archive on disk, ODIN can answer wiki questions when the network is down.
The libzim Python bindings are already installed; this script only fetches
the data file.

  python setup_kiwix.py list                 # show recommended archives + sizes
  python setup_kiwix.py download simple      # ~9 GB Simple English (good starting point)
  python setup_kiwix.py download nopic       # ~50 GB Full English no images
  python setup_kiwix.py download maxi        # ~100 GB Full English with images
  python setup_kiwix.py download <URL>       # arbitrary .zim URL
  python setup_kiwix.py set <path>           # point ATHENA at an already-downloaded .zim

After download, the script updates athena.kiwix_zim_path in config.yaml.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


KIWIX_DIR = Path("data/kiwix")
CONFIG_PATH = Path("config.yaml")

# Kiwix archive URLs — these update monthly; check library.kiwix.org for newer.
# The URLs point at the YYYY-MM tagged drop, not the symlinked "latest" that
# can rotate without warning. Update these manually when a new dump lands.
ARCHIVES = {
    "simple": {
        "name": "wikipedia_en_simple_all_maxi_2024-06.zim",
        "url":  "https://download.kiwix.org/zim/wikipedia/wikipedia_en_simple_all_maxi_2024-06.zim",
        "size": "~9 GB",
        "desc": "Simple English Wikipedia, all articles, with images.",
    },
    "nopic": {
        "name": "wikipedia_en_all_nopic_2024-06.zim",
        "url":  "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_nopic_2024-06.zim",
        "size": "~50 GB",
        "desc": "Full English Wikipedia, no images. Good text-only depth.",
    },
    "maxi": {
        "name": "wikipedia_en_all_maxi_2024-06.zim",
        "url":  "https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_maxi_2024-06.zim",
        "size": "~100 GB",
        "desc": "Full English Wikipedia with images. Disk-hungry; only pick this if you have the space.",
    },
}


def _set_config_path(zim_path: str):
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    cfg.setdefault("athena", {})["kiwix_zim_path"] = str(Path(zim_path).resolve()).replace("\\", "/")
    CONFIG_PATH.write_text(
        yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"  Updated config.yaml: athena.kiwix_zim_path = {zim_path}")


def list_archives():
    print("Recommended Kiwix archives (English Wikipedia variants):")
    print()
    for key, meta in ARCHIVES.items():
        print(f"  {key:<8}  {meta['size']:<8}  {meta['name']}")
        print(f"            → {meta['desc']}")
    print()
    print("Browse more at:  https://library.kiwix.org/")
    print()
    print("Run `python setup_kiwix.py download <key>` to fetch one of the above,")
    print("or `python setup_kiwix.py download <URL>` for any .zim URL.")


def _download(url: str, dest: Path):
    """Try curl first (gives a real progress bar), fall back to urllib."""
    KIWIX_DIR.mkdir(parents=True, exist_ok=True)
    if shutil.which("curl"):
        print(f"  curl-ing {url}")
        rc = subprocess.run(["curl", "-L", "-o", str(dest), url]).returncode
        if rc == 0:
            return
        print(f"  curl exited {rc}, retrying with urllib...")
    import urllib.request
    print(f"  urllib downloading {url}  (this can take a while; no progress bar)")
    urllib.request.urlretrieve(url, dest)


def download(arg: str):
    if arg in ARCHIVES:
        meta = ARCHIVES[arg]
        url = meta["url"]
        name = meta["name"]
        print(f"Downloading {name} ({meta['size']}) — get a coffee.")
    elif arg.startswith(("http://", "https://")):
        url = arg
        name = url.rstrip("/").rsplit("/", 1)[-1] or "wiki.zim"
        print(f"Downloading {url}")
    else:
        print(f"Unknown archive key {arg!r}. Run `python setup_kiwix.py list`.")
        sys.exit(1)

    dest = KIWIX_DIR / name
    if dest.exists():
        size_gb = dest.stat().st_size / (1024 ** 3)
        print(f"  Already present at {dest} ({size_gb:.2f} GB). Skipping download.")
    else:
        _download(url, dest)

    if not dest.exists():
        print(f"  Download did not produce {dest}. Aborting config update.")
        sys.exit(2)

    print()
    print(f"  Verifying with libzim...")
    try:
        from libzim.reader import Archive
        a = Archive(dest)
        print(f"  OK — archive has {a.entry_count} entries.")
    except Exception as e:
        print(f"  libzim could not open the archive ({e}). config NOT updated.")
        sys.exit(2)

    _set_config_path(str(dest))
    print()
    print("Done. Restart ODIN — ATHENA's wiki_lookup will now use the .zim when")
    print("the network is unreachable. Set athena.kiwix_first: true in config.yaml")
    print("to prefer the offline path over the live REST API.")


def set_path(path: str):
    p = Path(path)
    if not p.exists():
        print(f"File not found: {p}")
        sys.exit(1)
    try:
        from libzim.reader import Archive
        a = Archive(p)
        print(f"  Verified: {a.entry_count} entries.")
    except Exception as e:
        print(f"  libzim could not open the archive ({e}).")
        sys.exit(2)
    _set_config_path(str(p))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "list":
        list_archives()
    elif cmd == "download":
        if len(sys.argv) < 3:
            print("Need an archive key or URL. Try `python setup_kiwix.py list`.")
            sys.exit(1)
        download(sys.argv[2])
    elif cmd == "set":
        if len(sys.argv) < 3:
            print("Need a path. Example: python setup_kiwix.py set D:/wiki/simple.zim")
            sys.exit(1)
        set_path(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
