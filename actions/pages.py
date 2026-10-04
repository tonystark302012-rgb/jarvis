# actions/pages.py
"""pages — the Notion-like workspace BY VOICE/TOOL (owner side).

Spaces hold pages (nested), pages hold block content with honest
revisions: every save carries/derives a base revision, a stale write
gets a conflict message instead of silently overwriting. Dot-created
content NEVER lands directly — it arrives as a proposal you review via
`dots action=pending_list`.

The dashboard serves the same data through dots/server's router (one
JARVIS server, one login). Visual editor + slash commands = dashboard
UI batch; this action is the fully working voice/text surface NOW.

    pages action=space_create name=Research
    pages action=page_create space=Research title="Topic X" content="..."
    pages action=page_show page="Topic X"
    pages action=page_save page="Topic X" content="new body"
    pages action=revisions page="Topic X"
"""
from __future__ import annotations



def _resolve_space(params: dict):
    from dots import store
    raw = str(params.get("space") or "").strip()
    if not raw:
        spaces = store.list_spaces()
        return spaces[0] if len(spaces) == 1 else None
    if raw.isdigit():
        return store.get_space(int(raw))
    want = raw.lower()
    for s in store.list_spaces():
        if s["name"].lower() == want:
            return s
    return None


def _resolve_page(params: dict):
    from dots import store
    raw = str(params.get("page") or params.get("title") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return store.get_page(int(raw))
    want = raw.lower()
    hit = None
    for s in store.list_spaces():
        for p in store.list_pages(s["id"]):
            if p["title"].lower() == want:
                if hit is not None:          # ambiguous title
                    return None
                hit = p
    return hit


def pages(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "list").lower().strip()
    from dots import store

    if action in ("space_list", "spaces"):
        ss = store.list_spaces()
        if not ss:
            return "No spaces yet — pages action=space_create name=Research"
        out = []
        for s in ss:
            n = len(store.list_pages(s["id"]))
            out.append(f"#{s['id']} {s['name']} — {n} page(s)")
        return "Spaces:\n" + "\n".join(out)

    if action == "space_create":
        try:
            s = store.create_space(params.get("name"))
        except ValueError as e:
            return str(e)
        return f"Space '{s['name']}' created (#{s['id']})."

    if action in ("page_list", "list"):
        s = _resolve_space(params)
        if s is None:
            return _unknown_space(params)
        rows = store.list_pages(s["id"])
        if not rows:
            return f"Space '{s['name']}' has no pages yet."
        return f"Pages in '{s['name']}':\n" + "\n".join(
            f"#{p['id']} {p['title']} (rev {p['rev']})" for p in rows)

    if action == "page_create":
        s = _resolve_space(params)
        if s is None:
            return _unknown_space(params)
        title = str(params.get("title") or "").strip()
        if not title:
            return "Give a title — pages action=page_create space=… title=…"
        parent_id = None
        if params.get("parent"):
            parent = _resolve_page({"page": params["parent"]})
            if parent is None:
                return f"No parent page {params['parent']!r}."
            parent_id = parent["id"]
        page, err = store.owner_create_page(
            s["id"], title, content_md=params.get("content") or
            params.get("content_md"), parent_id=parent_id)
        if err:
            return err
        return (f"Page created in '{s['name']}': #{page['id']} "
                f"{page['title']!r} (rev {page['rev']})")

    if action in ("page_show", "show", "read"):
        p = _resolve_page(params)
        if p is None:
            return _unknown_page(params)
        body = p["content_md"] or "(empty page)"
        if player is not None:
            try:
                player.show_content(f"PAGE — {p['title']}"[:48],
                                    body[:4000])
            except Exception:
                pass
        srcs = p.get("sources") or []
        src_txt = ""
        if srcs:
            src_txt = "\nSources:\n" + "\n".join(
                f"- {s.get('title') or ''} {s.get('url')}".rstrip()
                for s in srcs)
        return (f"{p['title']} (rev {p['rev']}, space #{p['space_id']}):\n"
                f"{body}{src_txt}")

    if action in ("page_save", "save", "edit"):
        p = _resolve_page(params)
        if p is None:
            return _unknown_page(params)
        content = params.get("content")
        if content is None and params.get("content_md") is not None:
            content = params.get("content_md")
        if content is None and params.get("content_json") is None and \
                params.get("title") is None:
            return "Give new content — pages action=page_save page=… content=…"
        # voice has no editor state: derive base_rev from the live page
        # unless the caller (dashboard UI) sends one explicitly
        base = params.get("base_rev", p["rev"])
        page, conflict = store.owner_save_page(
            p["id"], base, title=params.get("title"),
            content_json=params.get("content_json"),
            content_md=content)
        if conflict is not None:
            if conflict.get("conflict"):
                return (f"CONFLICT — page moved to rev "
                        f"{conflict['current_rev']} while I was editing; "
                        f"nothing was overwritten. Re-read it and save "
                        f"again.")
            return conflict.get("error", "save failed")
        return f"Saved: {page['title']!r} → rev {page['rev']}"

    if action == "revisions":
        p = _resolve_page(params)
        if p is None:
            return _unknown_page(params)
        revs = store.list_revisions(p["id"])
        if not revs:
            return "No revisions."
        return "\n".join(
            f"rev {r['rev']} — {r['author']} — {r['note'] or 'edit'} "
            f"({time_str(r['created_at'])})" for r in revs)

    if action == "chat":
        # page-scoped conversation: dot reads THIS page (owner picks dot)
        message = str(params.get("message") or "").strip()
        if not message:
            return "Give a message — pages action=chat page=… dot=… message=…"
        p = _resolve_page(params)
        if p is None:
            return _unknown_page(params)
        from actions.dots import _require_dot
        d, err = _require_dot(params)
        if err:
            return err
        from dots import brain
        convo = f"page:{p['id']}"
        store.add_message(convo, "user", message)
        answer = brain.reply(d, convo, message, page=p)
        store.add_message(convo, "dot", answer)
        try:
            from dots import learning
            learning.mine()          # drafts only — never auto-publish
        except Exception:
            pass
        return answer

    return ("Unknown pages action — use: space_list | space_create | "
            "page_list | page_create | page_show | page_save | "
            "revisions | chat")


def _unknown_space(params: dict) -> str:
    from dots import store
    names = ", ".join(s["name"] for s in store.list_spaces()) or "(none)"
    return f"Unknown space {params.get('space')!r}. Spaces: {names}."


def _unknown_page(params: dict) -> str:
    raw = params.get("page") or params.get("title") or "?"
    return (f"Unknown page {raw!r} — give its #id or exact title "
            f"(pages action=page_list).")


def time_str(ts: float) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


TOOL = {
    "name": "pages",
    "description": (
        "The Notion-like workspace by voice: spaces and pages (nested), "
        "block content saved as markdown, honest revision history. Owner "
        "saves apply immediately (voice derives the current revision); a "
        "conflict message means nothing was overwritten — re-read and "
        "retry. Dot proposals are NOT saved here — review them with the "
        "dots action. Use for 'page banao', 'notes dikhao', 'us page me "
        "ye add karo'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "space_list | space_create | page_list "
                                      "| page_create | page_show | page_save "
                                      "| revisions | chat"},
            "space": {"type": "STRING",
                      "description": "Space name or #id"},
            "name": {"type": "STRING",
                     "description": "New space name (space_create)"},
            "page": {"type": "STRING",
                     "description": "Page #id or exact title"},
            "title": {"type": "STRING", "description": "Page title"},
            "parent": {"type": "STRING",
                       "description": "Parent page #id/title (nested "
                                      "subpage for page_create)"},
            "content": {"type": "STRING",
                        "description": "Markdown content (create/save)"},
            "base_rev": {"type": "NUMBER",
                         "description": "Optional explicit revision for "
                                        "save (dashboard UI sends this)"},
            "message": {"type": "STRING",
                        "description": "Message for page chat"},
            "dot": {"type": "STRING",
                    "description": "Dot to answer the page chat"},
        },
        "required": [],
    },
    "handler": pages,
}
