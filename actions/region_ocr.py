# actions/region_ocr.py
"""region_ocr — capture a live screen region and read its text.

WHAT IT DOES
    1. Captures a rectangle of the live screen (mss — same lib as
       screen_processor) with presets: full / center / top / bottom /
       left / right / cursor, or an explicit "x,y,w,h".
    2. Reads the text: pytesseract when the binary is installed (free,
       offline), otherwise Gemini vision (the app's own key — the same
       call file_processor's image reader makes).
    3. `repeat` turns one shot into a live read: N captures, a pause
       between them, and only what CHANGED is reported — good for
       watching a ticker, a counter, or a log line.

WHY NOT A DUPLICATE
    file_processor ocrs images already ON DISK. scanner reads system
    state. This tool reads the LIVE screen without saving a file.

FREE TOOLS ONLY: mss (capture), pytesseract/tesseract (offline OCR),
Gemini vision as the fallback reader.
"""
from __future__ import annotations

import time
from pathlib import Path


# ── seams ────────────────────────────────────────────────────────────────────

def _capture(region: str):
    """region preset or 'x,y,w,h' → PIL.Image (or raises)."""
    try:
        import mss
        import mss.tools
    except ImportError:
        raise RuntimeError("mss is not installed — pip install mss")

    with mss.mss() as sct:
        mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        box = _resolve_box(region, mon, sct.monitors)
        shot = sct.grab(box)
        try:
            from PIL import Image
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except ImportError as e:
            raise RuntimeError(f"Pillow is not installed: {e}")


def _resolve_box(region: str, mon: dict, monitors: list) -> dict:
    """preset name or 'x,y,w,h' → absolute mss box dict."""
    r = str(region or "center").strip().lower()
    x, y, w, h = mon["left"], mon["top"], mon["width"], mon["height"]

    if r == "full":
        # all real monitors combined (monitors[0] is the virtual union)
        if len(monitors) > 1:
            m0 = monitors[0]
            return {"left": m0["left"], "top": m0["top"],
                    "width": m0["width"], "height": m0["height"]}
        return {"left": x, "top": y, "width": w, "height": h}
    if r == "top":
        return {"left": x, "top": y, "width": w, "height": h // 2}
    if r == "bottom":
        return {"left": x, "top": y + h // 2, "width": w, "height": h - h // 2}
    if r == "left":
        return {"left": x, "top": y, "width": w // 2, "height": h}
    if r == "right":
        return {"left": x + w // 2, "top": y, "width": w - w // 2, "height": h}
    if r == "center":
        cw, ch = int(w * 0.6), int(h * 0.6)
        return {"left": x + (w - cw) // 2, "top": y + (h - ch) // 2,
                "width": cw, "height": ch}
    if r == "cursor":
        try:
            from PyQt6.QtGui import QCursor
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                raise RuntimeError("no Qt app")
            p = QCursor.pos()                       # global screen coords
            return {"left": max(0, p.x() - 200), "top": max(0, p.y() - 150),
                    "width": 400, "height": 300}
        except Exception:
            return {"left": x + (w - 400) // 2, "top": y + (h - 300) // 2,
                    "width": min(400, w), "height": min(300, h)}
    # explicit geometry: x,y,w,h (absolute screen coordinates)
    parts = [p.strip() for p in r.split(",") if p.strip()]
    if len(parts) == 4:
        try:
            gx, gy, gw, gh = (int(float(p)) for p in parts)
        except ValueError:
            raise ValueError(f"Bad region {region!r} — use a preset "
                             f"(full/center/top/bottom/left/right/cursor) "
                             f"or 'x,y,w,h'.")
        if gw <= 0 or gh <= 0:
            raise ValueError("Region width/height must be positive.")
        return {"left": gx, "top": gy, "width": gw, "height": gh}
    raise ValueError(f"Unknown region {region!r} — use a preset "
                     f"(full/center/top/bottom/left/right/cursor) or 'x,y,w,h'.")


def _read_text(img, mode: str = "text") -> str:
    """OCR one frame — tesseract first (offline), Gemini vision next."""
    # 1. local tesseract
    try:
        import pytesseract
        prompt = ("Extract all text visible in this image. Return only the "
                  "text, formatted clearly." if mode == "text"
                  else "Describe this region in detail.")
        if mode == "text":
            return pytesseract.image_to_string(img).strip()
        # describe mode still goes through Gemini below
    except Exception:
        pass

    # 2. Gemini vision — same shape file_processor uses
    from core import gemini
    if not gemini.api_key():
        raise RuntimeError(
            "No OCR backend: install tesseract (pip install pytesseract) "
            "or configure a Gemini API key.")
    prompt = ("Extract all text visible in this image. Return only the "
              "text, formatted clearly." if mode == "text"
              else "Describe this region in detail.")
    resp = gemini.call([prompt, img], tier=gemini.SMART, timeout_ms=45_000)
    if resp is None:
        raise RuntimeError("Gemini vision did not answer.")
    return "".join(
        p.text for p in resp.candidates[0].content.parts
        if getattr(p, "text", None)).strip()


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


# ── handler ──────────────────────────────────────────────────────────────────

def region_ocr(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    region = str(params.get("region") or "center").strip()
    mode = "describe" if str(params.get("mode") or "text").lower() in (
        "describe", "describe_only") else "text"
    try:
        repeat = max(1, min(30, int(params.get("repeat") or 1)))
    except (TypeError, ValueError):
        repeat = 1
    interval = 2.0
    try:
        interval = max(0.5, min(30.0, float(params.get("interval") or 2)))
    except (TypeError, ValueError):
        pass

    readings: list[str] = []
    last_err = ""
    for i in range(repeat):
        try:
            img = _capture(region)
            out = _read_text(img, mode)
            readings.append(out or "")
            last_err = ""
        except Exception as e:
            last_err = str(e)
            break
        if i < repeat - 1:
            time.sleep(interval)

    if last_err and not readings:
        return f"Region read failed: {last_err}"

    if repeat == 1:
        text = readings[0]
        if not text:
            return (f"No readable text in the {region} region "
                    f"(the reader saw nothing)." if not last_err else
                    f"Region read failed after capture: {last_err}")
        return f"[{region}] {text}"

    # live mode: report only changes
    changes, prev = [], None
    for n, r in enumerate(readings, 1):
        if r != prev:
            changes.append(f"#{n}: {r or '(no text)'}")
            prev = r
    if not any(readings):
        return (f"Live read of {region}: no readable text across "
                f"{repeat} captures.{(' Last error: ' + last_err) if last_err else ''}")
    header = f"Live read of {region}: {repeat} captures, {len(changes)} change(s)."
    return header + "\n" + "\n".join(changes[-10:])


TOOL = {
    "name": "region_ocr",
    "description": (
        "Read text from a LIVE region of the screen without saving a file. "
        "region: full | center (default) | top | bottom | left | right | "
        "cursor | 'x,y,w,h'. mode: text (default) | describe. repeat: N "
        "captures with interval seconds between them, reporting only "
        "changes — use repeat for watching a value update ('live read'). "
        "For images already on disk use file_processor instead."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "region": {"type": "STRING",
                       "description": "Preset or x,y,w,h — default center"},
            "mode": {"type": "STRING", "description": "text | describe"},
            "repeat": {"type": "INTEGER",
                       "description": "Captures for a live read — default 1"},
            "interval": {"type": "NUMBER",
                         "description": "Seconds between captures — default 2"},
        },
        "required": [],
    },
    "handler": region_ocr,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return region_ocr(params,
                      player=(ctx or {}).get("player"),
                      session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(region_ocr({}))
