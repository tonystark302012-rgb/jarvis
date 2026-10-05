# dots/tools_spaces.py
"""Space/page tools for the Dot brain (doc §3.3).

Read tools give the Dot its workspace view; the write tools
(`create_space_page`, `edit_space_page`) NEVER touch a page — they insert
a `pending_changes` row only ("proposed — pending approval #N"), so the
HITL card is the only path to disk (T2).

Every tool is gated on the dot's `space` permission by the router in
dots/tools.py BEFORE this module's run() is reached.
"""
from __future__ import annotations

_MD_CAP = 12_000  # keep a page's md inside one tool result comfortably


def _perms(dot: dict) -> dict:
    p = dot.get("permissions")
    return p if isinstance(p, dict) else {}


PRIV = "space"

SPECS = [
    {"type": "function",
     "function": {
         "name": "list_authorized_spaces",
         "description": "List the owner's spaces (workspaces) you may use.",
         "parameters": {"type": "object", "properties": {},
                        "required": []}}},
    {"type": "function",
     "function": {
         "name": "list_space_pages",
         "description": "List pages in a space: id, rev, parent, title.",
         "parameters": {"type": "object",
                        "properties": {"space_id": {"type": "integer"}},
                        "required": ["space_id"]}}},
    {"type": "function",
     "function": {
         "name": "read_space_page",
         "description": ("Read one page: title, rev (needed for edits), "
                         "markdown body and research sources."),
         "parameters": {"type": "object",
                        "properties": {"page_id": {"type": "integer"}},
                        "required": ["page_id"]}}},
    {"type": "function",
     "function": {
         "name": "create_space_page",
         "description": ("PROPOSE a new page (it is saved only after the "
                         "owner approves; nothing is written now). "
                         "sources = research links [{title,url} or urls]."),
         "parameters": {"type": "object",
                        "properties": {
                            "space_id": {"type": "integer"},
                            "title": {"type": "string"},
                            "content_md": {"type": "string"},
                            "parent_id": {"type": "integer"},
                            "reason": {"type": "string"},
                            "sources": {"type": "array", "items": {}},
                        },
                        "required": ["space_id", "title"]}}},
    {"type": "function",
     "function": {
         "name": "edit_space_page",
         "description": ("PROPOSE an edit to an existing page — a pending "
                         "change only; the owner's approval applies it iff "
                         "base_rev still matches (stale = refused). "
                         "Pass base_rev from read_space_page; omit "
                         "sources to keep the page's current sources, "
                         "[] to clear them."),
         "parameters": {"type": "object",
                        "properties": {
                            "page_id": {"type": "integer"},
                            "base_rev": {"type": "integer"},
                            "content_md": {"type": "string"},
                            "title": {"type": "string"},
                            "reason": {"type": "string"},
                            "sources": {"type": "array", "items": {}},
                        },
                        "required": ["page_id", "base_rev"]}}},
]


def allowed(dot: dict) -> bool:
    return bool(_perms(dot).get("space"))


def run(dot: dict, name: str, args: dict) -> str:
    from . import store

    if name == "list_authorized_spaces":
        spaces = store.list_spaces()
        if not spaces:
            return ("no spaces yet — the owner creates one with "
                    "pages action=space_create name=…")
        return "\n".join(f"#{s['id']} {s['name']}" for s in spaces)

    if name == "list_space_pages":
        sid = int(args["space_id"])
        if store.get_space(sid) is None:
            return f"error: no space #{sid}"
        pages = store.list_pages(sid)
        if not pages:
            return f"space #{sid} has no pages yet"
        return "\n".join(
            f"#{p['id']} rev {p['rev']} parent={p.get('parent_id')} "
            f"— {p['title']}" for p in pages)

    if name == "read_space_page":
        page = store.get_page(int(args["page_id"]))
        if page is None:
            return f"error: no page #{args.get('page_id')}"
        body = page["content_md"] or "(empty)"
        if len(body) > _MD_CAP:
            body = body[:_MD_CAP].rsplit("\n", 1)[0] + "\n… [truncated]"
        srcs = page.get("sources") or []
        src_txt = "\n".join(f"- {s.get('title') or ''} {s.get('url')}"
                            for s in srcs) or "(none)"
        return (f"page #{page['id']} rev {page['rev']} "
                f"space #{page['space_id']} parent {page['parent_id']}\n"
                f"title: {page['title']}\n"
                f"sources:\n{src_txt}\n---\n{body}")

    if name == "create_space_page":
        try:
            p = store.propose_create(
                dot["id"], int(args["space_id"]),
                str(args.get("title") or ""),
                content_md=str(args.get("content_md") or ""),
                parent_id=(int(args["parent_id"])
                           if args.get("parent_id") not in (None, "")
                           else None),
                reason=str(args.get("reason") or ""),
                sources=args.get("sources"))
        except (KeyError, ValueError, TypeError) as e:
            return f"error: {e}"
        return (f"proposed — pending approval #{p['id']} "
                f"(new page in space #{p['space_id']}; nothing saved yet)")

    if name == "edit_space_page":
        if args.get("base_rev") in (None, ""):
            return ("edit_space_page needs base_rev — call read_space_page "
                    "first and pass its rev. Nothing was changed.")
        if args.get("content_md") is None and args.get("title") is None:
            return ("edit_space_page: pass content_md (the new markdown) "
                    "or a new title. Nothing was changed.")
        try:
            base_rev = int(args["base_rev"])
            page_id = int(args["page_id"])
        except (KeyError, ValueError, TypeError) as e:
            return f"error: bad argument ({e})"
        try:
            p = store.propose_edit(
                dot["id"], page_id, base_rev,
                title=args.get("title"),
                content_md=(str(args["content_md"])
                            if args.get("content_md") is not None else None),
                reason=str(args.get("reason") or ""),
                sources=args.get("sources"))
        except (KeyError, ValueError) as e:
            return f"error: {e}"
        return (f"proposed — pending approval #{p['id']} "
                f"(edit of page #{page_id} from rev {base_rev}; "
                f"nothing saved yet)")

    return f"unknown space tool: {name}"
