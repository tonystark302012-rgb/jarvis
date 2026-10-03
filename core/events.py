# core/events.py
"""EventEngine — event-driven proactivity: edge triggers, not timers.

Roadmap: "Real Proactivity: battery low pe alert, calendar 10 min pehle —
event-driven (battery/calendar triggers, not timers)."

The difference from a timer: a timer SAYS something every N minutes; an
event engine fires ONCE when a STATE CROSSES a threshold, then goes quiet
until the state moves again.

    battery : discharging crosses 20% → one alert; crosses 10% → one
              alert; becomes fully charged → one alert. Hysteresis +
              last-fired level tracking so a poll at 19.9% every 60 s
              does NOT nag. psutil.sensors_battery() (already a dep).
    calendar: an event entering the T-10-min window fires once, keyed
              by (uid, start, lead) — the ICS feed is read via the
              existing calendar plugin (Google's ICS = free, no OAuth).
              A refresh that re-serves the same event cannot refire it.

Seams: `_battery()` (psutil) and `_ics_events(window)` (plugins.calendar)
are instance-overridable — tests inject fakes, production uses real ones.

Persistence of fired keys: memory/events_fired.json (bounded ring of the
last 200 keys) so a restart an hour before the meeting still knows it
already warned you.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

# battery level bands (percent). Crossing DOWN through a level fires.
_BATTERY_LEVELS = (20, 10)
_CALENDAR_LEADS_MIN = (10, 60)      # fire 60 min and 10 min before start
_STATE_FILE_MAX_KEYS = 200


@dataclass
class Event:
    key: str                         # stable id → dedup
    kind: str                        # "battery" | "calendar"
    message: str
    severity: str = "info"           # info | warn | critical
    meta: dict = field(default_factory=dict)


class EventEngine:
    """Poll `poll()`; it returns only NEW edge-crossing events."""

    def __init__(self, state_path: Path | None = None):
        self._lock = Lock()
        self._last_pct: int | None = None          # battery edge tracker
        self._last_plugged: bool | None = None
        self._fired: dict[str, float] = {}         # key → monotonic ts
        if state_path is None:
            from config import get_base_dir
            state_path = get_base_dir() / "memory" / "events_fired.json"
        self._state_path = Path(state_path)
        self._load_state()

    # ── persistence (best-effort; a corrupt file must not kill proactivity)
    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._fired = {str(k): float(v) for k, v in data.items()
                               if isinstance(v, (int, float))}
        except Exception:
            self._fired = {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            # ring: drop oldest beyond cap, then write
            if len(self._fired) > _STATE_FILE_MAX_KEYS:
                for k, _ in sorted(self._fired.items(),
                                   key=lambda kv: kv[1])[
                        :len(self._fired) - _STATE_FILE_MAX_KEYS]:
                    self._fired.pop(k, None)
            self._state_path.write_text(json.dumps(self._fired),
                                        encoding="utf-8")
        except Exception:
            pass

    def _already(self, key: str) -> bool:
        """Fired recently (within ttl 12 h)? Battery level keys may refire
        the next day; calendar keys are date-qualified so they expire."""
        ts = self._fired.get(key)
        if ts is None:
            return False
        return (time.monotonic() - ts) < 43_200

    def _mark(self, key: str) -> None:
        self._fired[key] = time.monotonic()

    # ── seams ────────────────────────────────────────────────────────────
    def _battery(self):
        """(percent:int|None, charging:bool). Default: psutil seam."""
        try:
            import psutil
        except ImportError:
            return None, False
        try:
            b = psutil.sensors_battery()
        except Exception:
            return None, False
        if b is None:
            return None, False
        pct = int(b.percent) if b.percent is not None else None
        charging = bool(b.power_plugged)
        return pct, charging

    def _ics_events(self, window_start: datetime,
                    window_end: datetime) -> list[dict]:
        """Upcoming events [{uid,title,start,end}] from the configured
        ICS feed. Default: plugins.calendar seam; empty when unset."""
        try:
            from plugins import calendar as cal
            url = cal._get_url()
            if not url:
                return []
            raw = cal._fetch_ics(url, timeout=15)
            return cal.parse_ics(raw, window_start, window_end)
        except Exception:
            return []

    # ── the poll ─────────────────────────────────────────────────────────
    def poll(self, now: datetime | None = None) -> list[Event]:
        now = now or datetime.now()
        out: list[Event] = []
        with self._lock:
            out.extend(self._poll_battery())
            out.extend(self._poll_calendar(now))
            if out:
                self._save_state()
        return out

    def _poll_battery(self) -> list[Event]:
        pct, charging = self._battery()
        if pct is None:
            self._last_pct = None
            return []
        out: list[Event] = []
        prev, self._last_pct = self._last_pct, pct
        prev_plug, self._last_plugged = self._last_plugged, charging

        # charge-complete edge: was plugged & lower, now 100% & still plugged
        if (charging and pct >= 100 and prev is not None
                and prev < 100 and (prev_plug is not False)):
            key = f"battery:full:{datetime.now():%Y-%m-%d}"
            if not self._already(key):
                self._mark(key)
                out.append(Event(key, "battery",
                                 "Laptop battery fully charged — "
                                 "you can unplug now.",
                                 "info", {"pct": pct}))
        if charging or prev is None:
            return out                      # edge baseline only when discharging

        # downward crossings: prev > level >= pct
        for level in _BATTERY_LEVELS:
            if prev > level >= pct:
                key = f"battery:{level}:{datetime.now():%Y-%m-%d}"
                if self._already(key):
                    continue
                self._mark(key)
                if level <= 10:
                    out.append(Event(
                        key, "battery",
                        f"Battery critical at {pct}% — plug in NOW or "
                        f"save your work.",
                        "critical", {"pct": pct, "level": level}))
                else:
                    out.append(Event(
                        key, "battery",
                        f"Heads up — battery is at {pct}%. "
                        f"Plug in when convenient.",
                        "warn", {"pct": pct, "level": level}))
        return out

    def _poll_calendar(self, now: datetime) -> list[Event]:
        # one fetch covers the longest lead; filter per lead afterwards
        max_lead = max(_CALENDAR_LEADS_MIN)
        window_end = now + timedelta(minutes=max_lead + 1)
        try:
            events = self._ics_events(now - timedelta(minutes=5), window_end)
        except Exception:
            return []
        out: list[Event] = []
        for ev in events:
            start = ev.get("start")
            if not isinstance(start, datetime):
                continue
            mins = (start - now).total_seconds() / 60.0
            if mins < -1 or mins > max_lead:
                continue
            uid = str(ev.get("uid") or ev.get("title") or "")[:80]
            day = start.strftime("%Y-%m-%d")
            title = str(ev.get("title") or "Event")
            # nearest applicable lead FIRST — then dedup on that key only,
            # so a fired T-10 can't fall through to a T-60 refire.
            nearest = next((l for l in sorted(_CALENDAR_LEADS_MIN)
                            if mins <= l), None)
            if nearest is None:
                continue
            key = f"cal:{uid}:{day}:T-{nearest}"
            if self._already(key):
                continue
            self._mark(key)
            when = start.strftime("%H:%M")
            if nearest <= 10:
                msg = (f"Reminder: {title} starts at {when} — "
                       f"about {max(1, int(mins))} minutes from now.")
                sev = "warn"
            else:
                msg = (f"Calendar: {title} is at {when} "
                       f"(in ~{int(mins)} min).")
                sev = "info"
            out.append(Event(key, "calendar", msg, sev,
                             {"title": title, "start": when,
                              "minutes": int(mins)}))
        return out

    # ── test/control helpers ─────────────────────────────────────────────
    def reset(self) -> None:
        with self._lock:
            self._last_pct = None
            self._last_plugged = None
            self._fired.clear()
            self._save_state()

    def fired_keys(self) -> list[str]:
        return sorted(self._fired)


__all__ = ["Event", "EventEngine"]
