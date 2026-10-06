"""
atspi — accessible-application scanner for Linux (Report #1 / Atspi).

Walks the AT-SPI2 accessibility tree (the same bus screen readers use)
so JARVIS can find and operate UI elements by ROLE + ACCESSIBLE NAME
instead of pixels — a second, robust locator channel for agenic
computer control, and a real scanner for "what apps/windows/controls
exist right now".

Guaranteed-free (AT-SPI is part of every Linux desktop), but the
bindings are an OPTIONAL system dependency:
  * bridge = gir1.2-atspi-2.0 + python3-gi (preferred), or pyatspi
  * without them every action returns the exact apt line — never a
    fake tree.

All paths are re-resolved at act() time (the live tree moves), and
every failure is reported as-is.
"""
from __future__ import annotations
from typing import Any


_INSTALL = ("AT-SPI bindings missing — free install: "
            "`sudo apt install python3-gi gir1.2-atspi-2.0` "
            "(or `pip install pyatspi`), then retry. I show real "
            "accessibility data only — never a fabricated tree.")


# ── seams (tests replace these) ────────────────────────────────────────────

def _bridge():
    """Return the Atspi module or None. Single import seam."""
    try:
        from gi.repository import Atspi          # preferred (gir bindings)
        return Atspi
    except Exception:
        pass
    try:
        import pyatspi                            # fallback
        return pyatspi
    except Exception:
        return None


def _desktop(bridge):
    return bridge.getDesktop(0)


def _children(node) -> list:
    try:
        n = node.childCount
    except Exception:
        return []
    out = []
    for i in range(min(int(n), 64)):             # per-node cap
        try:
            out.append(node.getChildAtIndex(i))
        except Exception:
            continue
    return [c for c in out if c is not None]


def _prop(node, name: str, default: str = "") -> str:
    try:
        v = getattr(node, name)
        if callable(v):
            v = v()
        return str(v if v is not None else default)
    except Exception:
        return default


# ── walk / format ───────────────────────────────────────────────────────────

def _node_dict(node, depth: int) -> dict:
    role = _prop(node, "roleName", "?")
    name = _prop(node, "name", "")
    d: dict[str, Any] = {"role": role, "name": name,
                         "desc": _prop(node, "description", ""),
                         "states": _prop(node, "states", "")[:120],
                         "children": []}
    if depth <= 0:
        return d
    for c in _children(node):
        d["children"].append(_node_dict(c, depth - 1))
    return d


def _format(d: dict, indent: int = 0, max_depth: int = 6) -> str:
    """Tree → text (pure — unit tested)."""
    pad = "  " * indent
    label = d.get("name") or ""
    line = f"{pad}{d.get('role', '?')}"
    if label:
        line += f": {label[:80]}"
    lines = [line]
    if indent < max_depth:
        for c in d.get("children", []):
            lines.extend(
                _format(c, indent + 1, max_depth).splitlines())
    return "\n".join(lines)


def _flatten(d: dict, crumbs: tuple = ()) -> list[dict]:
    """All nodes with breadcrumbs — search index (pure)."""
    here = crumbs + (d,)
    out = [dict(_crumbs=here)]
    for c in d.get("children", []):
        out += _flatten(c, here)
    return out


def _crumb_text(crumbs: tuple) -> str:
    return " > ".join((c.get("name") or c.get("role", "?"))[:40]
                      for c in crumbs)


# ── tool ────────────────────────────────────────────────────────────────────

def atspi(parameters: dict | None = None, player=None,
          session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "scan")).lower().strip()
    bridge = _bridge()
    if bridge is None:
        return _INSTALL

    if action == "scan":
        depth = int(params.get("depth", 3) or 3)
        depth = max(1, min(6, depth))
        try:
            desk = _desktop(bridge)
            root = _node_dict(desk, depth)
        except Exception as e:
            return f"AT-SPI desktop walk failed: {e}"
        apps = root.get("children", [])
        if not apps:
            return ("AT-SPI reports no applications on the desktop "
                    "(are you on a graphical session?).")
        return f"Desktop tree (depth {depth}, {len(apps)} app(s)):\n" + \
            _format(root, max_depth=depth + 1)

    if action in ("find", "search"):
        query = str(params.get("query", "")).strip()
        if not query:
            return "find needs query=… (role or name to look for)."
        try:
            root = _node_dict(_desktop(bridge), 6)
        except Exception as e:
            return f"AT-SPI desktop walk failed: {e}"
        q = query.lower()
        hits = []
        for item in _flatten(root):
            node = item["_crumbs"][-1]
            hay = f"{node.get('role', '')} {node.get('name', '')}".lower()
            if q in hay:
                hits.append(f"- {node.get('role')}: "
                            f"{node.get('name', '')[:60]!r}  @ "
                            f"{_crumb_text(item['_crumbs'])}")
            if len(hits) >= 8:
                break
        if not hits:
            return f"No accessibility nodes match {query!r}."
        return (f"{len(hits)} match(es) for {query!r}:\n"
                + "\n".join(hits))

    if action in ("act", "action", "click"):
        path = str(params.get("path", "")).strip()
        if not path:
            return ("act needs path=… — the ' > ' breadcrumb line from "
                    "find (it is re-resolved live, the tree moves).")
        parts = [p.strip() for p in path.split(">") if p.strip()]
        idx = int(params.get("index", 1) or 1)
        try:
            node = _desktop(bridge)
            for part in parts:
                nxt = None
                for c in _children(node):
                    nm = (_prop(c, "name", "") or _prop(c, "roleName", ""))
                    if part in nm or part == _prop(c, "roleName", ""):
                        nxt = c
                        break
                if nxt is None:
                    return (f"Path segment {part!r} not found live — the "
                            "tree changed. Re-run find.")
                node = nxt
            actions = node.queryAction()
            n = actions.nActions
            if n <= 0:
                return f"Node has no accessible actions (role={_prop(node, 'roleName')})."
            i = max(1, min(int(idx), int(n)))
            ok = actions.doAction(i - 1)
            return (f"Action {i} {'done' if ok else 'refused'} on "
                    f"{_prop(node, 'roleName')}:"
                    f"{_prop(node, 'name')!r} ({n} action(s) available).")
        except Exception as e:
            return f"act failed: {e}"

    return "action must be scan | find | act."


TOOL = {
    "name": "atspi",
    "description": (
        "Linux AT-SPI2 accessibility scanner: action=scan shows the "
        "live desktop/app/control tree (roles + accessible names), "
        "action=find locates a control by role/name with a ' > ' "
        "breadcrumb path, action=act re-resolves that path and performs "
        "accessible action index (default 1 = click/activate). Use for "
        "pixel-free app control, 'what's on screen', scanner-style "
        "recon. Requires free system bindings (python3-gi + "
        "gir1.2-atspi-2.0) — honest install hint otherwise."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "scan (default) | find | act."},
            "query": {"type": "STRING",
                      "description": "Role/name to search (find)."},
            "path": {"type": "STRING",
                     "description": "Breadcrumb path from find (act)."},
            "index": {"type": "INTEGER",
                      "description": "1-based accessible action (act)."},
            "depth": {"type": "INTEGER",
                      "description": "Scan depth 1-6 (scan)."},
        },
        "required": [],
    },
    "handler": atspi,
}
