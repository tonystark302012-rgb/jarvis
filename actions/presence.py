"""presence — is the user actually at the machine?

Shares the module-level tracker with main.py (every input path calls
note_activity; the proactive engine checks is_present) — so this action
is a pure read/write view over the same state machine in
core/presence.py. No camera, no new dependency; best-effort OS idle
probe where the platform provides one for free.
"""

from core.presence import tracker


def presence(parameters: dict, ctx: dict | None = None) -> str:
    # NOTE: the dispatcher invokes handlers as fn(parameters=..., **ctx) —
    # the kwarg MUST be named `parameters` (see core/action_loader._call_handler).
    parameters = parameters or {}
    action = str(parameters.get("action") or "status").lower()
    tr = tracker()

    if action == "status":
        st = tr.status()
        idle = st.get("idle_seconds")
        os_idle = st.get("os_idle_seconds")
        lines = [
            f"User: {st.get('state', '?').upper()} "
            f"(since {st.get('since', '?')})",
            f"JARVIS-input idle: {idle}s of {st.get('away_after_seconds')}s "
            "before AWAY",
        ]
        if os_idle is None:
            lines.append(
                "OS idle probe: unavailable on this platform — presence "
                "tracks JARVIS interactions only."
            )
        else:
            lines.append(f"OS keyboard/mouse idle: {os_idle}s")
        hist = st.get("history") or []
        if hist:
            last = hist[-1]
            lines.append(f"Last transition: {last.get('state')} "
                         f"({last.get('why')}) at {last.get('ts')}")
        return "\n".join(lines)

    if action == "history":
        hist = list(tr.history)
        if not hist:
            return "No presence transitions yet this session."
        out = [f"Last {len(hist)} presence transitions:"]
        for h in hist[-10:]:
            out.append(f"  {h['ts']}  {h['state'].upper()}  ({h['why']})")
        return "\n".join(out)

    if action in {"set_away_after", "set"}:
        secs = parameters.get("seconds") or parameters.get("value")
        if secs is None:
            return "Need a number of seconds, e.g. seconds=300."
        try:
            new = tr.set_away_after(float(secs))
        except (TypeError, ValueError):
            return "Need a number of seconds, e.g. seconds=300."
        return f"Away threshold set to {new:.0f}s of no interaction."

    if action in {"mark", "here"}:
        return "Marked present." if tr.force("present") else "Already present."

    if action in {"mark_away", "away"}:
        return "Marked AWAY." if tr.force("away") else "Already away."

    return ("Unknown action — use: status (default) | history | "
            "set_away_after seconds=N | mark | away")


TOOL = {
    "name": "presence",
    "description": (
        "Presence detection — is the user at the machine? Actions: status "
        "(default: present/away, idle seconds, OS idle probe), history "
        "(recent transitions), set_away_after seconds=N (idle threshold, "
        "default 300), mark (force present), away (force away). JARVIS "
        "already tracks this automatically from every input; use this to "
        "inspect or tune it. Presence gates proactive speech so JARVIS "
        "doesn't talk to an empty room."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | history | set_away_after | mark | away",
            },
            "seconds": {"type": "NUMBER",
                        "description": "Idle seconds before AWAY (set_away_after)"},
        },
        "required": [],
    },
    "handler": presence,
}
