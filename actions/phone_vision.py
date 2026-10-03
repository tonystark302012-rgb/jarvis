# actions/phone_vision.py
"""phone_vision — see through the PHONE camera from the PC.

HOW IT WORKS
    1. The phone dashboard (app.html 📷 button) captures its rear camera
       and POSTs JPEG frames to /api/camera-frame — the server keeps the
       latest one at uploads/camera/frame.jpg.
    2. This action reads that file and answers a prompt about it through
       the Gemini model ladder (same call shape as file_processor's image
       reader). OCR / describe / ask-anything.

WHY NOT A DUPLICATE
    screen_processor reads the PC's own screens and webcams. This reads
    what the PHONE camera currently sees — the remote eye, for 'what's on
    my desk', 'is the door closed', 'read this form', while you sit at
    the keyboard.

FREE: the phone's camera + the same Gemini key the assistant already uses.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_MAX_AGE_S = 300            # a 5-minute-old frame is stale — say so


def _camera_dir() -> Path:
    """Mirror dashboard/server.py's _make_uploads_dir() EXACTLY (same order,
    same mkdir semantics) without importing it — the server pulls in fastapi
    and an action must not. Order matters: if this list drifts from the
    server's, frames land in one dir and the action looks in another."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        _base_dir() / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate / "camera"
        except Exception:
            continue
    return _base_dir() / "uploads" / "camera"


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _latest_frame() -> tuple[Path, float]:
    """(frame path, epoch ts) — raises FileNotFoundError with guidance."""
    d = _camera_dir()
    frame = d / "frame.jpg"
    if not frame.is_file():
        raise FileNotFoundError(
            "No phone camera frame yet — open the JARVIS dashboard on your "
            "phone and tap the 📷 button first.")
    ts = 0.0
    try:
        ts = float(json.loads((d / "frame.json").read_text(encoding="utf-8"))
                   .get("ts") or 0)
    except Exception:
        try:
            ts = frame.stat().st_mtime
        except Exception:
            pass
    return frame, ts


def _ask(frame_path: Path, prompt: str, mode: str) -> str:
    """Send the frame + prompt to Gemini vision; returns answer text."""
    from core import gemini

    if not gemini.api_key():
        return ("No Gemini key configured — phone_vision needs the same key "
                "the assistant uses.")
    try:
        from PIL import Image
        img = Image.open(frame_path)
    except Exception as e:
        return f"Couldn't open the camera frame: {e}"
    if mode == "ocr":
        prompt = ("Extract all text visible in this image. Return only the "
                  "text, formatted clearly.")
    elif mode == "describe" and not prompt:
        prompt = "Describe this image in detail."
    resp = gemini.call([prompt or "Describe this image.", img],
                       tier=gemini.SMART, timeout_ms=45_000)
    if resp is None:
        return "Vision didn't answer — every model on the ladder failed."
    try:
        return "".join(p.text for p in resp.candidates[0].content.parts
                       if getattr(p, "text", None)).strip() or \
            "(the model returned nothing)"
    except Exception as e:
        return f"Couldn't read the vision reply: {e}"


def phone_vision(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode") or "ask").lower().strip()
    if mode not in ("ask", "ocr", "describe"):
        mode = "ask"
    prompt = str(params.get("prompt") or "").strip()
    try:
        max_age = max(10, min(3600, int(params.get("max_age") or _MAX_AGE_S)))
    except (TypeError, ValueError):
        max_age = _MAX_AGE_S

    try:
        frame, ts = _latest_frame()
    except FileNotFoundError as e:
        return str(e)

    age = time.time() - ts if ts else 0
    stale = ts and age > max_age
    if stale and not params.get("allow_stale"):
        return (f"The last phone frame is {int(age)}s old (limit {max_age}s) "
                f"— tap 📷 on the phone dashboard for a fresh shot, or pass "
                f"allow_stale=true to read it anyway.")

    answer = _ask(frame, prompt, mode)
    if answer.startswith(("No Gemini", "Couldn't", "Vision didn't")):
        return answer
    header = f"[phone camera, {int(age)}s ago]" if ts else "[phone camera]"
    return f"{header}\n{answer}"


TOOL = {
    "name": "phone_vision",
    "description": (
        "Answer a question about what the PHONE camera currently sees "
        "(frames arrive from the JARVIS phone dashboard's 📷 button). "
        "mode: ask (default, answer `prompt`), ocr (extract text), "
        "describe. max_age: seconds before the frame counts as stale "
        "(default 300). Use when the user wants the phone as a remote eye: "
        "'what's on my desk', 'read this label', 'is the door closed'. "
        "For the PC's own screen use screen_processor / region_ocr."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING",
                       "description": "Question to answer about the frame"},
            "mode": {"type": "STRING", "description": "ask | ocr | describe"},
            "max_age": {"type": "INTEGER",
                        "description": "Freshness limit in seconds — default 300"},
            "allow_stale": {"type": "BOOLEAN",
                            "description": "Read an old frame anyway"},
        },
        "required": [],
    },
    "handler": phone_vision,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return phone_vision(params,
                        player=(ctx or {}).get("player"),
                        session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(phone_vision({}))
