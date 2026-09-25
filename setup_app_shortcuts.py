"""ODIN app-shortcut installer.

Drops a clickable "ODIN" shortcut on the user's Desktop and in the Start menu,
both pointing at the silent .vbs launcher used by setup_autostart.py. Now
"running ODIN" is the same as launching any normal Windows app: double-click
the desktop icon, or Win + type "ODIN" to find it in the Start menu.

Reuses the .vbs wrapper written by setup_autostart.py so we don't duplicate
the silent-pythonw plumbing.

  python setup_app_shortcuts.py install      # create both shortcuts
  python setup_app_shortcuts.py uninstall    # remove both
  python setup_app_shortcuts.py status       # show what's where

If you also want ODIN to launch on Windows login, run:
  python setup_autostart.py enable
That's a separate concern — these shortcuts are for on-demand launching.
"""

import os
import sys
from pathlib import Path

# Reuse the silent-launcher plumbing from setup_autostart so there's exactly
# one place that knows how to spawn pythonw + main.py without a console.
from setup_autostart import _write_vbs, VBS_PATH, ROOT

DESKTOP_SHORTCUT_NAME   = "ODIN.lnk"
STARTMENU_SHORTCUT_NAME = "ODIN.lnk"


def _desktop_path() -> Path:
    """Resolve the user's Desktop. Prefers OneDrive-redirected desktop if
    present (the common Windows 11 + Microsoft 365 configuration), falls
    back to the local one."""
    onedrive_desktop = Path(os.path.expanduser("~/OneDrive/Desktop"))
    if onedrive_desktop.is_dir():
        return onedrive_desktop
    return Path(os.path.expanduser("~/Desktop"))


def _startmenu_path() -> Path:
    """%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs is per-user."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA env var not set — are you on Windows?")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


def _desktop_shortcut() -> Path:
    return _desktop_path() / DESKTOP_SHORTCUT_NAME


def _startmenu_shortcut() -> Path:
    return _startmenu_path() / STARTMENU_SHORTCUT_NAME


def _candidate_icons():
    """Look for a real .ico in the assets folder; fall back to python.exe icon
    if nothing's there. (Drop your own ODIN.ico into output/asgard_ui/assets/
    and it'll be picked up automatically next time you run this.)"""
    candidates = [
        ROOT / "output" / "asgard_ui" / "assets" / "odin.ico",
        ROOT / "output" / "asgard_ui" / "assets" / "ODIN.ico",
        ROOT / "data" / "icons" / "odin.ico",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    return sys.executable + ",0"


def _create_shortcut(lnk_path: Path, target: str):
    """Create or overwrite a .lnk. pywin32 path is cleanest; the PowerShell
    fallback avoids any hard dep so the script works on fresh installs."""
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    icon = _candidate_icons()

    try:
        from win32com.client import Dispatch
        shell = Dispatch("WScript.Shell")
        s = shell.CreateShortcut(str(lnk_path))
        s.TargetPath = target
        s.WorkingDirectory = str(ROOT)
        s.Description = "ODIN — Omniscient Digital Intelligence Node"
        s.IconLocation = icon
        s.Save()
        return
    except ImportError:
        pass

    import subprocess
    ps = (
        f'$ws = New-Object -ComObject WScript.Shell; '
        f'$s = $ws.CreateShortcut("{lnk_path}"); '
        f'$s.TargetPath = "{target}"; '
        f'$s.WorkingDirectory = "{ROOT}"; '
        f'$s.Description = "ODIN"; '
        f'$s.IconLocation = "{icon}"; '
        f'$s.Save()'
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def install():
    print("Installing ODIN app shortcuts…")
    if not VBS_PATH.exists():
        vbs = _write_vbs()
        print(f"  Wrote launcher (was missing): {vbs}")
    else:
        print(f"  Reusing launcher: {VBS_PATH}")
    target = str(VBS_PATH)

    desk = _desktop_shortcut()
    _create_shortcut(desk, target)
    print(f"  Desktop shortcut:   {desk}")

    start = _startmenu_shortcut()
    _create_shortcut(start, target)
    print(f"  Start menu shortcut: {start}")

    print()
    print("Done. Launch ODIN by:")
    print("  • Double-clicking the 'ODIN' icon on your Desktop, or")
    print("  • Pressing Win and typing 'ODIN'.")


def uninstall():
    print("Removing ODIN app shortcuts…")
    removed = []
    for p in (_desktop_shortcut(), _startmenu_shortcut()):
        if p.exists():
            try:
                p.unlink()
                removed.append(str(p))
            except Exception as e:
                print(f"  Could not remove {p}: {e}")
    if removed:
        for r in removed:
            print(f"  Removed: {r}")
    else:
        print("  Nothing to remove (no shortcuts installed).")


def status():
    print("ODIN app shortcut status:")
    print(f"  Launcher .vbs:      {'EXISTS' if VBS_PATH.exists() else 'missing'}  ({VBS_PATH})")
    desk = _desktop_shortcut()
    start = _startmenu_shortcut()
    print(f"  Desktop shortcut:   {'EXISTS' if desk.exists() else 'missing'}  ({desk})")
    print(f"  Start menu shortcut:{'EXISTS' if start.exists() else 'missing'} ({start})")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall", "status"):
        print(__doc__)
        sys.exit(1)
    {
        "install":   install,
        "uninstall": uninstall,
        "status":    status,
    }[sys.argv[1]]()


if __name__ == "__main__":
    main()
