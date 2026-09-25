"""Verify v4: volume routes, URL fallback, capped 'open' regex."""
import os, yaml
os.makedirs("data/memory", exist_ok=True)
os.makedirs("data/backups", exist_ok=True)
os.makedirs("data/logs", exist_ok=True)
os.makedirs("data/knowledge", exist_ok=True)

from input.heimdall import _FAST_ROUTES, _CAPABILITY_QUESTION
from output.thor import _KNOWN_URLS, _APP_ALIASES, _HAS_PYCAW

cases = [
    # Volume
    ("Set volume to 50",                "set_volume", {"level": 50}),
    ("Set volume to 50 percent",        "set_volume", {"level": 50}),
    ("Volume to 75",                    "set_volume", {"level": 75}),
    ("Volume up",                       "volume_up", {}),
    ("Volume down",                     "volume_down", {}),
    ("Mute",                            "mute", {}),
    ("What is the volume",              "get_volume", {}),
    # Brightness still works
    ("Set brightness to 60",            "set_brightness", {"level": 60}),
    ("What is the brightness",          "get_brightness", {}),
    # Open with capped capture
    ("Open chrome",                     "open_application", {"app_name": "chrome"}),
    ("Open vs code",                    "open_application", {"app_name": "vs code"}),
    ("Open visual studio code",         "open_application", {"app_name": "visual studio code"}),
    ("Open youtube",                    "open_application", {"app_name": "youtube"}),
    # Named-folder open now has its own fast-route (find_and_open)
    ("Open a folder named Project Files on desktop",  "find_and_open", {"name": "Project Files", "location": "desktop"}),
    # Should fall through (>3 words, no folder pattern)
    ("Open chrome for profile work at the gmail",  None, None),
]

print("V4 ROUTE CHECK:")
ok = 0
for cmd, expected_skill, expected_args in cases:
    matched = None
    for pattern, skill, args_fn in _FAST_ROUTES:
        m = pattern.search(cmd.strip())
        if m:
            try:
                args = args_fn(m)
            except Exception as e:
                args = f"<err: {e}>"
            matched = (skill, args)
            break
    status = "PASS"
    if expected_skill is None:
        if matched is None:
            mark = "[PASS]"
        else:
            mark = "[FAIL]"
            status = "FAIL"
    else:
        if matched and matched[0] == expected_skill and matched[1] == expected_args:
            mark = "[PASS]"
        else:
            mark = "[FAIL]"
            status = "FAIL"
    if status == "PASS":
        ok += 1
    desc = f"{matched[0]}({matched[1]})" if matched else "no match -> LLM"
    print(f"  {mark} '{cmd}' -> {desc}")

print()
print(f"Routes correct: {ok}/{len(cases)}")
print(f"Known URLs: {len(_KNOWN_URLS)} (youtube, gmail, github, ...)")
print(f"App aliases: {len(_APP_ALIASES)}")
print(f"pycaw available: {_HAS_PYCAW}")
