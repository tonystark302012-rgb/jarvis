# actions/privacy.py
"""privacy — toggle the local/cloud gate.

One word on ('privacy on') and every tool that would transmit your
content stops and says so, while history, RAG, files, scanner, DuckDB,
make_3d, vault, git and stdio-MCP keep working. Backed by
core.privacy (the flag lives in config/api_keys.json).
"""
from __future__ import annotations

from core import privacy as _p


def privacy(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    raw = str(params.get("state") or params.get("action") or "").strip().lower()
    if raw in ("on", "enable", "true", "1", "private"):
        _p.set(True)
        return _p.status()
    if raw in ("off", "disable", "false", "0", "public"):
        _p.set(False)
        return _p.status()
    if not raw or raw in ("status", "show"):
        return _p.status()
    return (f"Unknown privacy state {raw!r} — say 'privacy on', "
            "'privacy off' or just 'privacy' for status.")


TOOL = {
    "name": "privacy",
    "description": (
        "Privacy mode: 'on' blocks every cloud-facing tool (research, web "
        "search, scrape, phone vision, region OCR's cloud branch, translate, "
        "file analysis) while local tools keep working — history, RAG, "
        "files, scanner, data queries, make_3d, vault, git, automation. "
        "'off' lifts the gate; no argument returns status. Use when the "
        "user says 'privacy mode', 'offline mode', 'don't send anything "
        "to the cloud'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "state": {"type": "STRING", "description": "on | off (or empty for status)"},
        },
        "required": [],
    },
    "handler": privacy,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return privacy(params,
                   player=(ctx or {}).get("player"),
                   session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(privacy({}))
