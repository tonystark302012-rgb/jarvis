"""
Mission Control — the live activity timeline.

Answers "what are you doing / what did you just do / what happened in this
session" with a rendered timeline built from core.activity. Pushes the result
into the content panel when a player (UI) is in context, and always returns
the text so the model can read it aloud.

Read-only by design: viewing activity must never be able to change anything.
"""
from __future__ import annotations

from core import activity


def mission_control(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "timeline")).lower().strip()

    if action == "clear":
        activity.clear()
        result = "Activity timeline cleared."
    elif action == "stats":
        s = activity.stats()
        result = ("Session activity: {tool_calls} tool calls "
                  "({ok} ok, {failed} failed), {running} running, "
                  "{total} events total.").format(**s)
    else:
        try:
            n = max(1, min(60, int(params.get("limit", 20))))
        except (TypeError, ValueError):
            n = 20
        result = activity.timeline(n)

    if player is not None and action != "clear":
        try:
            title = "MISSION CONTROL — " + (
                "stats" if action == "stats" else "timeline")
            player.show_content(title, result)
        except Exception:
            pass
    return result


TOOL = {
    "name": "mission_control",
    "description": (
        "Shows the live Mission Control timeline: every tool call in this "
        "session with time, status (running/ok/failed) and duration. Use for "
        "'what are you doing', 'what did you just do', 'show activity', "
        "'status of the session'. Actions: timeline (default), stats, clear."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "timeline | stats | clear. Default: timeline.",
            },
            "limit": {
                "type": "STRING",
                "description": "How many recent events for timeline (1-60). Default 20.",
            },
        },
        "required": [],
    },
    "handler": mission_control,
}
