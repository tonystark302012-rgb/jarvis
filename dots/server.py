# dots/server.py
"""FastAPI surface for the Dots platform (batch 6a: dots, spaces, pages,
revisions, approvals, memory, conversations). Same deps as the JARVIS
dashboard — no new packages.
"""
from __future__ import annotations


from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import brain, store

app = FastAPI(docs_url="/api/docs", redoc_url=None)


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


@app.get("/api/health")
def health() -> dict:
    from . import db
    db._conn()
    return {"ok": True, "service": "dots", "version": "0.1.0"}


# ── dots ─────────────────────────────────────────────────────────────────────

@app.get("/api/dots")
def api_list_dots() -> list[dict]:
    return store.list_dots()


@app.post("/api/dots", status_code=201)
def api_create_dot(payload: dict = Body(...)) -> dict:
    try:
        return store.create_dot(payload.get("name"),
                                payload.get("role") or
                                payload.get("role_instructions") or "",
                                payload.get("permissions"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                      # UNIQUE constraint etc.
        raise HTTPException(400, str(e))


@app.get("/api/dots/{dot_id}")
def api_get_dot(dot_id: int) -> dict:
    d = store.get_dot(dot_id)
    if d is None:
        raise HTTPException(404, f"no dot #{dot_id}")
    return d


@app.patch("/api/dots/{dot_id}")
def api_update_dot(dot_id: int, payload: dict = Body(...)) -> dict:
    try:
        return store.update_dot(dot_id, name=payload.get("name"),
                                role=payload.get("role"),
                                permissions=payload.get("permissions"))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/dots/{dot_id}")
def api_delete_dot(dot_id: int) -> dict:
    if not store.delete_dot(dot_id):
        raise HTTPException(404, f"no dot #{dot_id}")
    return {"ok": True}


# ── spaces ───────────────────────────────────────────────────────────────────

@app.get("/api/spaces")
def api_list_spaces() -> list[dict]:
    return store.list_spaces()


@app.post("/api/spaces", status_code=201)
def api_create_space(payload: dict = Body(...)) -> dict:
    try:
        return store.create_space(payload.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/spaces/{space_id}/pages")
def api_list_pages(space_id: int) -> list[dict]:
    if store.get_space(space_id) is None:
        raise HTTPException(404, f"no space #{space_id}")
    return store.list_pages(space_id)


@app.post("/api/spaces/{space_id}/pages", status_code=201)
def api_owner_create_page(space_id: int,
                          payload: dict = Body(...)) -> dict:
    page, err = store.owner_create_page(
        space_id, payload.get("title") or "Untitled",
        content_json=payload.get("content_json"),
        content_md=payload.get("content_md"),
        parent_id=payload.get("parent_id"))
    if err:
        raise HTTPException(400, err)
    return page


# ── pages: read, owner save with revision check, revisions ───────────────────

@app.get("/api/pages/{page_id}")
def api_get_page(page_id: int) -> dict:
    p = store.get_page(page_id)
    if p is None:
        raise HTTPException(404, f"no page #{page_id}")
    return p


@app.patch("/api/pages/{page_id}")
def api_save_page(page_id: int, payload: dict = Body(...)) -> dict:
    page, conflict = store.owner_save_page(
        page_id, payload.get("base_rev"),
        title=payload.get("title"),
        content_json=payload.get("content_json"),
        content_md=payload.get("content_md"))
    if conflict is not None:
        status = 409 if conflict.get("conflict") else 400
        return JSONResponse(status_code=status, content=conflict)
    return page


@app.get("/api/pages/{page_id}/revisions")
def api_revisions(page_id: int) -> list[dict]:
    if store.get_page(page_id) is None:
        raise HTTPException(404, f"no page #{page_id}")
    return store.list_revisions(page_id)


@app.get("/api/pages/{page_id}/revisions/{rev}")
def api_revision(page_id: int, rev: int) -> dict:
    r = store.get_revision(page_id, rev)
    if r is None:
        raise HTTPException(404, f"no rev {rev} on page #{page_id}")
    return r


# ── conversations: per-dot and per-page (separate by construction) ──────────

@app.get("/api/conversations/{convo_key:path}/messages")
def api_list_messages(convo_key: str, limit: int = 50) -> list[dict]:
    return store.list_messages(convo_key, limit=limit)


@app.post("/api/conversations/{convo_key:path}/messages", status_code=201)
def api_post_message(convo_key: str, payload: dict = Body(...)) -> dict:
    text = str(payload.get("content") or "").strip()
    if not text:
        raise HTTPException(400, "content required")
    dot_id = payload.get("dot_id")
    page_id = payload.get("page_id")
    page = None
    if page_id is not None:
        page = store.get_page(page_id)
        if page is None:
            raise HTTPException(404, f"no page #{page_id}")
    dot = None
    if dot_id is not None:
        dot = store.get_dot(dot_id)
        if dot is None:
            raise HTTPException(404, f"no dot #{dot_id}")
    store.add_message(convo_key, "user", text)
    if dot is None:
        # owner's own note / system channel — no brain turn
        return {"stored": True}
    answer = brain.reply(dot, convo_key, text, page=page)
    msg = store.add_message(convo_key, "dot", answer)
    return {"stored": True, "reply": msg}


# ── approvals (human-in-the-loop review cards) ──────────────────────────────

@app.get("/api/pending")
def api_pending(status: str = "pending") -> list[dict]:
    return store.list_pending(status)


@app.post("/api/pending/{pid}/approve")
def api_approve(pid: int) -> dict:
    out = store.approve_pending(pid)
    if "error" in out:
        raise HTTPException(400 if "no proposal" in out["error"] else 409,
                            out["error"])
    if out.get("conflict"):
        return JSONResponse(status_code=409, content=out)
    return out


@app.post("/api/pending/{pid}/decline")
def api_decline(pid: int) -> dict:
    out = store.decline_pending(pid)
    if "error" in out:
        raise HTTPException(409, out["error"])
    return out


# ── memory (preferences) ─────────────────────────────────────────────────────

@app.get("/api/memory")
def api_memory() -> list[dict]:
    return store.list_prefs()


@app.put("/api/memory")
def api_memory_set(payload: dict = Body(...)) -> dict:
    try:
        return store.set_pref(payload.get("key"), payload.get("value"),
                              payload.get("allowed", "*"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/memory/{pref_id}")
def api_memory_delete(pref_id: int) -> dict:
    if not store.delete_pref(pref_id):
        raise HTTPException(404, f"no preference #{pref_id}")
    return {"ok": True}
