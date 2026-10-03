"""
weather_report — live weather, no API key.

WHAT CHANGED
------------
This tool used to be a stub wearing a real tool's name. It did exactly one
thing:

    url = f"https://www.google.com/search?q=weather+in+{city}"
    webbrowser.open(url)
    return "Showing the weather for {city}"

No temperature, no forecast, no data of any kind - it opened a browser tab and
told the model it had shown something. So "what's the weather in Jaipur" sent
the assistant off to ask a search engine, and the answer came back as prose
scraped from whatever the browser happened to load.

It now calls Open-Meteo, which is free, needs no key, and has no quota worth
speaking of. Two requests: geocode the city name, then read the forecast.

Everything is best-effort: any network or parsing failure returns a plain
sentence the assistant can say out loud, never a traceback.
"""

from __future__ import annotations

from datetime import datetime


try:
    import requests
    _REQUESTS = True
except ImportError:                                    # pragma: no cover
    _REQUESTS = False

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT = 8          # seconds. A weather answer that arrives late is useless.

# WMO weather codes -> plain English. Open-Meteo returns the integer only.
_WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "light freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "light freezing rain", 67: "heavy freezing rain",
    71: "slight snowfall", 73: "moderate snowfall", 75: "heavy snowfall",
    77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}


def _describe(code) -> str:
    try:
        return _WMO.get(int(code), "")
    except (TypeError, ValueError):
        return ""


def _geocode(city: str) -> dict | None:
    r = requests.get(_GEOCODE_URL,
                     params={"name": city, "count": 1, "language": "en", "format": "json"},
                     timeout=_TIMEOUT)
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    return results[0] if results else None


def _forecast(lat: float, lon: float, days: int = 3,
              hourly: bool = False) -> dict | None:
    params = {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                   "weather_code,wind_speed_10m,precipitation",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": "auto",
        "forecast_days": days,
    }
    if hourly:
        params["hourly"] = "temperature_2m,precipitation_probability,weather_code"
        params["forecast_hours"] = 12
    r = requests.get(_FORECAST_URL, params=params, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def weather_action(parameters: dict = None, player=None, session_memory=None) -> str:
    """Live weather for a city. Free, no API key, no browser tab."""
    params = parameters or {}
    city = (params.get("city") or "").strip()
    when = (params.get("time") or "today").strip()

    if not city:
        msg = "Sir, the city is missing for the weather report."
        _log(msg, player)
        return msg

    if not _REQUESTS:
        msg = "Sir, weather needs the 'requests' package. Run: pip install requests"
        _log(msg, player)
        return msg

    _log(f"fetching live weather for {city} ({when})", player)

    try:
        place = _geocode(city)
        if not place:
            msg = f"Sir, I couldn't find a place called '{city}'."
            _log(msg, player)
            return msg

        lat = place.get("latitude")
        lon = place.get("longitude")
        name = place.get("name", city)
        admin = place.get("admin1") or place.get("country") or ""
        label = f"{name}, {admin}" if admin and admin != name else name

        # 'hourly' / 'next hours' asks → pull the 12-hour strip as well.
        want_hourly = ("hour" in when or "tonight" in when
                       or str(params.get("hourly", "")).lower() in ("1", "true", "yes"))
        data = _forecast(lat, lon, hourly=want_hourly)
        if not data:
            msg = f"Sir, the weather service returned nothing for {label}."
            _log(msg, player)
            return msg

        cur = data.get("current") or {}
        daily = data.get("daily") or {}

        temp = cur.get("temperature_2m")
        feels = cur.get("apparent_temperature")
        humid = cur.get("relative_humidity_2m")
        wind = cur.get("wind_speed_10m")
        cond = _describe(cur.get("weather_code")) or "conditions unclear"

        # A missing field must not print "None" - drop it and keep the sentence.
        if temp is None:
            parts = [f"In {label}, {cond}"]
        else:
            parts = [f"In {label} it is {temp}\u00b0C, {cond}"]
        if None not in (feels, temp) and abs(feels - temp) >= 2:
            parts.append(f"feels like {feels}\u00b0C")
        if humid is not None:
            parts.append(f"humidity {humid}%")
        if wind is not None:
            parts.append(f"wind {wind} km/h")

        # a short outlook is what people actually want after "what's the weather"
        dates = daily.get("time") or []
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        pops = daily.get("precipitation_probability_max") or []
        if len(dates) > 1 and highs and lows:
            outlook = []
            for idx in range(1, min(3, len(dates))):
                try:
                    outlook.append(f"{dates[idx]}: {lows[idx]}\u2013{highs[idx]}\u00b0C")
                except (IndexError, TypeError):
                    break
            if outlook:
                parts.append("next up " + "; ".join(outlook))
            if pops and pops[0] is not None:
                parts.append(f"{pops[0]}% chance of rain today")

        # 12-hour strip when the user asked for it — the renderer the content
        # panel shows underneath the spoken line.
        if want_hourly:
            hourly = data.get("hourly") or {}
            h_times = hourly.get("time") or []
            h_temps = hourly.get("temperature_2m") or []
            h_pops  = hourly.get("precipitation_probability") or []
            if h_times:
                now_h = datetime.now().strftime("%Y-%m-%dT%H:00")
                start = 0
                for i, t in enumerate(h_times):
                    if t >= now_h:
                        start = i
                        break
                strip = []
                for i in range(start, min(start + 12, len(h_times))):
                    hh = h_times[i][11:16]
                    try:
                        strip.append(f"{hh} {h_temps[i]}°C"
                                     + (f" ({h_pops[i]}% rain)"
                                        if i < len(h_pops) and h_pops[i] else ""))
                    except IndexError:
                        break
                if strip:
                    parts.append("hourly: " + " · ".join(strip[:8]))

        msg = ", ".join(parts) + "."
        _log(msg, player)

        if session_memory:
            try:
                session_memory.set_last_search(query=f"weather in {city} {when}",
                                               response=msg)
            except Exception:
                pass
        return msg

    except Exception as e:
        # Network down, service down, unexpected shape - say so plainly.
        msg = f"Sir, I couldn't reach the weather service: {e}"
        _log(msg, player)
        return msg


# ── Tool declaration (auto-discovered by core/action_loader.py) ───────────────
TOOL = {
    "name": "weather_report",
    "description": (
        "Live weather for a city: current temperature, conditions, humidity, "
        "wind and a two-day outlook. Free, no API key. Use for ANY weather "
        "question - current conditions, forecasts, 'is it going to rain'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City name, e.g. 'Jaipur', 'London', 'New York'",
            },
            "time": {
                "type": "STRING",
                "description": "When: 'today', 'tomorrow', or a date. Default: today.",
            },
        },
        "required": ["city"],
    },
    "handler": weather_action,
}
