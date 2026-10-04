# actions/dots.py
"""dots — JARVIS's specialist agents (Dots) BY VOICE/TOOL.

The Dots ENGINE lives in dots/ (storage, approvals, brain — shared with
the dashboard's workspace routes). This action is the JARVIS-native
surface: create specialists, talk to each one (every Dot keeps its OWN
conversation), review their proposed page edits, and manage the
preferences (memory) they are allowed to read.

    dots action=dot_create name=Researcher role="cite every source" \
         perms="research,memory,space"
    dots action=chat dot=Researcher message="compare these two papers"
    dots action=pending_list
    dots action=approve which=3
    dots action=memory_set key=language value=Hinglish

Duplicate-check: multi_agent (one-shot dry run) and task_agent (goal
planner) are UNCHANGED — Dots are persistent personas with separate
conversations, permissions and review rights; nothing here replaces
them.
"""
from __future__ import annotations

import json


def _resolve_dot(params: dict) -> dict | None:
    raw = str(params.get("dot") or params.get("name") or "").strip()
    if not raw:
        return None
    from dots import store
    if raw.isdigit():
        return store.get_dot(int(raw))
    return store.find_dot_by_name(raw)


def _parse_perms(raw) -> dict | None:
    """dot_create perms: 'research,memory,space' | 'all' | JSON object |
    already-a-dict → permissions dict (doc: per-Dot permissions)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (list, tuple)):
        return {str(k).strip(): True for k in raw if str(k).strip()}
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    if s.lower() == "all":
        return {"research": True, "memory": True, "space": True}
    return {k.strip().lower(): True for k in s.split(",") if k.strip()}


def _require_dot(params: dict) -> tuple[dict | None, str | None]:
    d = _resolve_dot(params)
    if d is None:
        from dots import store
        known = ", ".join(x["name"] for x in store.list_dots()) or "(none)"
        return None, f"No Dot named {params.get('dot')!r}. Known: {known}."
    return d, None


def dots(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "chat").lower().strip()
    from dots import store

    # ── specialist agents ──────────────────────────────────────────────
    if action in ("dot_create", "create"):
        try:
            d = store.create_dot(
                params.get("dot") or params.get("name"),
                params.get("role") or "",
                permissions=_parse_perms(params.get("perms")
                                         or params.get("permissions")))
        except ValueError as e:
            return str(e)
        granted = ", ".join(sorted(k for k, v in d["permissions"].items()
                                   if v)) or "none"
        return (f"Dot '{d['name']}' created (#{d['id']}) "
                f"[perms: {granted}]. "
                f"Talk to it: dots action=chat dot={d['name']} "
                f"message=...")

    if action in ("dot_list", "list"):
        ds = store.list_dots()
        if not ds:
            return ("No Dots yet — create one: "
                    "dots action=dot_create name=Researcher role=...")
        return "\n".join(
            f"#{x['id']} {x['name']} — "
            f"{(x['role_instructions'] or '(no role)')[:80]}" for x in ds)

    if action in ("dot_show", "show"):
        d, err = _require_dot(params)
        if err:
            return err
        return (f"Dot #{d['id']} '{d['name']}'\n"
                f"Role: {d['role_instructions'] or '(none)'}\n"
                f"Permissions: {json.dumps(d['permissions'])}")

    if action in ("dot_delete", "delete"):
        d, err = _require_dot(params)
        if err:
            return err
        store.delete_dot(d["id"])
        return f"Dot '{d['name']}' deleted."

    # ── chat (separate conversation per Dot; optional page scope) ─────
    if action == "chat":
        message = str(params.get("message") or "").strip()
        if not message:
            return "Give a message — dots action=chat dot=… message=…"
        d, err = _require_dot(params)
        if err:
            return err
        page = None
        convo = f"dot:{d['id']}"
        page_ref = params.get("page")
        if page_ref not in (None, ""):
            page = _resolve_page(params)
            if page is None:
                return f"No page {page_ref!r}."
            convo = f"page:{page['id']}"
        from dots import brain
        store.add_message(convo, "user", message)
        answer = brain.reply(d, convo, message, page=page)
        store.add_message(convo, "dot", answer)
        return answer

    # ── human-in-the-loop review (owner side; Dots cannot self-approve)
    # ──────────────────────────────────────────────────────────────────
    if action in ("pending_list", "pending", "review"):
        rows = store.list_pending("pending")
        if not rows:
            return "No proposals waiting for review."
        out = []
        for p in rows:
            tgt = (f"page #{p['page_id']}" if p["kind"] == "edit"
                   else f"new page in space #{p['space_id']}")
            dot_name = (store.get_dot(p["dot_id"]) or {}).get("name",
                                                              "?")
            out.append(f"#{p['id']} [{p['kind']}] by {dot_name} → {tgt} "
                       f"(base rev {p['base_rev']}): "
                       f"{p['title']!r} — {p['reason'] or 'no reason'}")
        return ("Pending proposals:\n" + "\n".join(out) +
                "\nApprove: dots action=approve which=<id> · "
                "Decline: dots action=decline which=<id>")

    if action == "approve":
        which = str(params.get("which") or "").strip()
        if not which.isdigit():
            return "Give the proposal number — dots action=approve which=3"
        out = store.approve_pending(int(which))
        if "error" in out:
            return out["error"]
        if out.get("conflict"):
            return (f"NOT saved — {out['reason']}. Nothing was overwritten; "
                    f"tell the Dot to re-propose on the current revision.")
        return (f"Approved — page saved as rev "
                f"{out['page']['rev']}: {out['page']['title']!r}")

    if action == "decline":
        which = str(params.get("which") or "").strip()
        if not which.isdigit():
            return "Give the proposal number — dots action=decline which=3"
        out = store.decline_pending(int(which))
        return out.get("error") or "Declined — nothing was saved."

    # ── memory (preferences the permitted Dots may read) ──────────────
    if action in ("memory_list", "memory"):
        prefs = store.list_prefs()
        if not prefs:
            return "No preferences saved yet."
        return "\n".join(
            f"#{p['id']} {p['key']} = {p['value']} "
            f"(allowed: {p['allowed']})" for p in prefs)

    if action == "memory_set":
        key = str(params.get("key") or "").strip()
        value = str(params.get("value") or "").strip()
        if not key or not value:
            return "Give key and value — dots action=memory_set key=… value=…"
        allowed = params.get("allowed") or "*"
        if isinstance(allowed, str) and allowed.strip() != "*":
            allowed = [x.strip() for x in allowed.split(",") if x.strip()]
        try:
            p = store.set_pref(key, value, allowed)
        except ValueError as e:
            return str(e)
        return f"Saved: {p['key']} = {p['value']} (allowed: {p['allowed']})"

    if action == "memory_delete":
        which = str(params.get("which") or params.get("key") or "").strip()
        if which.isdigit():
            ok = store.delete_pref(int(which))
            return "Deleted." if ok else f"No preference #{which}."
        for p in store.list_prefs():
            if p["key"].lower() == which.lower():
                store.delete_pref(p["id"])
                return f"Deleted {p['key']}."
        return f"No preference {which!r}."

    return ("Unknown dots action — use: dot_create | dot_list | dot_show | "
            "dot_delete | chat | pending_list | approve | decline | "
            "memory_list | memory_set | memory_delete")


def _resolve_page(params: dict):
    from dots import store
    raw = str(params.get("page") or "").strip()
    if raw.isdigit():
        return store.get_page(int(raw))
    # voice fallback: unique title match
    want = raw.lower()
    for s in store.list_spaces():
        for p in store.list_pages(s["id"]):
            if p["title"].lower() == want:
                return p
    return None


TOOL = {
    "name": "dots",
    "description": (
        "Specialist agents (Dots): persistent personas, each with its own "
        "role, permissions and SEPARATE conversation. Use to create a "
        "specialist (dot_create), ask one something (chat, optionally "
        "scoped to a page), review the page edits Dots have proposed "
        "(pending_list → approve/decline — only the owner approves, and a "
        "stale proposal is refused, never overwritten), and manage the "
        "preferences Dots may read (memory_*). Example: 'Researcher se "
        "pucho, ye topic 3 sources ke saath summary bana do'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "dot_create | dot_list | dot_show | "
                                      "dot_delete | chat | pending_list | "
                                      "approve | decline | memory_list | "
                                      "memory_set | memory_delete"},
            "dot": {"type": "STRING",
                    "description": "Dot name or #id (chat/show/delete)"},
            "name": {"type": "STRING", "description": "New Dot's name"},
            "role": {"type": "STRING",
                     "description": "Role instructions for dot_create"},
            "perms": {"type": "STRING",
                      "description": "dot_create permissions: comma list "
                                     "(research,memory,space), 'all', or "
                                     "a JSON object; default none"},
            "message": {"type": "STRING",
                        "description": "Message for chat"},
            "page": {"type": "STRING",
                     "description": "Optional page id/title to scope chat"},
            "which": {"type": "STRING",
                      "description": "Proposal # (approve/decline) or "
                                     "preference # (memory_delete)"},
            "key": {"type": "STRING", "description": "Preference key"},
            "value": {"type": "STRING", "description": "Preference value"},
            "allowed": {"type": "STRING",
                        "description": "memory_set: '*' (default) or comma "
                                       "list of Dot names"},
        },
        "required": [],
    },
    "handler": dots,
}
