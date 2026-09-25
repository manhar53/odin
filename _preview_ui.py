# Temporary: standalone orrery preview. Boots ONLY the ASGARD UI in a
# pywebview window with a stub bridge + simulated activity, so you can
# play with the cosmos without starting all of ODIN. Close window to exit.
import json
import random
import threading
import time
from pathlib import Path

import webview

URL = Path("output/asgard_ui/index.html").resolve().as_uri()

# Simulated module dispatches so beams/flashes/ticker feel alive.
DEMO_EVENTS = [
    ("SESHAT", "learn_link"), ("THOTH", "store_message"), ("HERMES", "recall"),
    ("SARASWATI", "deep_research"), ("IRIS", "speak_text"), ("THOR", "get_system_stats"),
    ("CHRONOS", "set_reminder"), ("GANESH", "excel_summarize"), ("GILGAMESH", "think"),
    ("HEPHAESTUS", "code_search"), ("OGMA", "translate"), ("KARN", "scan"),
]

# Stub skills so the dossier panel has something real-looking to show.
STUB_SKILLS = {
    "default": [{"name": "preview_mode", "description": "Demo window — boot ODIN for live skill data."}],
}


class StubApi:
    def on_click(self):
        return "ok"   # no phantom in preview — window stays opaque

    def submit_text(self, text):
        print(f"[PREVIEW] command bar: {text}")
        return "ok"

    def on_drop(self, payload_json):
        p = json.loads(payload_json or "{}")
        names = ", ".join(f.get("name", "?") for f in p.get("files", [])) or p.get("url") or "text"
        print(f"[PREVIEW] drop on {p.get('layer')}: {names}")
        return f"PREVIEW — would route {names} to {p.get('layer')}"

    def get_module_info(self, name):
        return json.dumps({"name": name, "skills": STUB_SKILLS["default"]})


def demo_loop(win):
    time.sleep(2.0)
    win.evaluate_js("odin.setState('idle')")
    states = ["listening", "thinking", "speaking", "idle"]
    si = 0
    while True:
        time.sleep(2.8)
        try:
            mod, skill = random.choice(DEMO_EVENTS)
            win.evaluate_js(f"odin.moduleActive({json.dumps(mod)}, {json.dumps(skill)})")
            if random.random() < 0.45:
                s = states[si % len(states)]
                si += 1
                if s == "speaking":
                    win.evaluate_js(
                        "odin.beginTurn(); odin.setState('speaking', "
                        "{text: 'Preview mode — the orrery breathes, but my mind sleeps.', append: false})"
                    )
                    for _ in range(10):
                        win.evaluate_js(f"odin.setMouthLevel({random.uniform(0.2, 0.95):.2f})")
                        time.sleep(0.12)
                    win.evaluate_js("odin.resetMouth()")
                else:
                    win.evaluate_js(f"odin.setState({json.dumps(s)})")
        except Exception:
            break   # window closed


win = webview.create_window(
    "ODIN — Orrery Preview", url=URL,
    width=1440, height=900, background_color="#05070f",
    js_api=StubApi(),
)
threading.Thread(target=demo_loop, args=(win,), daemon=True).start()
webview.start()
print("[PREVIEW] window closed.")
