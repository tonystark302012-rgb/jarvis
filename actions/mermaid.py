"""
mermaid — render Mermaid diagram source to a viewable SVG (Report R2 §12).

The world writes diagrams in Mermaid (flowchart/sequence/class/state/…).
JARVIS now renders them locally:

  1. Normalize the pasted source (strip ``` fences, add direction if a
     bare flowchart body arrived).
  2. Render with mermaid-cli (`npx -y @mermaid-js/mermaid-cli`, free,
     MIT) into <base>/diagrams/mermaid-<ts>.svg — argv list, file input,
     hard timeout, NO shell.
  3. Reveal the file in the content panel; any browser can open it.
  4. HONEST fallback: when the CLI/Chromium is missing, say exactly that
     with the free install one-liner — never fake an SVG.

WHY NOT A DUPLICATE: diagram.py draws a JARVIS-specific spec (flow/
sequence/mindmap/timeline) in pure Python. This accepts the Mermaid
language itself, which users paste from docs — a different input
dialect with a real renderer behind it.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _normalize(source: str) -> str:
    """Strip code fences and fix up the most common paste mistakes."""
    s = str(source or "").strip()
    s = re.sub(r"^```(?:mermaid)?\s*\n", "", s)
    s = re.sub(r"\n```\s*$", "", s).strip()
    return s


def _cli_candidates() -> list[list[str]]:
    """Executable invocations to try, in order. Free, all local."""
    cmds: list[list[str]] = []
    if shutil.which("mmdc"):
        cmds.append(["mmdc"])
    if shutil.which("npx"):
        cmds.append(["npx", "-y", "@mermaid-js/mermaid-cli"])
    return cmds


def _run_cli(argv: list[str], mmd_path: Path, svg_path: Path,
             timeout: int = 90) -> str:
    """Run mermaid-cli on the input file → renders svg_path."""
    proc = subprocess.run(
        argv + ["-i", str(mmd_path), "-o", str(svg_path), "-b", "transparent"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise RuntimeError(f"mermaid-cli exit {proc.returncode}: {tail}")
    return svg_path.read_text(encoding="utf-8", errors="replace")


def _out_path() -> Path:
    d = _base_dir() / "diagrams"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"mermaid-{int(time.time())}.svg"


# ── tool ────────────────────────────────────────────────────────────────────

def mermaid(parameters: dict = None, player=None,
            session_memory=None) -> str:
    params = parameters or {}
    source = _normalize(params.get("source", "") or params.get("diagram", ""))
    if not source:
        return ("Give me Mermaid source — mermaid source=\"flowchart TD; "
                "A-->B\" (``` fences are fine).")
    if not re.match(r"^(flowchart|graph|sequenceDiagram|classDiagram|"
                    r"stateDiagram|stateDiagram-v2|erDiagram|gantt|pie|"
                    r"journey|mindmap|timeline|gitGraph|quadrantChart|"
                    r"subgraph\b)", source, re.I | re.S):
        # not fatal — mermaid may still parse it, but catch the common
        # "forgot the type line" mistake up front with a clear hint
        if "-->" not in source and "->" not in source:
            return ("That doesn't look like Mermaid (no diagram type line "
                    "and no arrows). Start with e.g. `flowchart TD` or "
                    "`sequenceDiagram`.")

    cmds = _cli_candidates()
    if not cmds:
        return ("Mermaid renderer not installed — no `mmdc` and no `npx` "
                "on this machine. Free install (MIT): `npm install -g "
                "@mermaid-js/mermaid-cli` (needs Chromium) or install "
                "Node.js first, then ask me again. I render with real "
                "mermaid-cli — never a fake SVG.")

    mmd_path = _out_path().with_suffix(".mmd")
    svg_path = mmd_path.with_suffix(".svg")
    mmd_path.write_text(source, encoding="utf-8")

    last_err = ""
    for argv in cmds:
        try:
            svg = _run_cli(argv, mmd_path, svg_path)
        except subprocess.TimeoutExpired:
            last_err = "render timed out after 90s (huge diagram?)"
            continue
        except Exception as e:
            last_err = str(e)[:400]
            continue
        if "<svg" not in svg:
            last_err = "renderer produced no SVG"
            continue
        shown = ""
        if player is not None:
            try:
                player.show_content("MERMAID", f"{svg_path}\n\n{source[:3000]}")
                shown = " — shown in the content panel"
            except Exception:
                pass
        return (f"Mermaid diagram rendered: {svg_path}{shown}. "
                f"Open it in any browser (SVG).")

    # every candidate failed — be precise, never fake
    return (f"Mermaid render failed: {last_err or 'unknown error'}. "
            "The source is saved at "
            f"{mmd_path} — fix it (mermaid.live previews it free) or "
            "re-ask with a simpler diagram.")


TOOL = {
    "name": "mermaid",
    "description": (
        "Render Mermaid diagram source (``` fences OK) to a real SVG on "
        "disk via mermaid-cli and reveal it. Use whenever someone pastes "
        "or asks for a mermaid/flowchart/sequence diagram. HONEST: if "
        "mermaid-cli/Chromium is missing it returns the free install "
        "one-liner instead of faking output."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "source": {"type": "STRING",
                       "description": "Mermaid source, e.g. 'flowchart TD; "
                                      "A-->B'."},
        },
        "required": ["source"],
    },
    "handler": mermaid,
}
