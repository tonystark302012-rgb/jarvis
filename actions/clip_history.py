"""
clip_history — searchable clipboard history.

pyperclip polls the clipboard every 1.5s while watching is on; every NEW text
entry is appended to a bounded ring (500). The tool lists, searches, reuses
('paste that link') and clears history. Everything is local — nothing leaves
the machine.

The watcher is optional and lazy: it starts on first `watch=on` (or when the
UI enables it) so importing this action costs nothing in tests or headless
runs where no clipboard exists.
"""
from __future__ import annotations

import threading
import time
from collections import deque

_LOCK = threading.Lock()
_HISTORY: deque[dict] = deque(maxlen=500)
_WATCHER: threading.Thread | None = None
_STOP = threading.Event()
_LAST = ""
_INTERVAL = 1.5


def _clip_get() -> str | None:
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        return None


def _clip_set(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def record(text: str) -> bool:
    """Store one entry (deduped against the previous one)."""
    global _LAST
    text = (text or "").strip()
    if not text or text == _LAST:
        return False
    _LAST = text
    with _LOCK:
        _HISTORY.append({"text": text[:8000],
                         "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return True


def _watch_loop():
    while not _STOP.is_set():
        val = _clip_get()
        if val:
            record(val)
        _STOP.wait(_INTERVAL)


def watch(on: bool) -> str:
    global _WATCHER
    if on:
        if _WATCHER and _WATCHER.is_alive():
            return "Clipboard watch already on."
        if _clip_get() is None:
            return "No clipboard available on this system (pyperclip missing or headless)."
        _STOP.clear()
        _WATCHER = threading.Thread(target=_watch_loop, daemon=True,
                                    name="clip-history")
        _WATCHER.start()
        return "Clipboard watch on — new copies will be recorded."
    _STOP.set()
    _WATCHER = None
    return "Clipboard watch off."


def clip_history(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()

    if action == "watch":
        state = str(params.get("state", "on")).lower().strip()
        result = watch(state in ("on", "true", "1", "start"))
    elif action == "clear":
        with _LOCK:
            _HISTORY.clear()
        result = "Clipboard history cleared."
    elif action == "save":
        # explicit capture (e.g. UI hook after a copy event)
        ok = record(str(params.get("text", "")))
        result = "Saved." if ok else "Nothing new to save."
    elif action in ("use", "paste", "get"):
        try:
            idx = int(params.get("index", 1))
        except (TypeError, ValueError):
            idx = 1
        with _LOCK:
            items = list(_HISTORY)
        if not items:
            return "Clipboard history is empty."
        # 1 = most recent
        pick = items[-idx] if 1 <= idx <= len(items) else None
        if pick is None:
            return f"No entry #{idx}. History has {len(items)} entries."
        if _clip_set(pick["text"]):
            result = f"Entry #{idx} copied back to the clipboard."
        else:
            result = f"Entry #{idx} (clipboard unavailable to write):\n{pick['text'][:500]}"
    else:
        query = str(params.get("query", "")).strip().lower()
        with _LOCK:
            items = list(_HISTORY)
        if query:
            items = [it for it in items if query in it["text"].lower()]
        if not items:
            return f"No clipboard entries{f' matching {query!r}' if query else ''}."
        shown = items[-15:]
        lines = []
        for i, it in enumerate(reversed(shown), 1):
            one = it["text"].replace("\n", " ")
            if len(one) > 100:
                one = one[:97] + "…"
            lines.append(f"{i}. [{it['at']}] {one}")
        result = (f"Clipboard history ({len(items)} "
                  f"{'matching ' + repr(query) if query else 'total'}):\n"
                  + "\n".join(lines))

    if player is not None and action == "list":
        try:
            player.show_content("CLIPBOARD HISTORY", result[:4000])
        except Exception:
            pass
    return result


TOOL = {
    "name": "clip_history",
    "description": (
        "Searchable clipboard history. Actions: list (default) with optional "
        "query search, use (copy an entry back to clipboard by index, 1=most "
        "recent), watch on/off (start recording new copies), save (record "
        "given text), clear. Use when the user says 'what did I copy', "
        "'paste that thing from earlier', 'clipboard history'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | use | watch | save | clear."},
            "query": {"type": "STRING", "description": "Filter for list/search."},
            "index": {"type": "STRING", "description": "Entry number for use (1=recent)."},
            "state": {"type": "STRING", "description": "on/off for watch."},
            "text": {"type": "STRING", "description": "Text to record for save."},
        },
        "required": [],
    },
    "handler": clip_history,
}
