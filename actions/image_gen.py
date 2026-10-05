"""
image_gen — free text-to-image via Pollinations (keyless, no account).

GET https://image.pollinations.ai/prompt/{prompt}?width&height&seed&model&nologo
returns the rendered image bytes directly — no API key, no quota dance. The
endpoint is a free community service: it needs network access, and when it is
unreachable the action says so instead of inventing a file.

Output lands in generated/ (gitignored) like charts/ does for SVG charts.
"""
from __future__ import annotations

import time
import urllib.parse
import urllib.request
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _get(url: str, timeout: float = 120.0) -> bytes:
    """Fetch seam — tests monkeypatch this instead of hitting the network."""
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sniff(data: bytes) -> str:
    """Image magic → extension, or '' when the payload is not an image."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def _int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def image_gen(parameters: dict = None, player=None,
              session_memory=None) -> str:
    p = parameters or {}
    prompt = str(p.get("prompt") or "").strip()
    if not prompt:
        return ("Give a prompt to draw — image_gen(prompt=\"a red fox in "
                "snow\").")
    width = _int(p.get("width"), 1024, 256, 2048)
    height = _int(p.get("height"), 1024, 256, 2048)
    seed = _int(p.get("seed"), int(time.time()) % 100000, 0, 999999999)
    model = str(p.get("model") or "flux").strip() or "flux"
    if not model.replace("-", "").replace("_", "").isalnum():
        return f"Unknown model {model!r} — keep it alphanumeric (e.g. flux)."

    url = ("https://image.pollinations.ai/prompt/"
           f"{urllib.parse.quote(prompt, safe='')}"
           f"?width={width}&height={height}&seed={seed}"
           f"&model={urllib.parse.quote(model)}&nologo=true")
    try:
        data = _get(url)
    except Exception as e:                       # noqa: BLE001 — honest report
        return (f"Image service unreachable ({type(e).__name__}: {e}) — "
                "the free Pollinations endpoint needs network access.")

    ext = _sniff(data)
    if not ext:
        head = data[:80].decode("utf-8", "replace").replace("\n", " ")
        return (f"Image service returned non-image data ({len(data)} bytes, "
                f"starts {head!r}) — refused to save a fake image.")
    if len(data) < 200:
        return f"Image service returned a suspiciously tiny file ({len(data)} bytes) — refused."

    out = _base_dir() / "generated" / (
        f"img-{time.strftime('%Y%m%d-%H%M%S')}-{seed}.{ext}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return f"Saved: {out} ({width}x{height}, model={model}, seed={seed})"


TOOL = {
    "name": "image_gen",
    "description": (
        "Generate an image from a text prompt (free, keyless Pollinations "
        "endpoint). Saves the PNG/JPG/WebP under generated/ and returns the "
        "path. Use when the user wants a picture, illustration, wallpaper, "
        "or 'draw/create an image of …'.")
    ,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {"type": "STRING",
                       "description": "What to draw — be concrete."},
            "width": {"type": "INTEGER",
                      "description": "Pixels wide 256-2048 (default 1024)."},
            "height": {"type": "INTEGER",
                       "description": "Pixels tall 256-2048 (default 1024)."},
            "seed": {"type": "INTEGER",
                     "description": "Reproducibility seed (default: now)."},
            "model": {"type": "STRING",
                      "description": "Hosted model name (default flux)."},
        },
        "required": ["prompt"],
    },
    "handler": image_gen,
}
