"""
manim_anim — mathematical animations via Manim CE (Report #1 / Manim).

A Manim scene snippet → mp4 rendered locally with the free Manim
Community Edition (MIT), isolated in a subprocess with a hard timeout
and captured stderr — honest failure, never a placeholder video.

Dependencies (FREE, guarded — never faked):
  * `pip install manim` (needs ffmpeg for mp4; imageio-ffmpeg counts)
  * system Cairo/Pango via the usual wheels

WHY NOT A DUPLICATE: charts.py plots data series into static charts;
diagram.py draws static svgs. This renders ANIMATED explainers
(calculus, transforms, graphs in motion) to video.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

_INSTALL = ("Manim not installed — free (MIT): `pip install manim` "
            "(plus ffmpeg). I render real mp4s only — no placeholder "
            "video when the library is missing.")
_TIMEOUT = 300


# ── seams (tests replace these) ────────────────────────────────────────────

def _has_manim() -> bool:
    return (importlib.util.find_spec("manim") is not None
            or _which_manim() is not None)


def _which_manim() -> str | None:
    import shutil
    return shutil.which("manim")


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _scene_name(code: str) -> str | None:
    """First Scene subclass declared in the snippet (pure — tested)."""
    m = re.search(r"class\s+([A-Za-z_]\w*)\s*\(\s*(?:MovingScene|"
                  r"Scene|ThreeDScene|VectorScene|ZoomedScene)\s*\)",
                  code)
    return m.group(1) if m else None


def _build_file(code: str, path: Path) -> str:
    """Write the scene file; return the scene class name to render."""
    body = code.rstrip()
    lines = [l for l in body.splitlines()
             if not l.strip().startswith("```")]
    body = "\n".join(lines).strip()
    if not re.search(r"^from manim import|^import manim", body, re.M):
        body = "from manim import *\n\n" + body
    name = _scene_name(body)
    if not name:
        body += "\n\nclass JavisScene(Scene):\n    def construct(self):\n" \
                "        t = Text('JARVIS').scale(0.8)\n" \
                "        self.play(Write(t))\n        self.wait(0.5)\n"
        name = "JavisScene"
    path.write_text(body + "\n", encoding="utf-8")
    return name


def _render(file_path: Path, scene: str, quality: str,
            media_dir: Path, timeout: int = _TIMEOUT) -> Path:
    """Subprocess render → newest .mp4 path. Raises with stderr tail."""
    exe = _which_manim()
    base = ([exe] if exe else [sys.executable, "-m", "manim"])
    q = {"low": "-ql", "medium": "-qm", "high": "-qh"}.get(quality, "-ql")
    proc = subprocess.run(
        base + ["render", q, "--media_dir", str(media_dir),
                str(file_path), scene],
        capture_output=True, text=True, timeout=timeout,
        cwd=str(file_path.parent))
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or ""))[-600:]
        raise RuntimeError(f"manim failed (exit {proc.returncode}):\n{tail}")
    clips = sorted(media_dir.rglob("*.mp4"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not clips:
        raise RuntimeError("manim reported success but produced no mp4")
    return clips[0]


# ── tool ────────────────────────────────────────────────────────────────────

def manim_anim(parameters: dict = None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "render")).lower().strip()

    if action in ("status", "check"):
        if _has_manim():
            return ("Manim available — manim_anim code='class S(Scene): …' "
                    "renders to mp4.")
        return _INSTALL

    code = str(params.get("code", "") or params.get("scene", "")).strip()
    if not code:
        return ("Give me a Manim scene — manim_anim code='class S(Scene): "
                "def construct(self): …' (first Scene subclass wins).")

    if not _has_manim():
        return _INSTALL

    out_dir = _base_dir() / "anims"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    file_path = out_dir / f"manim-{stamp}.py"
    media = out_dir / f"media-{stamp}"
    try:
        scene = _build_file(code, file_path)
    except Exception as e:
        return f"Could not write scene file: {e}"

    quality = str(params.get("quality", "low") or "low").lower()
    try:
        mp4 = _render(file_path, scene, quality, media)
    except subprocess.TimeoutExpired:
        return (f"Render timed out after {_TIMEOUT}s — scene kept at "
                f"{file_path} (try quality=low / shorter animation).")
    except RuntimeError as e:
        return (f"Render failed: {e}\nScene kept at {file_path} — fix "
                "and re-ask (free live editor: manim.community/docs).")
    except Exception as e:
        return f"Render error: {e}"

    if player is not None:
        try:
            player.show_content("ANIMATION",
                                f"{mp4}\n\nscene: {scene}")
        except Exception:
            pass
    return (f"Animation rendered: {mp4} (scene `{scene}`, quality="
            f"{quality}, {mp4.stat().st_size} B). Open in any player.")


TOOL = {
    "name": "manim_anim",
    "description": (
        "Render a Manim Community Edition scene (first Scene subclass) "
        "to a real mp4 — math/algorithm explainers, transforms, "
        "calculus visuals. Free MIT library; action=status checks "
        "availability and returns the exact `pip install manim` line "
        "otherwise (never a placeholder video). Use for 'animate X', "
        "'make a video showing how Y works'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "render (default) | status."},
            "code": {"type": "STRING",
                     "description": "Manim scene python source."},
            "quality": {"type": "STRING",
                        "description": "low (default) | medium | high."},
        },
        "required": [],
    },
    "handler": manim_anim,
}
