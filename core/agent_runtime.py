# core/agent_runtime.py
"""Cooperative cancel registry for long-running agent runs.

A run (a task_agent execution, a gui_agent sweep) registers its key before
it starts and polls `is_cancelled()` at STEP BOUNDARIES — never mid-step.
The flag is set by a parallel tool call (main dispatches tool calls through
asyncio.gather, so `task_agent action=cancel` interleaves with a running
mission), by the dashboard, or by the UI.

Why cooperative: killing threads mid-write is how you corrupt taskstore or
half-apply a file operation. Step boundaries are the safe points, and the
existing steps are all bounded by their own timeouts anyway.

Thread-safe; entries are removed by finish() so the set stays bounded.
"""
from __future__ import annotations

from threading import Lock

_LOCK = Lock()
_CANCELLED: set[str] = set()
_ACTIVE: set[str] = set()


def begin(key: str) -> None:
    """Mark a run active and (re)clear any stale cancel flag."""
    key = str(key)
    with _LOCK:
        _ACTIVE.add(key)
        _CANCELLED.discard(key)


def cancel(key: str) -> bool:
    """Request cancellation. True if the run was active and is now flagged;
    False if there was nothing to cancel (finished / unknown key)."""
    key = str(key)
    with _LOCK:
        if key not in _ACTIVE:
            return False
        _CANCELLED.add(key)
        return True


def is_cancelled(key: str) -> bool:
    with _LOCK:
        return str(key) in _CANCELLED


def finish(key: str) -> None:
    key = str(key)
    with _LOCK:
        _ACTIVE.discard(key)
        _CANCELLED.discard(key)


def active() -> list[str]:
    with _LOCK:
        return sorted(_ACTIVE)


def reset_for_tests() -> None:
    with _LOCK:
        _ACTIVE.clear()
        _CANCELLED.clear()
