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


def mission_control(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "timeline")).lower().strip()

    if action == "clear":
        activity.clear()
        result = "Activity timeline cleared."
    elif action == "dag":
        n = 12
        try:
            n = max(1, min(40, int(params.get("limit", 16))))
        except (TypeError, ValueError):
            pass
        try:
            result = _dag_svg(n, player)
        except Exception as e:
            result = f"DAG render failed: {e}"
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
                "stats" if action == "stats" else
                "dag" if action == "dag" else "timeline")
            player.show_content(title, result)
        except Exception:
            pass
    return result


def _esc(x: str) -> str:
    return (str(x).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _dag_svg(n: int, player=None) -> str:
    """Step-graph of the last n tool calls — who ran after whom, how
    long, ok/failed (Report L). Saved like every other diagram, returned
    as a path + a readable fallback list."""
    import re as _re
    import time as _time
    from config import get_base_dir

    events = [e for e in activity.recent(n)
              if e.kind in ("tool", "task", "mirror")]
    if not events:
        return "No tool events yet — nothing to graph."
    per_row, box_w, box_h, gap_x, gap_y = 5, 150, 54, 34, 46
    rows = (len(events) + per_row - 1) // per_row
    w = per_row * (box_w + gap_x) + 40
    h = rows * (box_h + gap_y) + 60
    body = ['<defs><marker id="a" markerWidth="8" markerHeight="8" '
            'refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" '
            'fill="#475569"/></marker></defs>']
    prev_xy = None
    for i, ev in enumerate(events):
        r, c = divmod(i, per_row)
        # serpentine so the eye follows start → end without jumping rows
        cc = c if r % 2 == 0 else per_row - 1 - c
        x = 20 + cc * (box_w + gap_x)
        y = 40 + r * (box_h + gap_y)
        fill = "#0f1a12" if ev.ok else ("#1a0f0f" if ev.ok is False
                                        else "#0f1420")
        stroke = "#22c55e" if ev.ok else ("#ef4444" if ev.ok is False
                                          else "#38bdf8")
        dur = "…" if ev.running else f"{max(ev.duration, 0):.1f}s"
        mark = "…" if ev.ok is None else ("✓" if ev.ok else "✗")
        name = _esc(ev.name[:18])
        body.append(
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" '
            f'rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
            f'<text x="{x + 8}" y="{y + 20}" fill="#e2e8f0" '
            f'font-size="12" font-weight="700" font-family="monospace">'
            f'{mark} {name}</text>'
            f'<text x="{x + 8}" y="{y + 38}" fill="#94a3b8" '
            f'font-size="10" font-family="monospace">'
            f'{_esc(dur)} · {_esc(ev.when()[3:])}</text>')
        if prev_xy is not None:
            px, py = prev_xy
            if (r, c) == (r, 0) and c == 0 and i % per_row == 0:
                # serpentine turn: elbow down instead of a long back arrow
                body.append(f'<path d="M {px} {py + box_h / 2} L {px} '
                            f'{y + box_h / 2}" stroke="#475569" '
                            f'fill="none" marker-end="url(#a)"/>')
            else:
                body.append(f'<path d="M {px} {py} L {x} {y + box_h / 2}" '
                            f'stroke="#475569" fill="none" '
                            f'marker-end="url(#a)"/>')
        prev_xy = (x + box_w, y + box_h / 2)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" '
           f'height="{h}" viewBox="0 0 {w} {h}" '
           f'font-family="monospace">'
           f'<rect width="{w}" height="{h}" fill="#0b1220"/>'
           f'<text x="16" y="24" fill="#e2e8f0" font-size="13" '
           f'font-weight="600">MISSION DAG — last {len(events)} step(s)'
           f'</text>' + "\n".join(body) + '</svg>')
    d = get_base_dir() / "diagrams"
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"mission-dag-{_re.sub(r'[^0-9]+', '', str(_time.time()))}.svg"
    out.write_text(svg, encoding="utf-8")
    chain = " → ".join(f"{e.name}({'…' if e.running else format(max(e.duration, 0), '.1f') + 's'})"
                       for e in events[:12])
    text = f"Mission DAG saved: {out}\nChain: {chain}"
    if player is not None:
        try:
            player.show_content("MISSION DAG", text[:4000])
        except Exception:
            pass
    return text


TOOL = {
    "name": "mission_control",
    "description": (
        "Shows the live Mission Control timeline: every tool call in this "
        "session with time, status (running/ok/failed) and duration; "
        "action=dag also renders the last steps as an SVG step-graph "
        "(who ran after whom, how long). Use for 'what are you doing', "
        "'what did you just do', 'show activity', 'show the mission graph', "
        "'status of the session'. Actions: timeline (default), stats, clear."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "timeline | stats | dag | clear. Default: timeline.",
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
