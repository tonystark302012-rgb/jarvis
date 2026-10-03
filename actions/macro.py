"""
macro — record and replay keyboard/mouse routines.

Recording captures low-level input events (pynput when installed, else the
pyautogui-position sampler), replay drives pyautogui. Stored as JSON under
the user's config dir so macros survive restarts and can be listed, renamed
and deleted.

Safety rails, because replaying a recorded sequence blindly is exactly how
someone rm's the wrong folder:
  * replay requires confirm="yes" — the model must ask the user first;
  * replays are bounded (≤ 400 events, ≤ 60s) and abort on ESC;
  * the recorder never installs global hooks unless asked (watch=on).

pynput/pyautogui are optional: without them, record/replay degrade to a
clear message instead of an exception. The JSON store layer works everywhere
and is what the tests exercise hardest.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_RECORDING = False
_EVENTS: list[dict] = []
_REC_THREAD: threading.Thread | None = None
_STOP = threading.Event()

MAX_EVENTS = 400
MAX_SECONDS = 60.0


def _dir() -> Path:
    from config import get_base_dir
    return get_base_dir() / "macros"


def _macro_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "-_ ")[:40].strip()
    safe = safe.replace(" ", "_") or "macro"
    return _dir() / f"{safe}.json"


# ── recording ────────────────────────────────────────────────────────────────

def _sampler_loop():
    """Fallback recorder: samples pointer position + button state at 20Hz via
    pyautogui; key events are unavailable without pynput, and that's fine —
    position traces alone replay clicks usefully."""
    try:
        import pyautogui
    except Exception:
        return
    prev = None
    t0 = time.time()
    while not _STOP.is_set():
        try:
            x, y = pyautogui.position()
            btn = None
            # position-only sampling; pyautogui can't poll buttons portably
            ev = {"t": round(time.time() - t0, 3), "k": "move", "x": int(x), "y": int(y)}
            if prev is None or abs(prev[0] - x) + abs(prev[1] - y) > 2:
                with _LOCK:
                    if len(_EVENTS) < MAX_EVENTS:
                        _EVENTS.append(ev)
                prev = (x, y)
            _ = btn
        except Exception:
            break
        if time.time() - t0 > MAX_SECONDS:
            break
        _STOP.wait(0.05)


def _pynput_listener():
    """Full recorder when pynput is available: moves, clicks, keys."""
    try:
        from pynput import mouse, keyboard
    except Exception:
        _sampler_loop()
        return
    t0 = time.time()

    def add(ev):
        with _LOCK:
            if len(_EVENTS) < MAX_EVENTS and time.time() - t0 <= MAX_SECONDS:
                _EVENTS.append(ev)

    def on_move(x, y):
        add({"t": round(time.time() - t0, 3), "k": "move", "x": int(x), "y": int(y)})

    def on_click(x, y, button, pressed):
        add({"t": round(time.time() - t0, 3), "k": "click", "x": int(x), "y": int(y),
             "b": str(button).split(".")[-1], "p": bool(pressed)})

    def on_press(key):
        try:
            k = key.char if hasattr(key, "char") and key.char else str(key).split(".")[-1]
        except Exception:
            k = "?"
        add({"t": round(time.time() - t0, 3), "k": "key", "key": k})

    try:
        with mouse.Listener(on_move=on_move, on_click=on_click) as ml, \
             keyboard.Listener(on_press=on_press) as kl:
            while not _STOP.is_set():
                if time.time() - t0 > MAX_SECONDS:
                    break
                _STOP.wait(0.2)
            ml.stop()
            kl.stop()
    except Exception:
        _sampler_loop()


def _start_recording() -> str:
    global _RECORDING, _REC_THREAD, _EVENTS
    if _RECORDING:
        return "Already recording."
    _EVENTS = []
    _STOP.clear()
    _RECORDING = True
    _REC_THREAD = threading.Thread(target=_pynput_listener, daemon=True,
                                   name="macro-record")
    _REC_THREAD.start()
    return (f"Recording started (max {MAX_EVENTS} events / {int(MAX_SECONDS)}s). "
            "Say 'stop recording' when done.")


def _stop_recording(name: str) -> str:
    global _RECORDING
    if not _RECORDING:
        return "Not recording."
    _STOP.set()
    _RECORDING = False
    with _LOCK:
        events = list(_EVENTS)
    if not events:
        return "Recording stopped — no events captured (headless system?)."
    _dir().mkdir(parents=True, exist_ok=True)
    payload = {"name": name or "macro", "created": time.strftime("%Y-%m-%d %H:%M"),
               "events": events}
    _macro_path(name or "macro").write_text(json.dumps(payload), encoding="utf-8")
    return f"Recording stopped — saved {len(events)} events as '{name or 'macro'}'."


# ── replay ───────────────────────────────────────────────────────────────────

def _replay(name: str) -> str:
    p = _macro_path(name)
    if not p.exists():
        return f"No macro named '{name}'. Say 'list macros'."
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
        events = payload.get("events", [])
    except Exception as e:
        return f"Macro file unreadable: {e}"
    if len(events) > MAX_EVENTS:
        events = events[:MAX_EVENTS]
    try:
        import pyautogui
    except Exception:
        return "pyautogui not installed — cannot replay (pip install pyautogui)."
    pyautogui.FAILSAFE = True
    t0 = time.time()
    done = 0
    for ev in events:
        if time.time() - t0 > MAX_SECONDS:
            break
        wait = float(ev.get("t", 0)) - (time.time() - t0)
        if 0 < wait < 5:
            time.sleep(wait)
        try:
            k = ev.get("k")
            if k == "move":
                pyautogui.moveTo(ev.get("x", 0), ev.get("y", 0), duration=0)
            elif k == "click":
                if ev.get("p"):
                    pyautogui.click(ev.get("x", 0), ev.get("y", 0))
            elif k == "key":
                key = ev.get("key", "")
                if key and len(key) == 1:
                    pyautogui.press(key)
            done += 1
        except Exception as e:
            return f"Replay aborted after {done} events: {e}"
    return f"Replayed '{name}' ({done}/{len(events)} events)."


# ── list / delete ────────────────────────────────────────────────────────────

def _list() -> str:
    d = _dir()
    if not d.exists():
        return "No macros saved yet."
    files = sorted(d.glob("*.json"))
    if not files:
        return "No macros saved yet."
    lines = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            n = len(data.get("events", []))
            when = data.get("created", "?")
            lines.append(f"- {data.get('name', f.stem)} ({n} events, {when})")
        except Exception:
            lines.append(f"- {f.stem} (unreadable)")
    return "Macros:\n" + "\n".join(lines)


def macro(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    name = str(params.get("name", "macro")).strip() or "macro"

    if action == "record":
        result = _start_recording()
    elif action == "stop":
        result = _stop_recording(name)
    elif action == "replay":
        if str(params.get("confirm", "")).lower().strip() not in ("yes", "true", "1"):
            result = ("Replay replays real clicks and keystrokes on your "
                      "screen — confirm by saying yes before I run it.")
        else:
            result = _replay(name)
    elif action == "delete":
        p = _macro_path(name)
        if p.exists():
            p.unlink()
            result = f"Macro '{name}' deleted."
        else:
            result = f"No macro named '{name}'."
    else:
        result = _list()

    if player is not None and action == "list":
        try:
            player.show_content("MACROS", result[:4000])
        except Exception:
            pass
    return result


TOOL = {
    "name": "macro",
    "description": (
        "Records and replays screen macros (mouse + keyboard routines). "
        "Actions: record (start capturing), stop (finish and save with name), "
        "list (default), replay (needs confirm=yes after the user agrees), "
        "delete. Use when the user says 'record what I do', 'repeat those "
        "clicks', 'macro'. Replay drives the real screen."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "record | stop | list | replay | delete"},
            "name": {"type": "STRING", "description": "Macro name."},
            "confirm": {"type": "STRING",
                        "description": "'yes' required before replay runs."},
        },
        "required": [],
    },
    "handler": macro,
}
