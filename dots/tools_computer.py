# dots/tools_computer.py
"""Computer tools for the Dot brain (doc §3.4 / tools table).

Spec exposure needs the dot-level `computer` permission; each call is
then gated AGAIN on the computer's live browser/files/shell toggles
inside dots/computer.py (honest `denied:` + audit row, actor='agent').
If the dot has no computer yet, the tool says so honestly.
"""
from __future__ import annotations

PRIV = "computer"

_SPECS = [
    ("computer_navigate", "Open an http(s) URL in the Dot's browser.",
     {"url": {"type": "string"}}, ["url"]),
    ("computer_read", "Read the current page's URL, title and body text.",
     {}, []),
    ("computer_snapshot",
      "Full page state: url/title/text + interactive elements (with CSS "
      "selectors you can pass to computer_click/computer_type).",
      {}, []),
    ("computer_screenshot",
      "PNG screenshot of the current viewport, saved inside the "
      "computer's work dir; returns its relative path.",
      {}, []),
    ("computer_click", "Click an element by CSS selector or text=... "
      "(e.g. '#submit' or 'text=Login').",
      {"target": {"type": "string"}}, ["target"]),
    ("computer_type", "Fill a form field: selector + text.",
      {"target": {"type": "string"}, "text": {"type": "string"}},
      ["target", "text"]),
    ("computer_key", "Press a keyboard key (Enter, Tab, Escape, ...).",
      {"key": {"type": "string"}}, ["key"]),
    ("computer_scroll", "Scroll the page: direction=up|down, amount px.",
     {"direction": {"type": "string"}, "amount": {"type": "integer"}},
     []),
    ("files_list", "List files in the Dot computer's work dir (path "
      "optional, jailed).",
      {"path": {"type": "string"}}, []),
    ("files_read", "Read a file from the Dot computer's work dir (jailed).",
      {"path": {"type": "string"}}, ["path"]),
    ("files_write", "Write a file inside the Dot computer's work dir "
      "(jailed). append=true appends.",
      {"path": {"type": "string"}, "content": {"type": "string"},
       "append": {"type": "boolean"}},
      ["path", "content"]),
    ("exec",
      "Run one command in the Dot computer: argv = LIST of strings "
      "(never a shell line), cwd = work dir, 30 s default / 60 s hard "
      "cap, output capped.",
      {"argv": {"type": "array", "items": {"type": "string"}},
       "timeout": {"type": "integer"}},
      ["argv"]),
]

SPECS = [
    {"type": "function",
     "function": {"name": n, "description": d,
                  "parameters": {"type": "object", "properties": pr,
                                 "required": req}}}
    for n, d, pr, req in _SPECS
]


def allowed(dot: dict) -> bool:
    p = dot.get("permissions")
    return bool(isinstance(p, dict) and p.get("computer"))


def run(dot: dict, name: str, args: dict) -> str:
    from . import computer
    return computer.run_agent_tool(dot["id"], name, args)
