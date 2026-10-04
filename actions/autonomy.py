"""autonomy — how much JARVIS may do without asking (observe/ask/auto).

Voice example: 'autonomy auto 60' → unattended agent runs for an hour,
then falls back to ask. 'autonomy observe' → read-only agent. The mode is
persisted with the same config file as privacy mode and read at the single
interface choke point (main._execute_tool), so nothing the model says on
any other tool can escalate it.
"""

from core import autonomy as _a


def autonomy(parameters: dict, ctx: dict | None = None) -> str:
    parameters = parameters or {}
    action = str(parameters.get("action") or "").lower().strip()
    mode = str(parameters.get("mode") or "").lower().strip()

    if not action and not mode:
        action = "status"
    if mode and not action:
        action = "set"
    if action in {"status", "show"}:
        st = _a.status()
        lines = [
            f"Autonomy: {st['mode'].upper()}",
            {
                "observe": "read-only — mutating tools are refused honestly.",
                "ask": "today's behaviour — existing confirm gates apply.",
                "auto": "unattended — undo-protected agent steps proceed "
                        "without asking; irreversible tools still gate.",
            }[st["mode"]],
        ]
        if st["expires"] and st["mode"] != "ask":
            import datetime as _dt
            exp = _dt.datetime.fromtimestamp(st["expires"])
            lines.append(f"(expires {exp:%Y-%m-%d %H:%M})")
        lines.append("Set: action=set mode=observe|ask|auto [minutes=N]")
        return "\n".join(lines)

    if action in {"set", "mode", "on"}:
        try:
            minutes = parameters.get("minutes")
            minutes = None if minutes in (None, "") else float(minutes)
            st = _a.set_mode(mode or str(parameters.get("value") or ""), minutes)
        except (ValueError, TypeError) as e:
            return (f"Bad mode — use observe | ask | auto "
                    f"(optional minutes=60). ({e})")
        tail = f" for {minutes:.0f} min" if minutes else " (no expiry)"
        return f"Autonomy → {st['mode'].upper()}{tail}."

    return ("Unknown action — use: status (default) | "
            "set mode=observe|ask|auto [minutes=N]")


TOOL = {
    "name": "autonomy",
    "description": (
        "Autonomy mode: how much the agent may do without asking. "
        "Actions: status (default), set (mode: observe = read-only agent, "
        "ask = current confirm-gated behaviour, auto = unattended runs for "
        "undo-protected work; optional minutes=N for a TTL, e.g. "
        "'autonomy auto 60'). Irreversible actions (shutdown, sending "
        "messages, force-push, vault writes) always stay gated regardless "
        "of mode."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status | set"},
            "mode": {"type": "STRING",
                     "description": "observe | ask | auto"},
            "minutes": {"type": "NUMBER",
                        "description": "Expire the mode after N minutes"},
        },
        "required": [],
    },
    "handler": autonomy,
}
