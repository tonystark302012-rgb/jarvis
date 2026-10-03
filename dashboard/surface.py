"""Universal render surface — which dashboard tab a piece of content gets.

The dashboard shows five tabs: CHAT (the conversation feed — always the
fallback) plus DISPLAY, SCAN, 3D and WEB. Every tool that puts something
on screen funnels through `player.show_content(title, text)`; this module
maps the TITLE to the tab it belongs on, and main.py broadcasts the
result as {"type": "content", "surface": ...}.

Rules are prefix-based on the existing title conventions in the codebase
(SCRAPE — …, 3D — …, NEWS — …): deterministic, no model call, and a
title that matches nothing lands on DISPLAY — the generic board — which
is always a correct place to put it.

Pure module: no fastapi, no Qt — importable anywhere, fully unit-tested.
"""

from __future__ import annotations

# Every surface a content message can be routed to. CHAT is implicit:
# conversation never becomes a "content" message (it is type "log").
SURFACES = ("display", "scan", "3d", "web")

# Longest-prefix wins, evaluated in order — prefixes are distinct by
# construction (no "SCAN" startswith "SCRAPE" collision: scan keys are
# checked against word boundaries via the split below).
_WEB_PREFIXES = ("SCRAPE", "STRUCTURED", "SEARCH", "NEWS", "WIKI",
                 "WEB", "FLIGHT", "WEATHER")
_SCAN_PREFIXES = ("OCR", "SCAN", "SCREEN", "REGION")
_3D_PREFIXES = ("3D", "MODEL", "MAKE_3D")


def classify_surface(title: str) -> str:
    """Map a show_content() title to its render-surface tab.

    Case-insensitive, prefix-based. Unknown or empty titles → "display"
    (the generic board), never an error — this runs on the content path
    of every tool.
    """
    t = str(title or "").strip().upper()
    if not t:
        return "display"
    # "3D — box" / "SCRAPE — example.com" / "NEWS — top world news"
    # all carry the tag before the first dash (or as the first word).
    head = t.split("—", 1)[0].split("-", 1)[0].strip()
    for key in _3D_PREFIXES:
        if head == key or head.startswith(key + " "):
            return "3d"
    for key in _SCAN_PREFIXES:
        if head == key or head.startswith(key + " "):
            return "scan"
    for key in _WEB_PREFIXES:
        if head == key or head.startswith(key + " "):
            return "web"
    return "display"
