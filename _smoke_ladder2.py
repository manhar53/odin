# Throwaway — proves the escalation BRANCH fires: fast model stubbed to punt,
# R1 rung goes out live. Delete after.
import os, subprocess, yaml

key = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "(Get-ItemProperty HKCU:\\Environment -Name NVIDIA_API_KEY).NVIDIA_API_KEY"],
    capture_output=True, text=True).stdout.strip()
os.environ["NVIDIA_API_KEY"] = key

from intelligence.saraswati import Saraswati

cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
s = Saraswati(cfg)

calls = []
real = s._nvidia_chat
def stubbed(model, system, user_prompt, max_tokens):
    calls.append((model, max_tokens))
    if model == s.models["nvidia"]:
        return "I don't know."          # force the punt
    return real(model, system, user_prompt, max_tokens)
s._nvidia_chat = stubbed

text = s._call_provider("nvidia", "You are a careful solver.",
                        "A bat and a ball cost $1.10 total. The bat costs $1.00 "
                        "more than the ball. What does the ball cost?", 300)
print("models called:", calls)
print("answer:", text.strip()[:300])
assert len(calls) == 2 and calls[1][0] == "deepseek-ai/deepseek-v4-pro", "ladder did not escalate"
assert calls[1][1] >= 1200, "token budget not raised"
assert "0.05" in text or "5 cent" in text.lower(), "R1 wrong or think-strip broke"
assert "<think>" not in text, "think trace leaked"
print("ladder branch verified: punt -> R1 -> clean answer")
