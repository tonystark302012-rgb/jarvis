"""
sky — free keyless facts about space and seismology.

  iss      — current ISS position (api.open-notify.org)
  quakes   — last 24 h of earthquakes, bucketed by magnitude
             (earthquake.usgs.gov geojson feeds)
  suntimes — sunrise/sunset for a place and date (sunrise-sunset.org)

All three are public no-auth endpoints. Each network failure is reported
per action with the real reason; nothing is invented offline.
"""
from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
import urllib.request


def _get(url: str, timeout: float = 20.0) -> bytes:
    """Fetch seam — tests monkeypatch this instead of hitting the network."""
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _json(url: str, timeout: float = 20.0):
    raw = _get(url, timeout)
    return json.loads(raw.decode("utf-8", "replace"))


def _coord(parameters: dict, key: str) -> float | None:
    try:
        return float((parameters or {}).get(key))
    except (TypeError, ValueError):
        return None


def _iss(parameters: dict) -> str:
    data = _json("http://api.open-notify.org/iss-now.json")
    pos = data.get("iss_position") or {}
    lat, lon = pos.get("latitude"), pos.get("longitude")
    if lat is None or lon is None:
        return f"ISS feed answered without a position: {str(data)[:120]}"
    stamp = _dt.datetime.utcfromtimestamp(
        int(data.get("timestamp") or 0)).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"ISS at lat {float(lat):+.4f}, lon {float(lon):+.4f} ({stamp})"


_QUAKE_BUCKETS = ("1.0", "2.5", "4.5", "significant")


def _quakes(parameters: dict) -> str:
    p = parameters or {}
    mag = str(p.get("min_magnitude") or "2.5").strip()
    if mag not in _QUAKE_BUCKETS:
        try:
            f = float(mag)
            mag = ("1.0" if f < 2.5 else "2.5" if f < 4.5
                   else "4.5" if f < 6.0 else "significant")
        except ValueError:
            mag = "2.5"
    try:
        limit = max(1, min(25, int(p.get("limit") or 5)))
    except (TypeError, ValueError):
        limit = 5
    url = (f"https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
           f"{mag}_day.geojson")
    data = _json(url)
    feats = list(data.get("features") or [])
    if not feats:
        return f"No M{mag}+ earthquakes in the last 24 h (USGS feed empty)."
    lines = [f"USGS last 24 h, M{mag}+ — showing {min(limit, len(feats))} "
             f"of {len(feats)}:"]
    for f in feats[:limit]:
        pr = f.get("properties") or {}
        coord = (f.get("geometry") or {}).get("coordinates") or [0, 0, 0]
        when = _dt.datetime.utcfromtimestamp(
            int(pr.get("time") or 0) / 1000).strftime("%m-%d %H:%M UTC")
        place = str(pr.get("place") or "unknown location")
        mm = pr.get("mag")
        lines.append(f"  M{mm} {place} — {when} "
                     f"(lat {float(coord[1]):+.3f}, lon {float(coord[0]):+.3f})"
                     f" {str(pr.get('url') or '')}")
    return "\n".join(lines)


def _suntimes(parameters: dict) -> str:
    p = parameters or {}
    lat, lon = _coord(p, "lat"), _coord(p, "lon")
    if lat is None or lon is None:
        return "suntimes needs lat=… and lon=… (decimal degrees)."
    date = str(p.get("date") or "").strip()
    if date:
        try:
            _dt.date.fromisoformat(date)
        except ValueError:
            return f"date must be YYYY-MM-DD, got {date!r}"
    else:
        date = _dt.date.today().isoformat()
    qs = urllib.parse.urlencode({"lat": lat, "lng": lon, "date": date,
                                 "formatted": "0"})
    data = _json(f"https://api.sunrise-sunset.org/json?{qs}")
    if str(data.get("status") or "").upper() != "OK":
        return f"Sun service said {data.get('status')!r} for {date}."
    r = data.get("results") or {}

    def hhmm(key: str) -> str:
        v = str(r.get(key) or "?")
        try:
            return _dt.datetime.fromisoformat(v).strftime("%H:%M")
        except ValueError:
            return v[:5] if len(v) >= 5 else v
    return (f"Sun for {date} @ {lat:.4f},{lon:.4f} (UTC): "
            f"sunrise {hhmm('sunrise')}, sunset {hhmm('sunset')}, "
            f"day length {r.get('day_length', '?')}s")


def sky(parameters: dict = None, player=None, session_memory=None) -> str:
    p = parameters or {}
    action = str(p.get("action") or "iss").strip().lower() or "iss"
    handlers = {"iss": _iss, "quake": _quakes, "quakes": _quakes,
                "sun": _suntimes, "suntimes": _suntimes}
    fn = handlers.get(action)
    if fn is None:
        return f"Unknown sky action {action!r} — iss | quakes | suntimes"
    try:
        return fn(p)
    except Exception as e:                       # noqa: BLE001 — honest report
        return f"sky/{action} failed ({type(e).__name__}: {e}) — free public endpoint unreachable or answered badly."


TOOL = {
    "name": "sky",
    "description": (
        "Free keyless facts: current ISS position (iss), last-24h "
        "earthquakes from USGS (quakes, min_magnitude=1.0|2.5|4.5|"
        "significant, limit=), sunrise/sunset for a location (suntimes, "
        "lat+lon+date=YYYY-MM-DD). Use for 'where is the ISS', 'any "
        "earthquakes', 'when is sunrise'.")
    ,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "iss | quakes | suntimes."},
            "lat": {"type": "NUMBER",
                    "description": "Latitude (suntimes)."},
            "lon": {"type": "NUMBER",
                    "description": "Longitude (suntimes)."},
            "date": {"type": "STRING",
                     "description": "YYYY-MM-DD for suntimes (default today)."},
            "min_magnitude": {"type": "STRING",
                              "description": "USGS bucket: 1.0 | 2.5 | 4.5 | significant."},
            "limit": {"type": "INTEGER",
                      "description": "How many quakes to show (1-25, default 5)."},
        },
        "required": [],
    },
    "handler": sky,
}
