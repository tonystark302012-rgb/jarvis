"""
charts — pure-Python SVG charts on demand.

bar / line / pie from simple {label: value} input, saved to disk like
actions/diagram.py. No matplotlib: the drawing is a hundred lines of SVG and
renders anywhere — browser, dashboard, image viewer.
"""
from __future__ import annotations

import html
import re
import time
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _parse_data(raw) -> list[tuple[str, float]]:
    """Accept dict {label: value}, list of [label, value], or 'a=1, b=2'."""
    pairs: list[tuple[str, float]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = [(x[0], x[1]) if isinstance(x, (list, tuple)) and len(x) >= 2
                 else (str(x), 0) for x in raw]
    else:
        text = str(raw)
        items = []
        for part in re.split(r"[,;\n]", text):
            part = part.strip()
            if not part:
                continue
            if "=" in part:
                k, v = part.split("=", 1)
            elif ":" in part:
                k, v = part.split(":", 1)
            else:
                continue
            items.append((k.strip(), v.strip()))
    for k, v in items:
        try:
            pairs.append((str(k)[:28], float(v)))
        except (TypeError, ValueError):
            continue
    return pairs[:24]


def _save(svg: str, title: str) -> Path:
    d = _base_dir() / "charts"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]+", "-", title.lower()).strip("-") or "chart"
    p = d / f"{safe}-{int(time.time())}.svg"
    p.write_text(svg, encoding="utf-8")
    return p


_PALETTE = ["#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f472b6",
            "#60a5fa", "#fb7185", "#4ade80"]


def _bar_svg(pairs, title):
    w, h = 720, 400
    left, bottom, top = 70, 60, 60
    plot_w, plot_h = w - left - 30, h - bottom - top
    vmax = max((v for _, v in pairs), default=1) or 1
    n = len(pairs)
    bw = max(8, plot_w // max(1, n) - 12)
    parts = [f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" '
             f'stroke="#475569" stroke-width="1.5"/>',
             f'<line x1="{left}" y1="{top+plot_h}" x2="{left+plot_w}" '
             f'y2="{top+plot_h}" stroke="#475569" stroke-width="1.5"/>']
    for i, (label, val) in enumerate(pairs):
        bh = int(plot_h * (val / vmax))
        x = left + 14 + i * (plot_w // max(1, n))
        y = top + plot_h - bh
        color = _PALETTE[i % len(_PALETTE)]
        parts.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" '
                     f'fill="{color}" rx="3"/>')
        parts.append(f'<text x="{x + bw//2}" y="{y - 6}" fill="#e2e8f0" '
                     f'font-size="11" text-anchor="middle">{_esc(_fmt(val))}</text>')
        parts.append(f'<text x="{x + bw//2}" y="{top + plot_h + 18}" '
                     f'fill="#94a3b8" font-size="11" text-anchor="middle">'
                     f'{_esc(label[:14])}</text>')
    return _frame(w, h, title, "\n".join(parts))


def _line_svg(pairs, title):
    w, h = 720, 380
    left, bottom, top = 70, 56, 60
    plot_w, plot_h = w - left - 30, h - bottom - top
    vals = [v for _, v in pairs]
    vmax, vmin = max(vals), min(vals)
    if vmax == vmin:
        vmax = vmin + 1
    n = max(1, len(pairs) - 1)
    pts = []
    for i, (label, val) in enumerate(pairs):
        x = left + int(plot_w * i / n)
        y = top + plot_h - int(plot_h * (val - vmin) / (vmax - vmin))
        pts.append((x, y, label, val))
    poly = " ".join(f"{x},{y}" for x, y, _, _ in pts)
    parts = [f'<polyline points="{poly}" fill="none" stroke="{_PALETTE[0]}" '
             f'stroke-width="2.5"/>']
    for x, y, label, val in pts:
        parts.append(f'<circle cx="{x}" cy="{y}" r="4" fill="{_PALETTE[0]}"/>')
        parts.append(f'<text x="{x}" y="{y-10}" fill="#e2e8f0" font-size="10" '
                     f'text-anchor="middle">{_esc(_fmt(val))}</text>')
        parts.append(f'<text x="{x}" y="{top+plot_h+18}" fill="#94a3b8" '
                     f'font-size="10" text-anchor="middle">{_esc(label[:12])}</text>')
    return _frame(w, h, title, "\n".join(parts))


def _pie_svg(pairs, title):
    pairs = [(k, v) for k, v in pairs if v > 0] or pairs
    w, h = 640, 400
    cx, cy, r = 230, 220, 130
    total = sum(v for _, v in pairs) or 1
    import math
    angle = -math.pi / 2
    parts = []
    for i, (label, val) in enumerate(pairs):
        frac = val / total
        a2 = angle + frac * 2 * math.pi
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        x2, y2 = cx + r * math.cos(a2), cy + r * math.sin(a2)
        large = 1 if frac > 0.5 else 0
        color = _PALETTE[i % len(_PALETTE)]
        if frac >= 0.999:
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
        else:
            parts.append(
                f'<path d="M {cx} {cy} L {x1:.1f} {y1:.1f} '
                f'A {r} {r} 0 {large} 1 {x2:.1f} {y2:.1f} Z" '
                f'fill="{color}" stroke="#0b1220" stroke-width="2"/>')
        angle = a2
    ly = 100
    for i, (label, val) in enumerate(pairs):
        color = _PALETTE[i % len(_PALETTE)]
        pct = 100 * val / total
        parts.append(f'<rect x="410" y="{ly-12}" width="14" height="14" '
                     f'fill="{color}" rx="3"/>')
        parts.append(f'<text x="432" y="{ly}" fill="#e2e8f0" font-size="12">'
                     f'{_esc(label[:20])} — {_fmt(val)} ({pct:.0f}%)</text>')
        ly += 26
    return _frame(w, h, title, "\n".join(parts))


def _fmt(v: float) -> str:
    return f"{v:.0f}" if abs(v - round(v)) < 1e-9 else f"{v:.2f}"


def _frame(w, h, title, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Segoe UI, Arial, sans-serif">'
        f'<rect width="{w}" height="{h}" fill="#0b1220"/>'
        f'<text x="16" y="30" fill="#e2e8f0" font-size="15" '
        f'font-weight="600">{_esc(title)}</text>{body}</svg>'
    )


_KINDS = {"bar": _bar_svg, "line": _line_svg, "pie": _pie_svg}


def chart(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    kind = str(params.get("kind", "bar")).lower().strip()
    title = str(params.get("title", "")).strip() or "Chart"
    if kind not in _KINDS:
        return f"Unknown chart kind '{kind}'. Use: bar, line, pie."
    pairs = _parse_data(params.get("data", ""))
    if len(pairs) < 2:
        return ("Not enough numeric data. Send data as 'a=1, b=2, c=3' "
                "or a dict of label→value.")
    out = _save(_KINDS[kind](pairs, title), title)
    result = f"Chart saved: {out}"
    if player is not None:
        try:
            player.show_content(f"CHART — {title}"[:48],
                                f"{kind}: " + ", ".join(f"{k}={_fmt(v)}"
                                                        for k, v in pairs))
        except Exception:
            pass
    return result


TOOL = {
    "name": "chart",
    "description": (
        "Draws a bar, line or pie chart as an SVG file from simple numeric "
        "data ('a=1, b=2' or label→value). Use when the user asks to "
        "visualise numbers: distributions, comparisons, trends. Returns the "
        "file path. Data must be numeric."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING", "description": "bar | line | pie."},
            "data": {"type": "STRING",
                     "description": "Values: 'label=value, label=value' or a mapping."},
            "title": {"type": "STRING", "description": "Chart title."},
        },
        "required": ["data"],
    },
    "handler": chart,
}
