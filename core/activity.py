"""
Activity log — the data layer behind the Mission Control timeline.

Every tool call in the session appends an event here: what ran, with what
(summarised), how long it took, whether it succeeded, and a short preview of
the result. The timeline view (actions/mission.py), the dashboard activity
feed, and any future panel all read from this one ring buffer.

Deliberately dependency-free and thread-safe: tools run in executor threads,
the dashboard runs in asyncio, and the UI reads from its own thread — so the
only correct design is a lock around a bounded deque.

This module must never import UI, dashboard, or action code: it sits at the
bottom of the dependency graph on purpose.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

MAX_EVENTS = 500          # ring size — a busy session rolls over, never grows
_PREVIEW = 240             # chars of result kept per event


@dataclass
class ActivityEvent:
    """One tool call (or a synthetic step — see note() / milestone())."""
    kind: str                     # "tool" | "note" | "task" | "mirror"
    name: str                     # tool name or label
    started: float                # time.time() when it began
    duration: float = -1.0        # seconds; -1 while still running
    ok: bool | None = None        # None while running, then True/False
    args: dict = field(default_factory=dict)
    preview: str = ""             # truncated result / error
    steps: int = 0                # sub-steps completed (tasks/orchestrator)

    @property
    def running(self) -> bool:
        return self.duration < 0

    def when(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.started))

    def summary(self) -> str:
        """Args reduced to a one-line summary — never dump raw payloads."""
        if not self.args:
            return ""
        parts = []
        for k, v in self.args.items():
            s = str(v).replace("\n", " ").strip()
            if len(s) > 60:
                s = s[:57] + "…"
            if s:
                parts.append(f"{k}={s}")
        return ", ".join(parts)[:160]


_lock = threading.Lock()
_events: deque[ActivityEvent] = deque(maxlen=MAX_EVENTS)
_seq = 0
_listeners: list = []            # callables(event) — dashboard mirror etc.


# ── writing ──────────────────────────────────────────────────────────────────

def begin(kind: str, name: str, args: dict | None = None) -> ActivityEvent:
    """Record the start of an operation. Returns the event (finish it later)."""
    global _seq
    ev = ActivityEvent(kind=kind, name=name, started=time.time(),
                       args=dict(args or {}))
    with _lock:
        _seq += 1
        _events.append(ev)
        listeners = list(_listeners)
    _notify(listeners, ev)
    return ev


def finish(ev: ActivityEvent, ok: bool, result=None) -> ActivityEvent:
    """Close an event started by begin(). Result is stored truncated."""
    ev.duration = max(0.0, time.time() - ev.started)
    ev.ok = bool(ok)
    if result is not None:
        text = result if isinstance(result, str) else repr(result)
        ev.preview = text[:_PREVIEW].replace("\n", " ").strip()
    with _lock:
        listeners = list(_listeners)
    _notify(listeners, ev)
    return ev


def fail(ev: ActivityEvent, error) -> ActivityEvent:
    return finish(ev, False, str(error))


def note(name: str, text: str = "", steps: int = 0) -> ActivityEvent:
    """Milestone marker (plan created, step done, screen mirrored, …)."""
    ev = ActivityEvent(kind="note", name=name, started=time.time(),
                       duration=0.0, ok=True, preview=text[:_PREVIEW],
                       steps=steps)
    with _lock:
        _events.append(ev)
        listeners = list(_listeners)
    _notify(listeners, ev)
    return ev


# ── reading ──────────────────────────────────────────────────────────────────

def recent(n: int = 30) -> list[ActivityEvent]:
    with _lock:
        items = list(_events)
    return items[-n:]


def running() -> list[ActivityEvent]:
    with _lock:
        items = list(_events)
    return [e for e in items if e.running]


def stats() -> dict:
    with _lock:
        items = list(_events)
    tools = [e for e in items if e.kind == "tool" and not e.running]
    return {
        "total": len(items),
        "tool_calls": len(tools),
        "ok": sum(1 for e in tools if e.ok),
        "failed": sum(1 for e in tools if e.ok is False),
        "running": sum(1 for e in items if e.running),
    }


def timeline(n: int = 30) -> str:
    """Plain-text timeline — the fallback the mission action renders."""
    lines = []
    for e in recent(n):
        mark = "…" if e.running else ("✓" if e.ok else "✗")
        dur = "" if e.running else f" {e.duration:.1f}s"
        args = e.summary()
        line = f"[{e.when()}] {mark} {e.name}{dur}"
        if args:
            line += f" ({args})"
        if e.preview and (not e.ok or e.kind == "note"):
            line += f" — {e.preview[:120]}"
        lines.append(line)
    return "\n".join(lines) or "(no activity yet)"


def clear() -> None:
    with _lock:
        _events.clear()


# ── listeners (dashboard mirror) ─────────────────────────────────────────────

def add_listener(fn) -> None:
    """fn(event) called on begin/finish/note. Exceptions are swallowed —
    a broken listener must never take a tool call down with it."""
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def remove_listener(fn) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def _notify(listeners, ev) -> None:
    for fn in listeners:
        try:
            fn(ev)
        except Exception:
            pass
