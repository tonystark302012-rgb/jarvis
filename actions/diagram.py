"""
diagram — generate SVG diagrams from a text spec.

Everything is drawn here in pure Python: no matplotlib, no graphviz, no
network. The SVG is written to disk and its path returned; the content panel
shows the spec, and any browser (including the dashboard) can open the file.

Supported kinds:
  flow      A -> B -> C  (linear or branching with "A -> B; A -> C")
  sequence  Alice: Hello / Bob: Hi back   (two-party message ladder)
  mindmap   central idea with (branch (sub)) nesting
  timeline  2024: Event one | 2025: Event two

The spec parser is intentionally forgiving — the model writes prose-ish
input, and a diagram that renders roughly beats an error.
"""
from __future__ import annotations

import html
import re
import time
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _out_path(name: str) -> Path:
    d = _base_dir() / "diagrams"
    d.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "diagram"
    return d / f"{safe}-{int(time.time())}.svg"


# ── layout helpers ───────────────────────────────────────────────────────────

_COLORS = ["#22d3ee", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa"]


def _svg(w: int, h: int, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Segoe UI, Arial, sans-serif">\n'
        f'<rect width="{w}" height="{h}" fill="#0b1220"/>\n'
        f'<text x="16" y="28" fill="#e2e8f0" font-size="15" '
        f'font-weight="600">{_esc(title)}</text>\n'
        f'{body}\n</svg>\n'
    )


def _node(x: int, y: int, w: int, h: int, label: str, color: str) -> str:
    cx, cy = x + w // 2, y + h // 2
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="#111c2e" stroke="{color}" stroke-width="2"/>\n'
        f'<text x="{cx}" y="{cy}" fill="#e2e8f0" font-size="13" '
        f'text-anchor="middle" dominant-baseline="central">'
        f'{_esc(label[:34])}</text>'
    )


def _arrow(x1: int, y1: int, x2: int, y2: int, color: str = "#64748b") -> str:
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
            f'stroke-width="1.8" marker-end="url(#a)"/>')


_ARROW_DEF = ('<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" '
              'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
              '<path d="M 0 0 L 10 5 L 0 10 z" fill="#64748b"/></marker></defs>')


def _parse_edges(spec: str) -> list[tuple[str, str]]:
    """'A -> B; A -> C' or 'A -> B -> C' → [(A,B),(B,C)...] per group."""
    edges: list[tuple[str, str]] = []
    for group in re.split(r"[;\n]", spec):
        nodes = [n.strip() for n in re.split(r"->|→", group) if n.strip()]
        for a, b in zip(nodes, nodes[1:]):
            edges.append((a, b))
    return edges


def _flow_svg(spec: str, title: str) -> str:
    edges = _parse_edges(spec)
    if not edges:
        raise ValueError("no 'A -> B' edges found")
    # unique nodes in first-seen order
    order: list[str] = []
    for a, b in edges:
        for n in (a, b):
            if n not in order:
                order.append(n)
    # columns = longest-path layering (BFS from sources)
    succ: dict[str, list[str]] = {}
    indeg = {n: 0 for n in order}
    for a, b in edges:
        succ.setdefault(a, []).append(b)
        indeg[b] += 1
    layer = {n: 0 for n in order}
    queue = [n for n in order if indeg[n] == 0] or [order[0]]
    seen = set()
    while queue:
        n = queue.pop(0)
        if n in seen:
            continue
        seen.add(n)
        for m in succ.get(n, []):
            if layer[m] < layer[n] + 1:
                layer[m] = layer[n] + 1
            queue.append(m)
    rows: dict[int, list[str]] = {}
    for n in order:
        rows.setdefault(layer[n], []).append(n)

    cols = max(rows) + 1
    nw, nh, gap_x, gap_y = 170, 48, 90, 40
    max_row = max(len(v) for v in rows.values())
    w = 60 + cols * nw + (cols - 1) * gap_x
    h = 60 + max_row * nh + (max_row - 1) * gap_y
    pos: dict[str, tuple[int, int]] = {}
    body = []
    for lv, nodes in sorted(rows.items()):
        for i, n in enumerate(nodes):
            x = 30 + lv * (nw + gap_x)
            y = 50 + i * (nh + gap_y)
            pos[n] = (x, y)
            body.append(_node(x, y, nw, nh, n, _COLORS[lv % len(_COLORS)]))
    for a, b in edges:
        ax, ay = pos[a]
        bx, by = pos[b]
        body.append(_arrow(ax + nw, ay + nh // 2, bx, by + nh // 2))
    return _svg(w, h, _ARROW_DEF + "\n".join(body), title)


def _sequence_svg(spec: str, title: str) -> str:
    # lines "Actor: message"
    msgs = []
    for line in spec.splitlines():
        line = line.strip()
        m = re.match(r"^\s*([A-Za-z0-9 _.-]{1,24})\s*:\s*(.+)$", line)
        if m:
            msgs.append((m.group(1).strip(), m.group(2).strip()))
    if not msgs:
        raise ValueError("expected lines like 'Alice: Hello'")
    actors: list[str] = []
    for who, _ in msgs:
        if who not in actors:
            actors.append(who)
    n = len(msgs)
    w = max(560, 160 + 180 * len(actors))
    h = 120 + 56 * n
    body = []
    xs = {a: 100 + i * ((w - 200) // max(1, len(actors) - 1) if len(actors) > 1 else 0)
          for i, a in enumerate(actors)}
    if len(actors) == 1:
        xs[actors[0]] = w // 2
    for a, x in xs.items():
        body.append(_node(x - 70, 48, 140, 40, a, _COLORS[0]))
        body.append(f'<line x1="{x}" y1="92" x2="{x}" y2="{h - 30}" '
                    f'stroke="#334155" stroke-width="1.5" stroke-dasharray="4 4"/>')
    for i, (who, msg) in enumerate(msgs):
        y = 130 + i * 56
        x1 = xs[who]
        others = [a for a in actors if a != who]
        if others:
            x2 = xs[others[0]]
        else:
            x2 = x1 + 40
        body.append(_arrow(x1, y, x2, y))
        mid = (x1 + x2) // 2
        body.append(f'<text x="{mid}" y="{y - 8}" fill="#cbd5e1" font-size="12" '
                    f'text-anchor="middle">{_esc(msg[:48])}</text>')
    return _svg(w, h, _ARROW_DEF + "\n".join(body), title)


def _mindmap_svg(spec: str, title: str) -> str:
    # "central (branch (leaf)(leaf2))(branch2)"
    root_m = re.match(r"^\s*([^(]+?)\s*\((.*)\)\s*$", spec.strip(), re.S)
    root = (root_m.group(1) if root_m else spec.strip().split("(")[0]).strip()
    branches: list[tuple[str, list[str]]] = []
    if root_m:
        for bm in re.finditer(r"\s*([^(]+?)\s*\(([^()]*)\)", "(" + root_m.group(2) + ")"):
            bname = bm.group(1).strip(" ()")
            leaves = [x.strip() for x in bm.group(2).split("(") ]
            leaves = [re.sub(r"\)", "", x).strip() for x in re.split(r"\)\s*\(", bm.group(2))]
            leaves = [x for x in leaves if x]
            if bname:
                branches.append((bname, leaves))
    if not branches:
        # one branch per line, none of them with children
        branches = [(ln.strip(), list[str]()) for ln in spec.splitlines() if ln.strip()][:8]
    if not branches:
        raise ValueError("expected 'idea (branch (leaf))' or lines of branches")

    row_h = 46
    rows = 1 + sum(1 + max(1, len(lv)) for _, lv in branches)
    w = 900
    h = 90 + rows * row_h
    cx, cy = w // 2, 70
    body = [f'<rect x="{cx-140}" y="{cy-24}" width="280" height="48" rx="12" '
            f'fill="#1e293b" stroke="{_COLORS[0]}" stroke-width="2.5"/>'
            f'<text x="{cx}" y="{cy}" fill="#f1f5f9" font-size="15" '
            f'font-weight="600" text-anchor="middle" dominant-baseline="central">'
            f'{_esc(root[:34])}</text>']
    y = cy + 60
    for bi, (bname, leaves) in enumerate(branches):
        color = _COLORS[(bi + 1) % len(_COLORS)]
        bx = 140
        body.append(_arrow(cx, cy + 24, bx + 130, y, color))
        body.append(_node(bx, y - 18, 260, 36, bname, color))
        sub = leaves or []
        sy = y - 18 - (len(sub) * 34) // 2 + 18
        if not sub:
            sy = y
        for li, leaf in enumerate(sub[:6]):
            ly = sy + li * 34
            lx = bx + 330
            body.append(_arrow(bx + 260, y, lx, ly + 14, "#475569"))
            body.append(_node(lx, ly, 300, 30, leaf, "#64748b"))
        y += row_h + 30 * max(0, len(sub) - 1)
    return _svg(w, max(h, y + 40), _ARROW_DEF + "\n".join(body), title)


def _timeline_svg(spec: str, title: str) -> str:
    items: list[tuple[str, str]] = []
    for chunk in re.split(r"[;\n|]", spec):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" in chunk:
            k, v = chunk.split(":", 1)
            items.append((k.strip(), v.strip()))
        else:
            items.append(("", chunk))
    if not items:
        raise ValueError("expected 'YYYY: event' entries")
    w = 900
    h = 90 + 70 * len(items)
    body = [f'<line x1="70" y1="70" x2="70" y2="{h - 30}" stroke="#334155" '
            f'stroke-width="3"/>']
    for i, (k, v) in enumerate(items):
        y = 80 + i * 70
        color = _COLORS[i % len(_COLORS)]
        body.append(f'<circle cx="70" cy="{y}" r="8" fill="{color}"/>')
        if k:
            body.append(f'<text x="94" y="{y - 6}" fill="{color}" font-size="13" '
                        f'font-weight="700">{_esc(k[:20])}</text>')
        body.append(f'<text x="94" y="{y + 14}" fill="#e2e8f0" font-size="13">'
                    f'{_esc(v[:80])}</text>')
    return _svg(w, h, "\n".join(body), title)


_RENDERERS = {
    "flow": _flow_svg,
    "sequence": _sequence_svg,
    "mindmap": _mindmap_svg,
    "timeline": _timeline_svg,
}


def diagram(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    kind = str(params.get("kind", "flow")).lower().strip()
    spec = str(params.get("spec", "")).strip()
    if not spec:
        return ("No spec given. Example: kind=flow, "
                "spec='Build -> Test -> Deploy'.")
    if kind not in _RENDERERS:
        return f"Unknown kind '{kind}'. Use: {', '.join(_RENDERERS)}."

    try:
        svg = _RENDERERS[kind](spec, str(params.get("title") or kind.title()))
    except ValueError as e:
        return f"Could not read the {kind} spec: {e}"

    out = _out_path(str(params.get("title") or kind))
    out.write_text(svg, encoding="utf-8")
    result = f"Diagram saved: {out}"
    if player is not None:
        try:
            player.show_content(f"DIAGRAM — {kind}"[:48],
                                f"spec:\n{spec[:1500]}\n\nsaved: {out}")
        except Exception:
            pass
    return result


TOOL = {
    "name": "diagram",
    "description": (
        "Draws an SVG diagram from a text spec and saves it. Kinds: flow "
        "('A -> B -> C' or 'A -> B; A -> C'), sequence ('Alice: hi' lines), "
        "mindmap ('idea (branch (leaf))'), timeline ('2025: event'). Use "
        "when the user asks for a flowchart, diagram, mindmap, timeline or "
        "'draw how X works'. Returns the file path."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "kind": {"type": "STRING",
                     "description": "flow | sequence | mindmap | timeline."},
            "spec": {"type": "STRING",
                     "description": "The diagram content in the kind's notation."},
            "title": {"type": "STRING", "description": "Optional diagram title."},
        },
        "required": ["kind", "spec"],
    },
    "handler": diagram,
}
