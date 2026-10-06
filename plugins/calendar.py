"""
Calendar from an ICS feed — free, no OAuth, works with Google Calendar's
secret iCal address (or any CalDAV/IcsAlarm/Cronofy/etc. feed).

Setup (one time): in Google Calendar → Settings → your calendar →
"Integrate calendar" → copy the **secret address in iCal format**, then
say 'calendar setup <url>' or put it in config/api_keys.json as
`"calendar_ics_url": "https://calendar.google.com/calendar/ical/…/basic.ics"`.

The feed is fetched read-only over HTTPS and parsed with the standard
library only (no icalendar package). Recurrence is expanded for simple
daily/weekly patterns; complex rules are listed as their next occurrence.

Actions:
  upcoming — events from now through `days` ahead (default 7)
  today    — just today's events
  on DATE  — events on an explicit date (YYYY-MM-DD)
  setup    — store the ICS URL
"""
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN = {
    "name": "calendar",
    "description": (
        "Read upcoming calendar events from an ICS feed (Google Calendar's "
        "secret iCal URL — set up once via calendar action=setup). Actions: "
        "upcoming (default, next `days` days), today, date (events on an "
        "explicit YYYY-MM-DD), setup (store feed URL). Use for 'what's on "
        "my calendar', 'am I free Friday', 'meetings today'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "upcoming | today | date | setup"},
            "date": {"type": "STRING",
                     "description": "YYYY-MM-DD for the date action"},
            "days": {"type": "INTEGER",
                     "description": "Days ahead for upcoming — default 7"},
            "url": {"type": "STRING", "description": "ICS feed URL for setup"},
        },
        "required": [],
    },
}


# ── config ───────────────────────────────────────────────────────────────────

def _cfg_path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "api_keys.json"


def _get_url() -> str:
    try:
        data = json.loads(_cfg_path().read_text(encoding="utf-8"))
        return str(data.get("calendar_ics_url") or "").strip()
    except Exception:
        return ""


def _set_url(url: str) -> None:
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data["calendar_ics_url"] = url
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── ICS parsing (stdlib) ─────────────────────────────────────────────────────

def _unfold(raw: str) -> list[str]:
    """RFC 5545 §3.1 unfolding — a CRLF followed by one whitespace char
    is REMOVED and the parts JOINED, so a folded SUMMARY arrives back as
    one logical line (stripping the indent alone would lose the tail)."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    for line in text.split("\n"):
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]           # continuation — join to previous
        elif line.strip():
            out.append(line)
    return out


def _parse_dt(value: str, params: str = ""):
    """'20261003T143000Z' | '20261003' (+ VALUE=DATE) → naive LOCAL datetime.

    Everything is normalised to naive local time so window comparisons
    (which callers build with plain datetime()) can never hit the
    aware-vs-naive TypeError that mixed feeds used to raise.
    """
    value = value.strip()
    if "VALUE=DATE" in params.upper() and re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d")
    if re.fullmatch(r"\d{8}T\d{6}Z", value):
        aware = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc).astimezone()
        return aware.replace(tzinfo=None)
    if re.fullmatch(r"\d{8}T\d{6}", value):
        return datetime.strptime(value, "%Y%m%dT%H%M%S")
    if re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d")
    raise ValueError(f"unparseable date: {value!r}")


def _parse_rrule(rule: str) -> dict:
    out = {}
    for part in rule.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().upper()] = v.strip()
    return out


def _expand(event: dict, window_start: datetime, window_end: datetime) -> list[datetime]:
    """Start datetimes that fall inside the window (simple RRULE support)."""
    start = event["start"]
    if not event.get("rrule"):
        return [start] if window_start <= start < window_end else []
    rule = event["rrule"]
    freq = (rule.get("FREQ") or "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    until = None
    if rule.get("UNTIL"):
        try:
            until = _parse_dt(rule["UNTIL"])
        except ValueError:
            until = None
    count = None
    if rule.get("COUNT"):
        try:
            count = int(rule["COUNT"])
        except ValueError:
            count = None

    hits, cur, n = [], start, 0
    # bounded iteration — a runaway rule must not hang the assistant
    while n < 1000 and cur < window_end and (until is None or cur <= until):
        n += 1
        if count is not None and n > count:
            break
        if window_start <= cur < window_end:
            hits.append(cur)
        if freq == "DAILY":
            cur = cur + timedelta(days=interval)
        elif freq == "WEEKLY":
            cur = cur + timedelta(weeks=interval)
        elif freq == "MONTHLY":
            month = cur.month - 1 + interval
            cur = cur.replace(year=cur.year + month // 12,
                              month=month % 12 + 1)
        else:
            break                       # daily/weekly/monthly cover real use
    return hits


def parse_ics(raw: str, window_start: datetime, window_end: datetime) -> list[dict]:
    """Events in [window_start, window_end), sorted by start."""
    events: list[dict] = []
    cur: dict | None = None
    in_event = False
    for line in _unfold(raw):
        if line.startswith("BEGIN:VEVENT"):
            cur, in_event = {}, True
            continue
        if line.startswith("END:VEVENT"):
            if cur is not None and cur.get("start"):
                for st in _expand(cur, window_start, window_end):
                    events.append({
                        "start": st,
                        "end": cur.get("end") or st,
                        "summary": cur.get("summary") or "(no title)",
                        "location": cur.get("location") or "",
                        "allday": cur.get("allday", False),
                    })
            cur, in_event = None, False
            continue
        if not in_event or cur is None or ":" not in line:
            continue
        head, _, value = line.partition(":")
        name = head.split(";")[0].strip().upper()
        if name == "DTSTART":
            try:
                cur["start"] = _parse_dt(value, head)
                cur["allday"] = "VALUE=DATE" in head.upper() and \
                    "T" not in value
            except ValueError:
                pass
        elif name == "DTEND":
            try:
                cur["end"] = _parse_dt(value, head)
            except ValueError:
                pass
        elif name == "SUMMARY":
            cur["summary"] = value.replace("\\,", ",").replace("\\n", " ")
        elif name == "LOCATION":
            cur["location"] = value.replace("\\,", ",")
        elif name == "RRULE":
            cur["rrule"] = _parse_rrule(value)
    events.sort(key=lambda e: e["start"])
    return events


# ── fetch ────────────────────────────────────────────────────────────────────

def _fetch_ics(url: str, timeout: int = 20) -> str:
    if not url.lower().startswith(("https://", "http://")):
        raise ValueError("calendar URL must be http(s)")
    req = urllib.request.Request(url, headers={
        "User-Agent": "JarvisCalendar/1.0 (+offline assistant)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(4_000_000)          # 4 MB cap — calendars are small
    return data.decode("utf-8", errors="replace")


def _fmt(ev: dict, now: datetime) -> str:
    st = ev["start"]
    same_day = st.date() == now.date()
    if ev.get("allday"):
        when = f"all day {st.strftime('%a %d %b')}"
    elif same_day:
        when = f"today {st.strftime('%H:%M')}"
    elif st.date() == (now + timedelta(days=1)).date():
        when = f"tomorrow {st.strftime('%H:%M')}"
    else:
        when = st.strftime("%a %d %b %H:%M")
    tail = f" @ {ev['location']}" if ev.get("location") else ""
    return f"• {when} — {ev['summary']}{tail}"


def _list_events(start: datetime, end: datetime) -> str:
    url = _get_url()
    if not url:
        return ("Calendar isn't set up yet. Say 'calendar setup <ICS URL>' "
                "— Google Calendar → Settings → your calendar → secret "
                "address in iCal format.")
    try:
        raw = _fetch_ics(url)
    except Exception as e:
        return f"Couldn't fetch the calendar feed: {e}"
    try:
        events = parse_ics(raw, start, end)
    except Exception as e:
        return f"Couldn't parse the calendar feed: {e}"
    if not events:
        span = (end - start).days
        return f"No events in the next {span} day(s)." if span > 1 \
            else "No events for that day."
    now = datetime.now().astimezone() if start.tzinfo else datetime.now()
    lines = [_fmt(e, now) for e in events[:25]]
    header = f"Calendar ({len(events)} event(s) from {start.strftime('%d %b')}):"
    more = f"\n…and {len(events) - 25} more." if len(events) > 25 else ""
    return header + "\n" + "\n".join(lines) + more


# ── entry ────────────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "upcoming").lower().strip()
    try:
        if action == "setup":
            url = str(params.get("url") or "").strip()
            if not url.lower().startswith(("https://", "http://")):
                return "Setup needs an http(s) ICS URL — paste the secret iCal address."
            _set_url(url)
            # verify it actually parses
            try:
                raw = _fetch_ics(url)
                evs = parse_ics(raw, datetime.now(), datetime.now() + timedelta(days=1))
                return (f"Calendar feed saved and verified — "
                        f"{len(evs)} event(s) in the next 24h.")
            except Exception as e:
                return f"URL saved, but the fetch/parse check failed: {e}"
        if action == "today":
            now = datetime.now()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return _list_events(start, start + timedelta(days=1))
        if action == "date":
            ds = str(params.get("date") or "").strip()
            try:
                d = datetime.strptime(ds, "%Y-%m-%d")
            except ValueError:
                return "Give the date as YYYY-MM-DD."
            return _list_events(d, d + timedelta(days=1))
        # upcoming
        days = 7
        try:
            days = max(1, min(60, int(params.get("days") or 7)))
        except (TypeError, ValueError):
            pass
        return _list_events(datetime.now(), datetime.now() + timedelta(days=days))
    except Exception as e:                      # never raise to the loader
        return f"Calendar failed: {e}"
