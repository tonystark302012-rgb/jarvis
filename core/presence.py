"""presence.py — is the user actually at the machine?

Zero new dependencies: two honest signals, no camera, no OS hooks that
don't exist.

1. **JARVIS activity** — every input path in main.py goes through
   `_fire_phrase_rules` (HUD text box, live voice transcript, phone
   command box), which calls `note_activity()`. No interaction for
   `away_after` seconds → we transition to `away`.
2. **OS idle probe (best-effort)** — platform-native "seconds since last
   keyboard/mouse input" where one exists without installing anything:
   Windows `GetLastInputInfo`, macOS `ioreg HIDIdleTime`, Linux
   `xprintidle` if it happens to be on PATH. Returns `None` when
   unavailable → activity-only tracking (never a hard error).

Transition semantics (flap-safe):
- present → away  : `poll()` reports it once, when effective idle first
  reaches `away_after`.
- away → present  : an activity note or the OS probe dropping below
  `away_after / 2` (hysteresis — half the threshold, so a single key
  press at the boundary doesn't flip us back and forth).

`note_activity()` / `poll()` return the edge string or None; they never
raise (presence must never break an input path). State is a module-level
singleton so main.py, the presence action, and the proactive gate all
read the same tracker.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

_AWAY_AFTER_S = 300.0          # 5 minutes with zero interaction → away
_HISTORY_MAX = 50


def _os_idle_seconds() -> Optional[float]:
    """Best-effort OS-level idle seconds (user not touching keyboard/
    mouse). None = probe unavailable → callers fall back to JARVIS
    activity. Never raises."""
    try:
        if sys.platform.startswith("win"):
            import ctypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", ctypes.c_uint),
                    ("dwTime", ctypes.c_uint),
                ]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                now_ms = ctypes.windll.kernel32.GetTickCount()
                idle_ms = (now_ms - info.dwTime) & 0xFFFFFFFF
                return idle_ms / 1000.0
        elif sys.platform == "darwin":
            out = subprocess.run(
                ["ioreg", "-c", "IOHIDSystem"],
                capture_output=True, text=True, timeout=2,
            )
            for line in out.stdout.splitlines():
                if "HIDIdleTime" in line and "=" in line:
                    ns = int(line.rsplit("=", 1)[1].strip().rstrip(","))
                    return ns / 1_000_000_000.0
        else:
            # Linux/X11 — only if the idle tool already exists. Wayland /
            # headless → None (honest fallback to activity tracking).
            out = subprocess.run(
                ["xprintidle"], capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0:
                return int(out.stdout.strip()) / 1000.0
    except Exception:
        return None
    return None


class PresenceTracker:
    """Flap-safe present/away state machine. Thread-safe (single lock
    around the small mutable state); every public call is non-raising."""

    def __init__(self, away_after: float = _AWAY_AFTER_S):
        self._away_after = float(away_after)
        self._present = True
        self._last_activity = time.monotonic()
        self._last_os_probe: Optional[float] = None   # os idle at last poll
        self._since = time.time()                     # current-state since
        self.history: deque = deque(maxlen=_HISTORY_MAX)
        self._lock: Any = None          # a real Lock below, in practice
        try:
            import threading
            self._lock = threading.Lock()
        except Exception:                              # pragma: no cover
            pass                        # single-threaded fallback: no lock

    # ── inputs ────────────────────────────────────────────────────────
    def note_activity(self, kind: str = "input") -> Optional[str]:
        """Called from every user-input path. Returns 'present' only on
        the away→present edge, else None. Never raises."""
        try:
            now = time.monotonic()
            edge = None
            with self._lock:
                self._last_activity = now
                if not self._present:
                    self._present = True
                    self._since = time.time()
                    edge = "present"
                    self._record(edge, kind)
            return edge
        except Exception:
            return None

    def poll(self, now: Optional[float] = None) -> Optional[str]:
        """Check for the present→away edge. Returns 'away' once when the
        threshold is first crossed, else None. Never raises."""
        try:
            now = time.monotonic() if now is None else now
            os_idle = _os_idle_seconds()
            edge = None
            with self._lock:
                # Effective idle: the SMALLER of (our own quiet time) and
                # (OS-reported user idle) when the OS probe exists.
                own_idle = now - self._last_activity
                if os_idle is None:
                    idle = own_idle
                else:
                    idle = min(own_idle, os_idle)
                self._last_os_probe = os_idle
                if self._present:
                    if idle >= self._away_after:
                        self._present = False
                        self._since = time.time()
                        edge = "away"
                        self._record(edge, f"idle {int(idle)}s")
                else:
                    # Hysteresis on the way back: probe must be clearly
                    # fresh (half threshold) or our own input (which
                    # note_activity already handled, but poll can race it).
                    if os_idle is not None and os_idle < self._away_after / 2:
                        self._present = True
                        self._since = time.time()
                        edge = "present"
                        self._record(edge, "os activity")
            return edge
        except Exception:
            return None

    # ── reads ─────────────────────────────────────────────────────────
    def is_present(self, now: Optional[float] = None) -> bool:
        try:
            with self._lock:
                return bool(self._present)
        except Exception:
            return True        # fail open — never block the assistant

    def status(self, now: Optional[float] = None) -> dict:
        try:
            now_m = time.monotonic() if now is None else now
            with self._lock:
                own_idle = max(0.0, now_m - self._last_activity)
                return {
                    "present": bool(self._present),
                    "state": "present" if self._present else "away",
                    "idle_seconds": round(own_idle, 1),
                    "os_idle_seconds": (
                        None if self._last_os_probe is None
                        else round(self._last_os_probe, 1)
                    ),
                    "away_after_seconds": self._away_after,
                    "since": datetime.fromtimestamp(
                        self._since, tz=timezone.utc
                    ).isoformat(),
                    "input_seen": self._last_activity > 0,
                    "history": list(self.history)[-10:],
                }
        except Exception as e:
            return {"present": True, "state": "unknown", "error": str(e)}

    def set_away_after(self, seconds: float) -> float:
        with self._lock:
            self._away_after = max(1.0, float(seconds))
            return self._away_after

    def force(self, state: str, why: str = "manual") -> bool:
        """Manually set present/away (action-tool seam). Returns True
        only when this call actually changed the state."""
        try:
            want = bool(state == "present")
            changed = False
            with self._lock:
                if self._present != want:
                    self._present = want
                    self._since = time.time()
                    self._record("present" if want else "away", why)
                    changed = True
            return changed
        except Exception:
            return False

    # ── internals ─────────────────────────────────────────────────────
    def _record(self, state: str, why: str) -> None:
        self.history.append(
            {
                "state": state,
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "why": why,
            }
        )


_TRACKER: Optional[PresenceTracker] = None


def tracker() -> PresenceTracker:
    """Module-level singleton — main.py, actions/presence.py and the
    proactive gate all share one state."""
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = PresenceTracker()
    return _TRACKER
