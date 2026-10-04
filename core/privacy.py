# core/privacy.py
"""Privacy mode — a hard gate for tools that would otherwise send your
content to the cloud.

When ON, tools in CLOUD_TOOLS refuse with an honest message instead of
transmitting (research queries, scraped pages, screen/phone images, …).
Local tools (history, RAG, DuckDB, make_3d, vault, git, MCP-over-stdio,
scanner, file ops) are unaffected — that's the point: go dark, keep
working.

The flag persists in config/api_keys.json (already gitignored, already
the home of machine-local preferences) under "privacy_mode": true.

NOTE: the assistant's own chat runs on Gemini — privacy mode cannot make
a cloud model local. What it DOES: stop the *tools* from sending files,
images and queries on your behalf, and say so honestly when asked.
"""
from __future__ import annotations

import json
from pathlib import Path

# Tools whose INPUTS are your content (queries, files, images, screens).
# Adding a cloud-backed tool here is how you opt it into the gate.
CLOUD_TOOLS = frozenset({
    "research",          # sends your topic as search queries + page fetches
    "web_search",        # sends the query to DDG/Gemini
    "scrape",            # fetches arbitrary URLs (your browsing intent)
    "phone_vision",      # sends camera frames to Gemini
    "region_ocr",        # gemini branch (tesseract branch stays local)
    "translate",         # gemini branch
    "file_processor",    # image/document analysis branches
    "background_monitor",  # sends monitoring topics as searches
    "flight_finder",     # sends routes/dates
    "smart_home",        # INTERNET brokers only — the handler re-allows
                         # loopback/private-network targets itself
    "gui_agent",         # screenshots leave the machine for decisions
    "world_view",        # your coordinates go to the public tile servers
})


def _path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "api_keys.json"


def is_on() -> bool:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return bool(data.get("privacy_mode"))
    except Exception:
        return False


def set(on: bool) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["privacy_mode"] = bool(on)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                 encoding="utf-8")


def gate(tool: str) -> str | None:
    """Returns a refusal message when the tool is cloud-bound AND privacy
    mode is on; None means 'go ahead'. Call this FIRST in the handler."""
    if tool in CLOUD_TOOLS and is_on():
        return (f"Privacy mode is ON — '{tool}' would send data to the "
                "cloud, so I didn't. Say 'privacy off' when you want the "
                "network back. Local tools still work.")
    return None


def status() -> str:
    if is_on():
        return ("Privacy mode: ON. Cloud-facing tools (research, search, "
                "scrape, vision, translate, …) are blocked; local tools, "
                "memory, history, RAG and automation keep working. "
                "The chat itself still runs on Gemini — say 'privacy off' "
                "to lift the gate.")
    return ("Privacy mode: OFF. All tools may use the network. Say "
            "'privacy on' to block cloud-facing tools while keeping "
            "everything local running.")
