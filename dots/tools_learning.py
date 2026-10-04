# dots/tools_learning.py
"""Skill tools for the Dot brain (doc §3.8).

Exposure is NOT a permission: a published skill is owner-approved
knowledge that every Dot may load (drafts/archived never reach these
tools — T9). `read_skill_file` is jailed to `memory/dots_skills/`, the
owner-managed folder where reference files live.
"""
from __future__ import annotations

from pathlib import Path

SPECS = [
    {"type": "function",
     "function": {
         "name": "load_skill",
         "description": ("Load a PUBLISHED skill. No argument → list "
                         "published skill ids+titles; with id or query "
                         "→ full skill body. Drafts are owner-only."),
         "parameters": {"type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": []}}},
    {"type": "function",
     "function": {
         "name": "read_skill_file",
         "description": ("Read a reference file the owner placed in the "
                         "skills folder (memory/dots_skills/, jailed)."),
         "parameters": {"type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"]}}},
]

PRIV = "skills"          # no dot-level gate; published = shared by design

_CAP = 200 * 1024


def allowed(dot: dict) -> bool:
    return True


def _skills_dir() -> Path:
    from config import get_base_dir
    d = Path(get_base_dir()) / "memory" / "dots_skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _jail(path_str: str) -> Path:
    raw = str(path_str or "").strip()
    if not raw:
        raise ValueError("path required")
    base = _skills_dir().resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = base / target
    resolved = target.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValueError(f"path escapes the skills folder: {raw!r}")
    return resolved


def run(dot: dict, name: str, args: dict) -> str:
    from . import store
    a = args if isinstance(args, dict) else {}

    if name == "load_skill":
        q = str(a.get("query") or "").strip()
        pub = store.list_skills("published")
        if not pub:
            return ("no published skills yet — drafts exist only for "
                    "the owner until they publish them")
        if not q:
            return "\n".join(f"[{s['id']}] {s['title']}" for s in pub)
        if q.isdigit():
            s = store.get_skill(int(q))
            if s is not None and s["status"] == "published":
                return f"{s['title']}\n\n{s['body_md']}"
            return (f"no PUBLISHED skill #{q} "
                    f"(drafts are owner-only — honest refusal)")
        ql = q.lower()
        for s in pub:
            if ql in (s["title"] or "").lower() \
                    or ql in (s["body_md"] or "").lower():
                return f"{s['title']}\n\n{s['body_md']}"
        return f"no published skill matches {q!r} — try load_skill with no argument"

    if name == "read_skill_file":
        try:
            target = _jail(a.get("path"))
        except ValueError as e:
            return f"denied: {e}"
        if not target.is_file():
            return f"no such skill file: {a.get('path')!r}"
        try:
            data = target.read_bytes()[:_CAP]
        except OSError as e:
            return f"error: {e}"
        return data.decode("utf-8", errors="replace")

    return f"unknown learning tool: {name}"
