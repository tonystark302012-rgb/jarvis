"""
spoken_brief — the morning brief you HEAR (Report K, "audio banao").

Composes a short spoken briefing from sources JARVIS already has —
weather_report, dots tasks/agenda, feed headlines — and hands it to the
existing `speak` context (main.speak → Kokoro/EdgeTTS, the same voice the
HUD uses). One rule can fire it every morning; the action itself decides
when it was last composed (daily cache, `action=refresh` forces a new
one).

Sections are seams (`_weather_section` etc.) so tests — and future
sources (calendar, monitor) — plug in without touching the composer.

WHY NOT A DUPLICATE: `_send_startup_briefing` is a Gemini conversation
that starts when the app launches. This is a deterministic, rule-able
TOOL: no model call, no network except the sources themselves, spoken on
demand or from a schedule rule.
"""
from __future__ import annotations

import time
from pathlib import Path

_MAX_SECTION = 320


def _trim(text: str, n: int = _MAX_SECTION) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _greeting() -> str:
    hour = time.localtime().tm_hour
    if 5 <= hour < 12:
        return "Good morning."
    if 12 <= hour < 17:
        return "Good afternoon."
    if 17 <= hour < 21:
        return "Good evening."
    return "Hello."


def _weather_section(city: str = "") -> str:
    try:
        from actions import weather_report
        params = {"city": city} if city else {}
        out = weather_report.weather_report(params)
        return _trim(out, 300) if isinstance(out, str) and out else ""
    except Exception:
        return ""


def _tasks_section() -> str:
    try:
        from actions import dots
        out = dots.dots({"action": "task_list"})
        if not isinstance(out, str) or not out.strip():
            return ""
        # keep only the actionable head
        lines = [ln for ln in out.splitlines() if ln.strip()][:6]
        return _trim(" | ".join(lines), 300)
    except Exception:
        return ""


def _headlines_section(limit: int = 4) -> str:
    try:
        from actions import feed
        out = feed.feed({"action": "check", "limit": str(limit)})
        if not isinstance(out, str) or not out.strip():
            return ""
        lines = [ln for ln in out.splitlines()
                 if ln.strip() and not ln.startswith("Feed")][:limit + 1]
        return _trim(" — ".join(l.strip("- ").strip() for l in lines), 340)
    except Exception:
        return ""


def compose(city: str = "") -> str:
    parts: list[str] = [_greeting()]
    missing: list[str] = []
    for label, fn in (("weather", lambda: _weather_section(city)),
                      ("agenda", _tasks_section),
                      ("headlines", _headlines_section)):
        try:
            text = fn()
        except Exception:
            text = ""
        if text:
            parts.append(f"{label.capitalize()}: {text}")
        else:
            missing.append(label)
    if missing and len(missing) == 3:
        return ("Brief is empty — weather, agenda and feeds are all "
                "unavailable right now (offline or not configured).")
    if missing:
        parts.append(f"(no {', '.join(missing)} available)")
    return "\n".join(parts)


def _cache_path() -> Path:
    from config import get_base_dir
    d = get_base_dir() / "memory" / "briefs"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"brief-{time.strftime('%Y-%m-%d')}.txt"


def spoken_brief(parameters: dict = None, player=None, speak=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "give")).lower().strip()
    city = str(params.get("city", "") or "").strip()
    cache = _cache_path()

    if action == "status":
        if cache.is_file():
            age_h = (time.time() - cache.stat().st_mtime) / 3600
            return f"Today's brief cached ({age_h:.1f}h old): {cache.name}"
        return "No brief cached for today yet."

    if action == "refresh":
        text = compose(city)
        try:
            cache.write_text(text, encoding="utf-8")
        except Exception:
            pass
    elif cache.is_file():
        try:
            text = cache.read_text(encoding="utf-8")
        except Exception:
            text = compose(city)
    else:
        text = compose(city)
        try:
            cache.write_text(text, encoding="utf-8")
        except Exception:
            pass

    # spoken through the HUD's existing TTS path (Kokoro/Edge via main.speak)
    if callable(speak):
        try:
            speak(text)
        except Exception:
            pass
    if player is not None:
        try:
            player.show_content("SPOKEN BRIEF", text[:4000])
        except Exception:
            pass
    return text


TOOL = {
    "name": "spoken_brief",
    "description": (
        "Spoken morning/daily brief: composes greeting + weather + "
        "agenda/tasks + feed headlines from existing sources (no model "
        "call) and SPOKES it through the HUD voice path (Kokoro/EdgeTTS). "
        "Daily-cached; actions: give (default — cached if today's exists), "
        "refresh (recompose now), status. Schedule it with a rule ('morning "
        "at 8 do spoken_brief'). Use for 'briefing sunao', 'subah ka "
        "brief', 'speak my brief'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "give | refresh | status."},
            "city": {"type": "STRING",
                     "description": "Weather city override."},
        },
        "required": [],
    },
    "handler": spoken_brief,
}
