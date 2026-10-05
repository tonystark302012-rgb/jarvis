# actions/world_view.py
"""world_view — satellite imagery + street maps of any place on Earth.

WHAT IT DOES
    Pulls free public tiles and stitches them into one PNG the HUD can
    show:

      satellite  NASA EOSDIS GIBS — VIIRS true colour (free, no key,
                 no account; newest granules can lag a few hours, so
                 the fetch walks back up to 5 days for a usable date)
      map        OpenStreetMap standard tiles (free, © OpenStreetMem-
                 ory/OSM contributors; one small burst of tiles, the
                 tile usage policy is respected: ≤16 tiles per request,
                 identifying User-Agent, no bulk download)

    world_view mode=satellite lat=26.9124 lon=75.7873 zoom=11
    world_view mode=map lat=26.9124 lon=75.7873 zoom=13 size=768

FREE: both sources are free-to-use public services (NASA public domain
data, OSM ODbL). Client is stdlib urllib + Pillow (already a core dep);
zero API keys. Under privacy mode the coordinates never leave the
machine (world_view ∈ CLOUD_TOOLS — the gate refuses first).
"""
from __future__ import annotations

import datetime as _dt
import io
import time
import urllib.error
import urllib.request
from pathlib import Path

from core import privacy as _privacy

_UA = "jarvis/1.0 (desktop assistant world view; single-user bursts)"
_MAX_GRID = 4            # 4×4 tiles = 1024 px cap
_SAT_MAX_ZOOM = 8        # GIBS GoogleMapsCompatible_Level8 ceiling
_SAT_DATES_BACK = 5      # newest granule may still be processing


# ── tile math (Web Mercator, XYZ scheme) ────────────────────────────────────

def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Fractional (x, y) tile coordinates for a point at `zoom`."""
    import math
    z = max(0, int(zoom))
    n = 1 << z
    lat = max(-85.05112878, min(85.05112878, float(lat)))
    x = (float(lon) + 180.0) / 360.0 * n
    lat_r = math.radians(lat)
    y = (1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n
    return x, y


def tile_to_latlon(x: float, y: float, zoom: int) -> tuple[float, float]:
    """Inverse of latlon_to_tile (center of the fractional tile)."""
    import math
    z = max(0, int(zoom))
    n = 1 << z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def _tile_grid(lat: float, lon: float, zoom: int,
               size_px: int) -> tuple[int, int, int]:
    """Top-left tile (left, top) and grid width n for a size_px canvas
    centred on (lat, lon), clamped inside the world."""
    n = max(1, min(_MAX_GRID, int(size_px) // 256))
    xt, yt = latlon_to_tile(lat, lon, zoom)
    limit = 1 << max(0, int(zoom))
    left = int(xt) - n // 2
    top = int(yt) - n // 2
    left = max(0, min(left, limit - n))
    top = max(0, min(top, limit - n))
    return left, top, n


# ── URLs (free sources) ─────────────────────────────────────────────────────

def _map_tile_url(z: int, x: int, y: int) -> str:
    return f"https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def _sat_tile_url(date: str, z: int, x: int, y: int) -> str:
    return ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
            "VIIRS_SNPP_CorrectedReflectance_TrueColor/"
            f"default/{date}/GoogleMapsCompatible_Level8/"
            f"{z}/{y}/{x}.jpeg")


def _sat_dates(now: float | None = None) -> list[str]:
    """Candidate granule dates, newest first (GIBS lags up to ~1 day)."""
    base = (_dt.datetime.utcfromtimestamp(
        now if now is not None else time.time()).date())
    return [(base - _dt.timedelta(days=i)).isoformat()
            for i in range(_SAT_DATES_BACK)]


# ── fetch + compose (test seams) ────────────────────────────────────────────

def _fetch(url: str, timeout: float = 15.0) -> bytes | None:
    """One tile → bytes, or None (missing/error — callers degrade honestly)."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _compose(pieces: dict[tuple[int, int], bytes], left: int, top: int,
             n: int) -> bytes:
    """Grid of tile bytes → PNG bytes. Missing tiles stay black (partial
    views are honest, not fatal). Raises ImportError if Pillow absent."""
    from PIL import Image
    canvas = Image.new("RGB", (n * 256, n * 256), (0, 0, 0))
    for (tx, ty), data in pieces.items():
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            canvas.paste(img, ((tx - left) * 256, (ty - top) * 256))
        except Exception:
            continue
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return out.getvalue()


def _out_path(mode: str) -> Path:
    from config import get_base_dir
    d = get_base_dir() / "world_view"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"world-view-{mode}-{int(time.time())}.png"


# ── handler ─────────────────────────────────────────────────────────────────

def _num(params: dict, key: str, default=None):
    raw = params.get(key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def world_view(parameters: dict = None, player=None,
               session_memory=None) -> str:
    # 1. privacy FIRST — coordinates are personal; refuse before parsing
    blocked = _privacy.gate("world_view")
    if blocked:
        return blocked

    params = parameters or {}
    mode = str(params.get("mode") or "satellite").lower().strip()
    if mode not in ("satellite", "map"):
        return "mode must be satellite | map."

    lat = _num(params, "lat")
    lon = _num(params, "lon")
    if lat is None or lon is None:
        return ("Give lat and lon — e.g. "
                "world_view mode=satellite lat=26.91 lon=75.79 zoom=11.")
    if not (-85.05 <= lat <= 85.05):
        return "lat must be between -85.05 and 85.05 (Web Mercator limit)."
    if not (-180.0 <= lon <= 180.0):
        return "lon must be between -180 and 180."

    zoom = _num(params, "zoom", 11 if mode == "map" else 6)
    zoom = 11 if zoom is None else int(zoom)
    z_cap = 19 if mode == "map" else _SAT_MAX_ZOOM
    zoom = max(0, min(zoom, z_cap))

    size = _num(params, "size", 512)
    size = 512 if size is None else int(size)
    size = max(256, min(size, _MAX_GRID * 256))

    left, top, n = _tile_grid(lat, lon, zoom, size)

    # ── fetch the grid (satellite walks dates until granules exist) ──
    dates = _sat_dates() if mode == "satellite" else [""]
    best_date = ""
    if mode == "satellite":
        probe = _sat_tile_url(dates[0], zoom, left, top)
        for d in dates:
            if _fetch(_sat_tile_url(d, zoom, left, top)) is not None:
                best_date = d
                break
        if not best_date:
            return ("No satellite tiles answered (GIBS down or no granule "
                    f"for the last {len(dates)} days) — try mode=map, or "
                    "retry later.")
        ordered = [best_date] + [d for d in dates if d != best_date]
        del probe
    else:
        ordered = [""]

    pieces: dict[tuple[int, int], bytes] = {}
    for ty in range(top, top + n):
        for tx in range(left, left + n):
            data = None
            for d in ordered:
                url = (_map_tile_url(zoom, tx, ty) if mode == "map"
                       else _sat_tile_url(d, zoom, tx, ty))
                data = _fetch(url)
                if data:
                    pieces[(tx, ty)] = data
                    break
    if not pieces:
        return ("Couldn't fetch a single tile — the free public tile "
                "servers didn't answer. Check the network and retry.")

    try:
        png = _compose(pieces, left, top, n)
    except ImportError:
        return ("Pillow is missing — image stitching needs it: "
                "pip install pillow (already in requirements.txt).")
    except Exception as e:
        return f"Stitching failed: {e}"

    path = _out_path(mode)
    path.write_bytes(png)

    cx, cy = latlon_to_tile(lat, lon, zoom)
    centre_lat, centre_lon = tile_to_latlon(cx, cy, zoom)
    src = ("NASA EOSDIS GIBS — VIIRS true colour"
           if mode == "satellite" else "© OpenStreetMap contributors")
    when = f", granule {best_date}" if best_date else ""
    missing = n * n - len(pieces)
    partial = f" ({missing} tile(s) missing)" if missing else ""
    summary = (f"WORLD VIEW — {mode} @ {lat:.4f}, {lon:.4f} z{zoom}"
               f"{when}{partial}\nSaved: {path}\nSource: {src}\n"
               f"Centre ≈ {centre_lat:.4f}, {centre_lon:.4f}")
    if player is not None:
        try:
            player.show_content(f"WORLD VIEW — {mode.upper()}"[:48], summary)
        except Exception:
            pass
    return summary


TOOL = {
    "name": "world_view",
    "description": (
        "Satellite imagery or a street map of any coordinates, stitched "
        "from FREE public tiles (NASA GIBS true colour / OpenStreetMap) "
        "into a PNG on the HUD. Use for 'show me satellite view of …', "
        "'map this area', 'what does this place look like from space'. "
        "No API key, no account. Requires lat+lon (ask if only a place "
        "name is given)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "satellite (default) | map"},
            "lat": {"type": "NUMBER", "description": "Latitude (-85..85)"},
            "lon": {"type": "NUMBER", "description": "Longitude (-180..180)"},
            "zoom": {"type": "NUMBER",
                     "description": "Map 0-19, satellite 0-8. Default 11/6."},
            "size": {"type": "NUMBER",
                     "description": "Output edge px: 256 | 512 | 768 | 1024 "
                                    "(tile grid 1-4). Default 512."},
        },
        "required": ["lat", "lon"],
    },
    "handler": world_view,
}
