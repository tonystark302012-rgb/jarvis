"""
focus — focus sessions & pomodoro timer.

A focus session mutes proactive check-ins and background topic alerts (via
the injected muter/notifier), runs work/break intervals, and reports status.
Everything is a plain timer — no UI dependency; the app wires the callbacks.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

_LOCK = threading.Lock()
_SESSION: dict | None = None
_TIMER: threading.Thread | None = None
_STOP = threading.Event()
_ON_PHASE: Callable[[str, int], None] | None = None   # (phase, minutes)
_ON_MUTE: Callable[[bool], None] | None = None


def set_callbacks(on_phase=None, on_mute=None) -> None:
    global _ON_PHASE, _ON_MUTE
    _ON_PHASE = on_phase
    _ON_MUTE = on_mute


def _announce(phase: str, minutes: int) -> None:
    if _ON_PHASE:
        try:
            _ON_PHASE(phase, minutes)
        except Exception:
            pass


def _loop():
    global _SESSION
    while not _STOP.is_set():
        with _LOCK:
            s = _SESSION
        if not s or not s.get("running"):
            break
        remaining = s["ends_at"] - time.time()
        if remaining <= 0:
            # switch phase
            s["phase"] = "break" if s["phase"] == "focus" else "focus"
            minutes = s["break_min"] if s["phase"] == "break" else s["work_min"]
            s["ends_at"] = time.time() + minutes * 60
            s["rounds"] += 1 if s["phase"] == "break" else 0
            if _ON_MUTE:
                try:
                    _ON_MUTE(s["phase"] == "focus")
                except Exception:
                    pass
            _announce(s["phase"], minutes)
        _STOP.wait(5)


def start_session(work_min: int, break_min: int, rounds: int = 4) -> str:
    global _SESSION, _TIMER
    stop_session(quiet=True)
    work_min = max(1, min(120, int(work_min)))
    break_min = max(1, min(30, int(break_min)))
    rounds = max(1, min(12, int(rounds)))
    _STOP.clear()
    with _LOCK:
        _SESSION = {
            "running": True, "phase": "focus", "work_min": work_min,
            "break_min": break_min, "rounds": rounds, "round": 1,
            "ends_at": time.time() + work_min * 60,
            "started": time.strftime("%H:%M"),
        }
    if _ON_MUTE:
        try:
            _ON_MUTE(True)
        except Exception:
            pass
    _TIMER = threading.Thread(target=_loop, daemon=True, name="focus")
    _TIMER.start()
    _announce("focus", work_min)
    return (f"Focus session started: {work_min} min work, {break_min} min "
            f"break, {rounds} rounds. Proactive alerts are muted while you "
            f"work.")


def stop_session(quiet: bool = False) -> str:
    global _SESSION
    _STOP.set()
    with _LOCK:
        had = _SESSION is not None and _SESSION.get("running")
        _SESSION = None
    if _ON_MUTE:
        try:
            _ON_MUTE(False)
        except Exception:
            pass
    if quiet:
        return ""
    return "Focus session ended. Alerts unmuted." if had else "No focus session running."


def status() -> str:
    with _LOCK:
        s = dict(_SESSION) if _SESSION else None
    if not s or not s.get("running"):
        return "No focus session running."
    left = max(0, int(s["ends_at"] - time.time()))
    mins, secs = divmod(left, 60)
    return (f"{s['phase'].title()} — {mins}:{secs:02d} left "
            f"(round {s['round']}/{s['rounds']}, work {s['work_min']}m / "
            f"break {s['break_min']}m, started {s['started']}).")


def focus(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()

    if action == "start":
        try:
            work = int(params.get("work_min", 25))
            brk = int(params.get("break_min", 5))
            rounds = int(params.get("rounds", 4))
        except (TypeError, ValueError):
            work, brk, rounds = 25, 5, 4
        result = start_session(work, brk, rounds)
    elif action in ("stop", "end", "cancel"):
        result = stop_session()
    else:
        result = status()

    if player is not None:
        try:
            player.show_content("FOCUS", result)
        except Exception:
            pass
    return result


TOOL = {
    "name": "focus",
    "description": (
        "Focus/pomodoro sessions. Actions: start (work_min, break_min, "
        "rounds — mutes proactive alerts during work phases), status "
        "(default), stop (ends session, unmutes). Use when the user wants "
        "to concentrate, start a pomodoro, or asks about the current focus "
        "timer."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "start | status | stop"},
            "work_min": {"type": "STRING", "description": "Work minutes (1-120)."},
            "break_min": {"type": "STRING", "description": "Break minutes (1-30)."},
            "rounds": {"type": "STRING", "description": "Rounds 1-12."},
        },
        "required": [],
    },
    "handler": focus,
}
