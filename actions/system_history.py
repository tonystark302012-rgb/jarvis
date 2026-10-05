"""
system_history — charts + anomaly report over metrics_store samples (L).

The monitor loop appends a sample every ~10 s (core.metrics_store); this
action turns that history into answers:

    report  — avg/min/max for cpu/ram/temp/gpu over hours=1|24
    chart   — bucket-averaged LINE chart of one metric (reuses the
              existing `chart` SVG renderer — no second drawing stack)
    anomaly — same-hour-yesterday ratio ("3x zyada"), honest '' when
              there isn't enough history to claim a spike

Read-only. Use from a rule ('every day compare cpu') or a question
("aaj system load kaisa raha").
"""
from __future__ import annotations

_METRIC_ALIASES = {"cpu": "cpu", "ram": "ram", "memory": "ram",
                   "temp": "temp", "temperature": "temp", "gpu": "gpu"}


def _metric(params: dict) -> str:
    raw = str(params.get("metric", "cpu") or "cpu").lower().strip()
    return _METRIC_ALIASES.get(raw, "cpu")


def _fmt_row(label: str, st: dict | None, unit: str = "%") -> str:
    if not st:
        return f"{label}: no samples in window"
    return (f"{label}: avg {st['avg']}{unit}  "
            f"(min {st['min']}, max {st['max']}, n={st['n']})")


def system_history(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    from core import metrics_store as ms
    params = parameters or {}
    action = str(params.get("action", "report")).lower().strip()
    try:
        hours = float(params.get("hours", 1))
    except (TypeError, ValueError):
        hours = 1.0
    hours = max(0.1, min(24, hours))
    metric = _metric(params)

    if action == "anomaly":
        out = ms.anomaly(metric) or ""
        if not out:
            n = ms.count(24)
            out = (f"No {metric} anomaly vs same hour yesterday"
                   + (" (enough history)." if n >= 50 else
                      f" — only {n} sample(s) in 24h, need more data."))
        elif player is not None:
            try:
                player.show_content("ANOMALY", out[:2000])
            except Exception:
                pass
        return out

    if action == "chart":
        pairs = ms.series(metric, hours=hours,
                          buckets=max(12, int(hours * 30)))
        if len(pairs) < 2:
            return (f"Not enough {metric} samples in the last "
                    f"{hours}h for a chart (the monitor loop collects "
                    "one every ~10s while JARVIS runs).")
        data = ", ".join(f"{label}={val}" for label, val in pairs)
        try:
            from actions import charts
            return charts.chart({"kind": "line", "data": data,
                                 "title": f"{metric.upper()} — last "
                                          f"{hours:g}h"})
        except Exception as e:
            return f"chart failed: {e}"

    # report (default)
    n1 = ms.count(1)
    if n1 == 0 and ms.count(24) == 0:
        return ("No metric history yet — samples are collected every ~10s "
                "while the system-monitor loop runs; try again later.")
    rows = [
        f"Metric history ({hours:g}h window):",
        _fmt_row("cpu", ms.stats("cpu", hours)),
        _fmt_row("ram", ms.stats("ram", hours)),
        _fmt_row("temp", ms.stats("temp", hours), "°C"),
        _fmt_row("gpu", ms.stats("gpu", hours)),
    ]
    anomalies = [a for a in (ms.anomaly("cpu"), ms.anomaly("ram")) if a]
    if anomalies:
        rows.append("Anomalies: " + " | ".join(anomalies))
    rows.append(f"(samples last 24h: {ms.count(24)}; chart: "
                "system_history action=chart metric=cpu)")
    out = "\n".join(rows)
    if player is not None:
        try:
            player.show_content("SYSTEM HISTORY", out[:4000])
        except Exception:
            pass
    return out


TOOL = {
    "name": "system_history",
    "description": (
        "System metric time-series (samples every ~10s while JARVIS "
        "runs). Actions: report (default — avg/min/max cpu/ram/temp/gpu "
        "over `hours`, plus anomalies), chart (line SVG via the chart "
        "renderer, `metric`=cpu|ram|temp|gpu), anomaly (same-hour-"
        "yesterday ratio, honest 'no data' instead of invented spikes). "
        "Use for 'how was the load today', 'cpu trend chart', 'kal isi "
        "waqt se zyada hai kya'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "report | chart | anomaly."},
            "metric": {"type": "STRING",
                       "description": "cpu | ram | temp | gpu."},
            "hours": {"type": "STRING",
                      "description": "Window in hours (0.1–24)."},
        },
        "required": [],
    },
    "handler": system_history,
}
