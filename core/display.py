"""
display — the surface inside the HUD that anything can push content to.

WHY THIS EXISTS
---------------
The HUD can already show the avatar, a video and the camera - each built as its
own one-off widget. There was no surface a *tool* could write to. So a search
result, a table, a diagram or a code listing could only come back as text in the
log, which is the least useful place for any of them.

This module is deliberately split in two:

*   **Everything here is Qt-free and testable.** Rendering is pure string work:
    content in, HTML out. It runs in a headless sandbox, in a test, or in a
    REPL, with no display and no Qt.
*   The widget that shows it (`ui/display_panel.py`) is a thin shell around
    `to_html()` - no rendering logic lives there, so the interesting part is
    always testable.

Anything can push: `display(kind=..., title=..., payload=...)`. Tools, plugins,
the dashboard. If a renderer is missing the content falls back to escaped text
rather than being dropped - a missing optional dependency must never lose
something the user asked to see.
"""

from __future__ import annotations

import html as _html
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DisplayItem", "display", "recent", "clear", "render", "to_html",
           "MAX_ITEMS"]

MAX_ITEMS = 50          # ring buffer: enough for a session, bounded in memory

# What the panel remembers. Deliberately module-level and simple - one list,
# one lock-free append, because pushes happen from tool threads.
_ITEMS: list["DisplayItem"] = []


@dataclass
class DisplayItem:
    kind:    str                 # markdown | code | table | text | html | image | link
    title:   str = ""
    payload: Any = ""
    at:      float = field(default_factory=time.time)

    @property
    def time_str(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))


# ── push / read ───────────────────────────────────────────────────────────────

def display(kind: str, payload: Any, title: str = "") -> DisplayItem:
    """Push something onto the panel. Returns the stored item."""
    kind = (kind or "text").strip().lower()
    if kind not in _RENDERERS:
        # unknown kind: show it rather than drop it
        kind = "text"
    item = DisplayItem(kind=kind, title=title or "", payload=payload)
    _ITEMS.append(item)
    if len(_ITEMS) > MAX_ITEMS:
        del _ITEMS[: len(_ITEMS) - MAX_ITEMS]
    return item


def recent(n: int = MAX_ITEMS) -> list[DisplayItem]:
    """Most recent first."""
    return list(reversed(_ITEMS[-max(1, n):]))


def clear() -> None:
    _ITEMS.clear()


# ── renderers ─────────────────────────────────────────────────────────────────

def _esc(text: Any) -> str:
    return _html.escape(str(text))


def _render_text(item: DisplayItem) -> str:
    return f"<pre class='plain'>{_esc(item.payload)}</pre>"


def _render_code(item: DisplayItem) -> str:
    code = str(item.payload)
    try:                                   # nicer output if pygments is present
        from pygments import highlight
        from pygments.lexers import guess_lexer
        from pygments.formatters import HtmlFormatter
        return highlight(code, guess_lexer(code), HtmlFormatter(nowrap=True))
    except Exception:
        return f"<pre class='code'>{_esc(code)}</pre>"


def _render_markdown(item: DisplayItem) -> str:
    text = str(item.payload)
    try:
        import markdown as _md
        return _md.markdown(text, extensions=["tables", "fenced_code"])
    except Exception:
        # no markdown package: enough HTML to stay readable
        out = []
        for line in text.splitlines():
            if line.startswith("### "):
                out.append(f"<h3>{_esc(line[4:])}</h3>")
            elif line.startswith("## "):
                out.append(f"<h2>{_esc(line[3:])}</h2>")
            elif line.startswith("# "):
                out.append(f"<h1>{_esc(line[2:])}</h1>")
            elif line.startswith("- "):
                out.append(f"<li>{_esc(line[2:])}</li>")
            elif line.strip() == "":
                out.append("<br>")
            else:
                out.append(f"<p>{_esc(line)}</p>")
        return "".join(out)


def _render_html(item: DisplayItem) -> str:
    # already-markup content is passed through; callers control it
    return str(item.payload)


def _render_table(item: DisplayItem) -> str:
    """payload: list of rows, first row is the header (or list of dicts)."""
    rows = item.payload
    # Guard the shape. A string indexed as rows[0] silently produced a table of
    # its characters, which is worse than admitting we could not render it.
    if not isinstance(rows, (list, tuple)) or not rows:
        return _render_text(item)
    if not isinstance(rows[0], (dict, list, tuple)):
        return _render_text(item)

    if isinstance(rows[0], dict):
        headers = list(rows[0].keys())
        body = [[r.get(h, "") for h in headers] for r in rows]
    else:
        headers = list(rows[0])
        body = [list(r) for r in rows[1:]]

    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in row) + "</tr>"
        for row in body
    )
    return (f"<table class='grid'><thead><tr>{head}</tr></thead>"
            f"<tbody>{body_html}</tbody></table>")


def _render_image(item: DisplayItem) -> str:
    src = str(item.payload)
    return f"<img class='shot' src='{_esc(src)}'>"


def _render_link(item: DisplayItem) -> str:
    url = str(item.payload)
    label = item.title or url
    return f"<a class='link' href='{_esc(url)}'>{_esc(label)}</a>"


_RENDERERS = {
    "text":     _render_text,
    "code":     _render_code,
    "markdown": _render_markdown,
    "html":     _render_html,
    "table":    _render_table,
    "image":    _render_image,
    "link":     _render_link,
}


def kinds() -> list[str]:
    return sorted(_RENDERERS)


def render(item: DisplayItem) -> str:
    """Render one item. Never raises - a broken renderer falls back to text."""
    try:
        return _RENDERERS.get(item.kind, _render_text)(item)
    except Exception as e:
        return f"<pre class='plain'>Could not render {_esc(item.kind)}: {_esc(e)}</pre>"


_CSS = """
:root { color-scheme: dark; }
body {
  margin:0; padding:14px 16px; background:#07090f; color:#dde3ed;
  font:13px/1.55 -apple-system,"Segoe UI",Roboto,sans-serif;
}
.card {
  border:1px solid #1b2231; border-radius:10px; margin:0 0 12px;
  background:#0b0f18; overflow:hidden;
}
.card h4 {
  margin:0; padding:8px 12px; font-size:12px; font-weight:600;
  color:#8fa3bf; background:#0e1420; border-bottom:1px solid #1b2231;
  display:flex; justify-content:space-between;
}
.card h4 .t { color:#4d5b70; font-weight:400; font-size:11px; }
.card .body { padding:10px 12px; }
pre.plain, pre.code {
  margin:0; white-space:pre-wrap; word-break:break-word;
  font:12px/1.5 ui-monospace,Menlo,Consolas,monospace; color:#c8d3e3;
}
pre.code { background:#080b12; border:1px solid #1b2231;
           border-radius:6px; padding:8px 10px; }
table.grid { border-collapse:collapse; width:100%; font-size:12px; }
table.grid th, table.grid td {
  border:1px solid #1b2231; padding:5px 8px; text-align:left; vertical-align:top;
}
table.grid th { background:#101724; color:#9fb4d0; font-weight:600; }
img.shot { max-width:100%; border-radius:6px; border:1px solid #1b2231; }
a.link { color:#6cc7ff; }
h1,h2,h3 { color:#e6edf7; margin:12px 0 6px; }
p { margin:6px 0; }
.empty { color:#4d5b70; text-align:center; padding:40px 0; }
"""


def to_html(items: list[DisplayItem] | None = None) -> str:
    """Full document for the panel. Newest first, as people read top-down."""
    items = recent() if items is None else items
    if not items:
        body = ("<div class='empty'>Nothing on the screen yet.<br>"
                "Anything JARVIS looks up can be put here.</div>")
    else:
        parts = []
        for it in items:
            title = _esc(it.title) if it.title else _esc(it.kind)
            parts.append(
                f"<div class='card'><h4><span>{title}</span>"
                f"<span class='t'>{_esc(it.time_str)}</span></h4>"
                f"<div class='body'>{render(it)}</div></div>"
            )
        body = "".join(parts)
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{_CSS}</style></head><body>{body}</body></html>")
