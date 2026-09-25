# HORUS — Egyptian — the all-seeing eye
# Vision: screenshots, image recognition, visual processing

import base64
import io
import os
import pyautogui
from datetime import datetime
from core.marduk import OdinModule

# OCR via tesseract — needs the binary installed (https://github.com/UB-Mannheim/tesseract/wiki)
# AND `pip install pytesseract`. We import lazily; the binary check happens at first call.
try:
    import pytesseract
    _HAS_PYTESSERACT = True
except ImportError:
    _HAS_PYTESSERACT = False

# Image understanding via a local multimodal model on Ollama (default: llava).
# Falls back to OCR-only output when the model isn't pulled.
try:
    import ollama
    _HAS_OLLAMA = True
except ImportError:
    _HAS_OLLAMA = False

# Object detection via YOLOv8 (ultralytics). Faster and lighter than LLaVA
# for "what's in this image" questions — runs the smallest yolov8n model
# (~6 MB) at ~30 fps on CPU. Returns a flat list of (class, confidence, box)
# instead of a paragraph description. Lazy-loaded on first detect call so
# the 6 MB weight download doesn't slow boot for users who never use it.
try:
    from ultralytics import YOLO
    _HAS_YOLO = True
except ImportError:
    _HAS_YOLO = False


class Horus(OdinModule):
    MODULE_NAME = "HORUS"
    LAYER = "INPUT"

    def __init__(self, config: dict):
        super().__init__(config)
        self.screenshots_path = "data/screenshots"
        os.makedirs(self.screenshots_path, exist_ok=True)
        host = (config or {}).get("gilgamesh", {}).get("host", "http://localhost:11434")
        self._client = ollama.Client(host=host) if _HAS_OLLAMA else None
        # Vision model — bake-in default; user can override in config.
        self._vision_model = (config or {}).get("horus", {}).get("vision_model", "llava")
        # YOLO config — model name + confidence floor for object listing.
        hcfg = (config or {}).get("horus", {})
        self._yolo_model_name = hcfg.get("yolo_model", "yolov8n.pt")   # nano = ~6 MB, fastest
        self._yolo_confidence = float(hcfg.get("yolo_confidence", 0.30))
        self._yolo = None    # lazy-loaded on first detect_objects call

    @property
    def skills(self) -> list[dict]:
        return [
            {
                "name": "take_screenshot",
                "description": "Take a screenshot of the current screen",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "read_screen",
                "description": "Take a screenshot and read the text on it (OCR)",
                "parameters": {},
                "required": []
            },
            {
                "name": "describe_screen",
                "description": "Take a screenshot and describe what is visible on screen",
                "parameters": {},
                "required": []
            },
            {
                "name": "get_screen_text",
                "description": "Take a screenshot and describe what is on screen",
                "parameters": {},
                "required": [],
                "internal_only": True
            },
            {
                "name": "detect_objects",
                "description": "Run YOLOv8 object detection on an image file. Returns a list of detected objects with confidence. Use for 'what objects are in this photo' / 'is there a person in this image'. Much faster than LLaVA — ~30 fps on CPU. Falls back to screen capture if no path given.",
                "parameters": {
                    "path":      {"type": "string",  "description": "Image file path (optional — defaults to a fresh screenshot)"},
                    "min_conf":  {"type": "number",  "description": "Minimum confidence 0.0-1.0 (default 0.30)"},
                },
                "required": [],
            },
            {
                "name": "count_objects",
                "description": "Count how many instances of a specific object class appear in an image. Use for 'how many people are in this photo' / 'is there a cat in this image'. Returns a number plus the confidence of the highest-scoring detection.",
                "parameters": {
                    "label":     {"type": "string", "description": "COCO class name (e.g. 'person', 'cat', 'laptop', 'cup'). See ultralytics docs for the full 80-class list."},
                    "path":      {"type": "string", "description": "Image file path (optional — defaults to a fresh screenshot)"},
                    "min_conf":  {"type": "number", "description": "Minimum confidence 0.0-1.0 (default 0.30)"},
                },
                "required": ["label"],
            },
            {
                "name": "objects_on_screen",
                "description": "Take a screenshot and list every object YOLO can detect on it. Convenience wrapper for 'what's on my screen right now'. Pairs well with read_screen for 'what does it say AND what does it show'.",
                "parameters": {
                    "min_conf": {"type": "number", "description": "Minimum confidence 0.0-1.0 (default 0.30)"},
                },
                "required": [],
            },
        ]

    def execute(self, skill_name: str, args: dict) -> str:
        _map = {
            "take_screenshot":    self._screenshot,
            "read_screen":        self._read_screen,
            "describe_screen":    self._describe_screen,
            "get_screen_text":    self._read_screen,
            "detect_objects":     self._detect_objects,
            "count_objects":      self._count_objects,
            "objects_on_screen":  self._objects_on_screen,
        }
        fn = _map.get(skill_name)
        if fn:
            try:
                return fn(**args)
            except Exception as e:
                return f"[HORUS] Error: {e}"
        return f"[HORUS] Unknown skill: {skill_name}"

    def _capture(self) -> tuple[str, "Image"]:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.screenshots_path, f"screen_{ts}.png")
        img = pyautogui.screenshot()
        img.save(path)
        return path, img

    def _screenshot(self) -> str:
        path, _ = self._capture()
        return f"Screenshot saved: {path}"

    def _read_screen(self) -> str:
        path, img = self._capture()
        if not _HAS_PYTESSERACT:
            return f"Screenshot saved: {path}. OCR unavailable — install pytesseract and Tesseract binary."
        try:
            text = pytesseract.image_to_string(img).strip()
        except pytesseract.pytesseract.TesseractNotFoundError:
            return (f"Screenshot saved: {path}. Tesseract binary not found — install from "
                    "https://github.com/UB-Mannheim/tesseract/wiki and add it to PATH.")
        if not text:
            return f"Screenshot saved: {path}. No readable text found on screen."
        # Compact the wall of text for voice readback.
        compact = " ".join(text.split())
        # Push the FULL text into working memory (SESHAT) so follow-ups like
        # "what did it say about X" work on what ODIN just saw — the spoken
        # reply stays truncated for voice.
        self._note_seen("Screen OCR", compact, "screen")
        return f"Screen text: {compact[:600]}{'…' if len(compact) > 600 else ''}"

    def _note_seen(self, title: str, content: str, kind: str):
        """Best-effort push of what HORUS just perceived into ODIN's working
        memory (SESHAT.note_context via the bus). Perception without memory
        evaporates; this makes sight interrogable after the fact."""
        try:
            if self.marduk and content and content.strip():
                stamp = datetime.now().strftime("%H:%M")
                self.send("SESHAT", "note_context",
                          title=f"{title} at {stamp}", content=content[:6000], kind=kind)
        except Exception as e:
            print(f"[HORUS] note_context push failed: {e}")

    def _describe_screen(self) -> str:
        path, img = self._capture()
        if not self._client:
            return f"Screenshot saved: {path}. Vision model unavailable — install ollama-py."
        # Send the PNG to a local multimodal model.
        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            resp = self._client.chat(
                model=self._vision_model,
                messages=[{
                    "role": "user",
                    "content": "Describe what is on this screen in one or two short sentences.",
                    "images": [b64],
                }],
            )
            text = (resp.get("message") or {}).get("content", "").strip()
            if text:
                self._note_seen("Screen description", text, "screen")
            return text or f"Screenshot saved: {path}. Vision model returned no description."
        except Exception as e:
            # Most common: model not pulled.
            return (f"Screenshot saved: {path}. Vision model failed ({e}). "
                    f"Run: ollama pull {self._vision_model}")

    # ─────────────────────────────────────────────────────────────────
    # YOLO object detection. Sits alongside the LLaVA describe_screen path —
    # YOLO answers "WHAT objects are in this image" deterministically and
    # fast; LLaVA answers "DESCRIBE this scene" with prose. Use whichever
    # fits the question. Both work; they complement.
    # ─────────────────────────────────────────────────────────────────

    def _ensure_yolo(self):
        """Lazy-load the YOLO model on first use. Returns the model or None
        on failure. First call downloads ~6 MB to ultralytics cache."""
        if self._yolo is not None:
            return self._yolo
        if not _HAS_YOLO:
            return None
        try:
            print(f"[HORUS] Loading YOLO ({self._yolo_model_name}) — first call only.")
            self._yolo = YOLO(self._yolo_model_name)
            return self._yolo
        except Exception as e:
            print(f"[HORUS] YOLO load failed: {e}")
            return None

    def _resolve_image_path(self, path: str = "") -> str:
        """Use the supplied path if it exists, otherwise take a fresh screenshot."""
        path = (path or "").strip()
        if path and os.path.exists(path):
            return path
        shot, _ = self._capture()
        return shot

    def _run_yolo(self, image_path: str, min_conf: float):
        """Run inference, return [(label, confidence, [x1, y1, x2, y2]), ...]"""
        model = self._ensure_yolo()
        if model is None:
            return None
        try:
            results = model(image_path, conf=min_conf, verbose=False)
        except Exception as e:
            print(f"[HORUS] YOLO inference failed: {e}")
            return None
        detections = []
        for r in results:
            names = r.names
            for box in r.boxes:
                cls_id = int(box.cls.item())
                conf = float(box.conf.item())
                xyxy = box.xyxy[0].tolist()
                detections.append((names[cls_id], conf, xyxy))
        return detections

    def _detect_objects(self, path: str = "", min_conf=None) -> str:
        if min_conf is None:
            min_conf = self._yolo_confidence
        else:
            try: min_conf = float(min_conf)
            except (TypeError, ValueError): min_conf = self._yolo_confidence
        img_path = self._resolve_image_path(path)
        detections = self._run_yolo(img_path, min_conf)
        if detections is None:
            return ("YOLO unavailable. Install with `pip install ultralytics` "
                    "and retry.")
        if not detections:
            return f"No objects detected above {int(min_conf*100)}% confidence in {os.path.basename(img_path)}."
        # Group by class to compact "person×3, laptop, cup×2" output.
        from collections import Counter
        counts = Counter(d[0] for d in detections)
        # Highest confidence per class for the spoken summary.
        best = {}
        for label, conf, _ in detections:
            if conf > best.get(label, 0):
                best[label] = conf
        parts = []
        for label, n in counts.most_common():
            tag = f"{label}×{n}" if n > 1 else label
            parts.append(f"{tag} ({int(best[label]*100)}%)")
        return f"Detected: {', '.join(parts)}."

    def _count_objects(self, label: str = "", path: str = "", min_conf=None) -> str:
        label = (label or "").strip().lower()
        if not label:
            return "Need a class label (e.g. 'person', 'cat', 'laptop')."
        if min_conf is None:
            min_conf = self._yolo_confidence
        else:
            try: min_conf = float(min_conf)
            except (TypeError, ValueError): min_conf = self._yolo_confidence
        img_path = self._resolve_image_path(path)
        detections = self._run_yolo(img_path, min_conf)
        if detections is None:
            return "YOLO unavailable."
        hits = [d for d in detections if d[0].lower() == label]
        if not hits:
            return f"No {label!r} detected in {os.path.basename(img_path)}."
        top_conf = max(h[1] for h in hits)
        return f"Found {len(hits)} {label}{'s' if len(hits) != 1 else ''} (top confidence {int(top_conf*100)}%)."

    def _objects_on_screen(self, min_conf=None) -> str:
        return self._detect_objects(path="", min_conf=min_conf)
