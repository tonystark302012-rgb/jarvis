# dots/tools.py
"""Permission-gated tool router for the Dot brain (doc §3.3).

Gate order is fixed: owner-defined permission check FIRST, execution
second, and every refusal is an honest string the model can read
(`denied: …`) — never a silent drop. `review_space_page` is deliberately
absent from the spec list: Dots never approve their own work; if one
tries, it gets an honest owner-only answer (T5 family).
"""
from __future__ import annotations

from . import tools_research, tools_spaces

_MODULES = (tools_spaces, tools_research)

# tool name → owning module (spec exposure + dispatch share this index)
_OWNERS: dict = {spec["function"]["name"]: m
                 for m in _MODULES for spec in m.SPECS}

# in the spec's tool table but OWNER-side only (never a brain tool)
_OWNER_ONLY = {
    "review_space_page":
        "review_space_page is owner-only — the owner decides with "
        "dots action=approve which=<id> / decline (Dots never "
        "self-approve).",
}


def specs_for(dot: dict) -> list[dict]:
    """Tool schemas this specific dot may call (empty = no tools)."""
    out: list[dict] = []
    for m in _MODULES:
        if m.allowed(dot):
            out.extend(m.SPECS)
    return out


def run_tool(dot: dict, name: str, args: dict | None) -> str:
    """Gate → execute → string result. Never raises; refusals are honest."""
    name = str(name or "")
    if name in _OWNER_ONLY:
        return _OWNER_ONLY[name]
    owner = _OWNERS.get(name)
    if owner is None:
        return f"unknown tool: {name}"
    if not owner.allowed(dot):
        return (f"denied: '{name}' needs the {owner.PRIV!r} permission — "
                f"this dot has it off (owner can grant it: "
                f"dots action=dot_show dot=…).")
    try:
        return str(owner.run(dot, name, dict(args or {})))
    except Exception as e:  # tool bugs must not kill the conversation
        return f"error: {name} failed ({type(e).__name__}: {e})"
