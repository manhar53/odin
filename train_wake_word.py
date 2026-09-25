"""Train a custom wake word for ODIN — e.g. "hey odin" — without TensorFlow
or gigabyte datasets.

How it works (the lightweight, fully-local path):
  1. Synthesize the wake phrase MANY ways using ODIN's own Piper voices,
     with augmentation (speed, pitch, gain, noise, time-shift) for variety.
  2. Synthesize decoy phrases + silence + noise as negatives.
  3. Embed every 2-second clip with openWakeWord's own feature extractor
     (the same melspectrogram->embedding front-end its models use).
  4. Train a small scikit-learn classifier head on positives vs negatives,
     pick an operating threshold, and save it to data/wakewords/<slug>.pkl.

HEIMDALL loads that head when heimdall.wake_engine == "custom" (point
heimdall.custom_wake_model at the .pkl).

Usage:
  python train_wake_word.py                      # trains "hey odin"
  python train_wake_word.py "hey odin" "okay odin"  # phrase + extra variants
  # Optional: drop real recordings of yourself saying the phrase into
  #   data/wakewords/samples/<slug>/*.wav   — they're added as strong
  #   positives and dramatically improve real-world accuracy.

Quality note: ODIN ships only a couple of Piper voices, so a synthetic-only
model is decent but not Siri-grade. Adding 10-20 of your own recordings is
the single biggest accuracy boost. Or keep "hey jarvis" (openWakeWord's
pretrained model) which was trained on thousands of voices.
"""

import os
import sys
import glob
import wave
import numpy as np

import yaml

SR = 16000              # openWakeWord embedder rate
WIN = 2.0               # clip window seconds
WIN_SAMPLES = int(SR * WIN)
OUT_DIR = "data/wakewords"

DECOYS = [
    # other wake words + commands
    "hey jarvis", "hey alexa", "hey google", "hey siri", "okay computer",
    "good morning", "hello there", "what time is it", "play some music",
    "turn on the lights", "open the door", "all done", "oh no", "wake up",
    "are you there", "thank you", "the weather today", "set a timer",
    "how are you", "let us go", "stop the music", "tell me a joke",
    "what is the news", "send a message", "take a screenshot",
    # NEAR-MISSES to "odin"/"hey odin" — force the boundary to be sharp
    "odyssey", "odeon", "showing", "go in", "old inn", "rowing", "oh then",
    "modern", "garden", "hey adam", "hey aiden", "hey owen", "ohio",
    "hey oden", "a den", "oden", "showing now", "holding",
    "hey there odin is", "the odin sphere", "wooden",
]


def _load_piper_voices(cfg):
    from piper.voice import PiperVoice
    voices = []
    paths = []
    for key in ("piper_voice_path",):
        p = cfg.get("iris", {}).get(key, "")
        if p and os.path.exists(p):
            paths.append(p)
    # Add any other English .onnx voices sitting in data/voices.
    for p in glob.glob("data/voices/en_*.onnx"):
        paths.append(p)
    # Dedup by normalized path so "a/b.onnx" and "a\b.onnx" aren't both loaded.
    seen, uniq = set(), []
    for p in paths:
        key = os.path.normcase(os.path.normpath(p))
        if key not in seen:
            seen.add(key); uniq.append(p)
    for p in uniq:
        try:
            voices.append(PiperVoice.load(p))
            print(f"  voice: {os.path.basename(p)}")
        except Exception as e:
            print(f"  (skip {p}: {e})")
    if not voices:
        raise SystemExit("No Piper voices found in data/voices — run setup_voice.py first.")
    return voices


def _synth(voice, text) -> np.ndarray:
    """Piper synth -> float32 mono at 16 kHz in [-1, 1]."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    try:
        with wave.open(tmp, "wb") as wf:
            voice.synthesize_wav(text, wf)
        with wave.open(tmp, "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
            raw = wf.readframes(n)
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if sr != SR:
            # Linear resample to 16 kHz.
            idx = np.linspace(0, len(audio) - 1, int(len(audio) * SR / sr))
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        return audio
    finally:
        try: os.unlink(tmp)
        except Exception: pass


def _speed(audio, factor):
    idx = np.linspace(0, len(audio) - 1, int(len(audio) / factor))
    return np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)


def _place(audio, rng):
    """Place a clip into a fixed 2s window at a random offset, with gain +
    light noise — returns int16 of length WIN_SAMPLES."""
    a = audio.copy()
    a = a * rng.uniform(0.6, 1.0)                       # gain
    buf = np.zeros(WIN_SAMPLES, dtype=np.float32)
    if len(a) >= WIN_SAMPLES:
        a = a[:WIN_SAMPLES]
        off = 0
    else:
        off = rng.integers(0, WIN_SAMPLES - len(a) + 1)
    buf[off:off + len(a)] += a
    buf += rng.normal(0, rng.uniform(0.0, 0.01), WIN_SAMPLES)   # ambient noise
    buf = np.clip(buf, -1.0, 1.0)
    return (buf * 32767).astype(np.int16)


def _augment_set(audio, rng, n):
    """n augmented int16 windows from one clean clip."""
    out = []
    for _ in range(n):
        a = audio
        sp = rng.uniform(0.9, 1.12)
        if abs(sp - 1.0) > 0.01:
            a = _speed(a, sp)
        out.append(_place(a, rng))
    return out


def main():
    args = sys.argv[1:]
    phrase = args[0] if args else "hey odin"
    variants = args[1:] if len(args) > 1 else [phrase, "okay odin", "hi odin", "odin"]
    variants = list(dict.fromkeys([phrase] + variants))
    slug = "".join(c if c.isalnum() else "_" for c in phrase.lower()).strip("_")

    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    print(f"Training wake word: '{phrase}'  (variants: {variants})")
    print("Loading Piper voices...")
    voices = _load_piper_voices(cfg)

    rng = np.random.default_rng(1234)
    print("Synthesizing positives...")
    pos_clips = []
    for v in voices:
        for txt in variants:
            base = _synth(v, txt)
            pos_clips.extend(_augment_set(base, rng, 40))

    # Real user recordings, if any (strong positives, augmented lightly).
    sample_dir = os.path.join(OUT_DIR, "samples", slug)
    real = glob.glob(os.path.join(sample_dir, "*.wav"))
    for w in real:
        try:
            with wave.open(w, "rb") as wf:
                sr = wf.getframerate(); raw = wf.readframes(wf.getnframes())
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if sr != SR:
                idx = np.linspace(0, len(a) - 1, int(len(a) * SR / sr))
                a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
            pos_clips.extend(_augment_set(a, rng, 25))
        except Exception as e:
            print(f"  (skip recording {w}: {e})")
    if real:
        print(f"  + {len(real)} of your own recordings")

    print("Synthesizing negatives...")
    neg_clips = []
    for v in voices:
        for txt in DECOYS:
            base = _synth(v, txt)
            neg_clips.extend(_augment_set(base, rng, 14))
    # Pure silence + noise negatives.
    for _ in range(120):
        buf = rng.normal(0, rng.uniform(0.0, 0.05), WIN_SAMPLES).astype(np.float32)
        neg_clips.append((np.clip(buf, -1, 1) * 32767).astype(np.int16))

    print(f"positives={len(pos_clips)}  negatives={len(neg_clips)}  — embedding...")
    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()

    def embed(clips):
        arr = np.stack(clips).astype(np.int16)
        emb = np.array(af.embed_clips(arr))      # (n, 16, 96)
        return emb.reshape(emb.shape[0], -1)     # flatten -> (n, 1536)

    Xp, Xn = embed(pos_clips), embed(neg_clips)
    X = np.concatenate([Xp, Xn])
    y = np.concatenate([np.ones(len(Xp)), np.zeros(len(Xn))])

    from sklearn.neural_network import MLPClassifier
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import classification_report
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, random_state=0, stratify=y)
    clf = MLPClassifier(hidden_layer_sizes=(128, 32), max_iter=400, random_state=0)
    clf.fit(Xtr, ytr)
    print(classification_report(yte, clf.predict(Xte), target_names=["other", phrase], digits=3))

    # Pick a threshold: high precision (few false wakes) on the held-out set.
    probs = clf.predict_proba(Xte)[:, 1]
    pos_probs = probs[yte == 1]
    threshold = float(np.clip(np.percentile(pos_probs, 15), 0.5, 0.9))
    print(f"chosen threshold: {threshold:.3f}")

    os.makedirs(OUT_DIR, exist_ok=True)
    import joblib
    out = os.path.join(OUT_DIR, f"{slug}.pkl")
    joblib.dump({"clf": clf, "threshold": threshold, "phrase": phrase,
                 "window_samples": WIN_SAMPLES, "sr": SR}, out)
    print(f"\nSaved {out}")
    print("Enable it in config.yaml:")
    print('  heimdall:')
    print('    wake_engine: "custom"')
    print(f'    custom_wake_model: "{out.replace(os.sep, "/")}"')


if __name__ == "__main__":
    main()
