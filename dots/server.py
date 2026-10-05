# dots/server.py
"""Dots routes — an APIRouter mounted INTO the JARVIS dashboard app
(dashboard/server.py `_build_app`), so the workspace shares JARVIS's
one server, one login, one origin. NOT a standalone product: there is
no `python -m dots`.

Covers dots, spaces, pages, revisions, approvals, conversations and
memory. The dashboard applies its bearer-token auth as an include
dependency; tests mount the router on a bare FastAPI.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from . import brain, store

router = APIRouter()


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": msg})


@router.get("/api/health")
def health() -> dict:
    from . import db
    db._conn()
    return {"ok": True, "service": "dots", "version": "0.1.0"}


# ── dots ─────────────────────────────────────────────────────────────────────

@router.get("/api/dots")
def api_list_dots() -> list[dict]:
    return store.list_dots()


@router.post("/api/dots", status_code=201)
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


@router.get("/api/dots/{dot_id}")
def api_get_dot(dot_id: int) -> dict:
    d = store.get_dot(dot_id)
    if d is None:
        raise HTTPException(404, f"no dot #{dot_id}")
    return d


@router.patch("/api/dots/{dot_id}")
def api_update_dot(dot_id: int, payload: dict = Body(...)) -> dict:
    try:
        return store.update_dot(dot_id, name=payload.get("name"),
                                role=payload.get("role"),
                                permissions=payload.get("permissions"))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/dots/{dot_id}")
def api_delete_dot(dot_id: int) -> dict:
    if not store.delete_dot(dot_id):
        raise HTTPException(404, f"no dot #{dot_id}")
    return {"ok": True}


# ── spaces ───────────────────────────────────────────────────────────────────

@router.get("/api/spaces")
def api_list_spaces() -> list[dict]:
    return store.list_spaces()


@router.post("/api/spaces", status_code=201)
def api_create_space(payload: dict = Body(...)) -> dict:
    try:
        return store.create_space(payload.get("name"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/spaces/{space_id}/pages")
def api_list_pages(space_id: int) -> list[dict]:
    if store.get_space(space_id) is None:
        raise HTTPException(404, f"no space #{space_id}")
    return store.list_pages(space_id)


@router.post("/api/spaces/{space_id}/pages", status_code=201)
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

@router.get("/api/pages/{page_id}")
def api_get_page(page_id: int) -> dict:
    p = store.get_page(page_id)
    if p is None:
        raise HTTPException(404, f"no page #{page_id}")
    return p


@router.patch("/api/pages/{page_id}")
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


@router.get("/api/pages/{page_id}/revisions")
def api_revisions(page_id: int) -> list[dict]:
    if store.get_page(page_id) is None:
        raise HTTPException(404, f"no page #{page_id}")
    return store.list_revisions(page_id)


@router.get("/api/pages/{page_id}/revisions/{rev}")
def api_revision(page_id: int, rev: int) -> dict:
    r = store.get_revision(page_id, rev)
    if r is None:
        raise HTTPException(404, f"no rev {rev} on page #{page_id}")
    return r


# ── conversations: per-dot and per-page (separate by construction) ──────────

@router.get("/api/conversations/{convo_key:path}/messages")
def api_list_messages(convo_key: str, limit: int = 50) -> list[dict]:
    return store.list_messages(convo_key, limit=limit)


@router.post("/api/conversations/{convo_key:path}/messages", status_code=201)
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

@router.get("/api/pending")
def api_pending(status: str = "pending") -> list[dict]:
    return store.list_pending(status)


@router.post("/api/pending/{pid}/approve")
def api_approve(pid: int) -> dict:
    out = store.approve_pending(pid)
    if "error" in out:
        raise HTTPException(400 if "no proposal" in out["error"] else 409,
                            out["error"])
    if out.get("conflict"):
        return JSONResponse(status_code=409, content=out)
    return out


@router.post("/api/pending/{pid}/decline")
def api_decline(pid: int) -> dict:
    out = store.decline_pending(pid)
    if "error" in out:
        raise HTTPException(409, out["error"])
    return out


# ── memory (preferences) ─────────────────────────────────────────────────────

@router.get("/api/memory")
def api_memory() -> list[dict]:
    return store.list_prefs()


@router.put("/api/memory")
def api_memory_set(payload: dict = Body(...)) -> dict:
    try:
        return store.set_pref(payload.get("key"), payload.get("value"),
                              payload.get("allowed", "*"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/memory/{pref_id}")
def api_memory_delete(pref_id: int) -> dict:
    if not store.delete_pref(pref_id):
        raise HTTPException(404, f"no preference #{pref_id}")
    return {"ok": True}


# ── computers (per-Dot isolated machine — owner/takeover surface) ────────────

@router.get("/api/computers")
def api_list_computers(dot_id: int | None = None) -> list[dict]:
    from . import computer
    return computer.list_computers(dot_id)


@router.post("/api/computers", status_code=201)
def api_create_computer(payload: dict = Body(...)) -> dict:
    from . import computer
    dot_id = payload.get("dot_id")
    if dot_id is None:
        raise HTTPException(400, "dot_id required")
    try:
        return computer.create_for_dot(int(dot_id),
                                       payload.get("perms"))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/computers/{cid}")
def api_get_computer(cid: int) -> dict:
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    return comp


@router.post("/api/computers/{cid}/start")
def api_start_computer(cid: int) -> dict:
    from . import computer
    try:
        return computer.start(cid, actor="owner")
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/api/computers/{cid}/stop")
def api_stop_computer(cid: int) -> dict:
    from . import computer
    try:
        return computer.stop(cid, actor="owner")
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.patch("/api/computers/{cid}/perms")
def api_computer_perms(cid: int, payload: dict = Body(...)) -> dict:
    from . import computer
    try:
        return computer.set_perms(cid, payload.get("perms") or payload)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/computers/{cid}")
def api_delete_computer(cid: int) -> dict:
    from . import computer
    if not computer.delete(cid, actor="owner"):
        raise HTTPException(404, f"no computer #{cid}")
    return {"ok": True}


@router.get("/api/computers/{cid}/audit")
def api_computer_audit(cid: int, limit: int = 100) -> list[dict]:
    from . import computer
    if computer.get(cid) is None:
        raise HTTPException(404, f"no computer #{cid}")
    return computer.audit_rows(cid, limit)


def _op_result(result) -> dict:
    if isinstance(result, dict) and "error" in result:
        status = 403 if str(result["error"]).startswith("denied:") else 400
        return _err(status, result["error"])
    return result


@router.post("/api/computers/{cid}/exec")
def api_computer_exec(cid: int, payload: dict = Body(...)) -> dict:
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    argv = payload.get("argv")
    if not isinstance(argv, list):
        return _err(400, "argv must be a JSON list of strings "
                         "(no shell string, ever)")
    return _op_result(computer.shell(comp, argv, payload.get("timeout"),
                                     actor="owner"))


@router.post("/api/computers/{cid}/files/list")
def api_computer_files_list(cid: int, payload: dict = Body(default={})) -> dict:
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    return _op_result(computer.files_list(
        comp, str((payload or {}).get("path") or ""), actor="owner"))


@router.post("/api/computers/{cid}/files/read")
def api_computer_files_read(cid: int, payload: dict = Body(...)) -> dict:
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    path = payload.get("path")
    if not path:
        return _err(400, "path required")
    return _op_result(computer.files_read(comp, str(path), actor="owner"))


@router.post("/api/computers/{cid}/files/write")
def api_computer_files_write(cid: int, payload: dict = Body(...)) -> dict:
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    path = payload.get("path")
    if not path:
        return _err(400, "path required")
    return _op_result(computer.files_write(
        comp, str(path), payload.get("content"),
        append=bool(payload.get("append")), actor="owner"))


_BROWSER_OPS = {"navigate", "read", "snapshot", "screenshot", "click",
                "type", "key", "scroll"}


@router.post("/api/computers/{cid}/browser")
def api_computer_browser(cid: int, payload: dict = Body(...)) -> dict:
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    op = str(payload.get("op") or "").strip()
    if op not in _BROWSER_OPS:
        return _err(400, f"op must be one of {sorted(_BROWSER_OPS)}")
    kw = {k: v for k, v in payload.items() if k != "op"}
    return _op_result(computer.browser(comp, op, actor="owner", **kw))


# ── tasks (recurring background instructions) ───────────────────────────────

@router.get("/api/tasks")
def api_list_tasks() -> list[dict]:
    from . import scheduler
    scheduler.ensure_started()          # opening the list resumes ticks
    return store.list_tasks()


@router.post("/api/tasks", status_code=201)
def api_create_task(payload: dict = Body(...)) -> dict:
    from . import scheduler
    scheduler.ensure_started()
    try:
        raw_dot = payload.get("dot_id")
        return store.create_task(payload.get("name"),
                                 payload.get("instruction"),
                                 payload.get("every_seconds", 3600),
                                 None if raw_dot is None else int(raw_dot),
                                 cron=payload.get("cron") or None,
                                 run_at=payload.get("run_at"))
    except (ValueError, KeyError, TypeError) as e:
        status = 404 if isinstance(e, KeyError) else 400
        raise HTTPException(status, str(e))


@router.get("/api/tasks/{tid}")
def api_get_task(tid: int) -> dict:
    t = store.get_task(tid)
    if t is None:
        raise HTTPException(404, f"no task #{tid}")
    return t


@router.post("/api/tasks/{tid}/pause")
def api_pause_task(tid: int) -> dict:
    from . import scheduler
    try:
        return scheduler.pause(tid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/tasks/{tid}/resume")
def api_resume_task(tid: int) -> dict:
    from . import scheduler
    try:
        return scheduler.resume(tid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/tasks/{tid}/cancel")
def api_cancel_task(tid: int) -> dict:
    from . import scheduler
    try:
        return scheduler.cancel(tid)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/api/tasks/{tid}/retry")
def api_retry_task(tid: int) -> dict:
    from . import scheduler
    scheduler.ensure_started()
    try:
        return scheduler.retry(tid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/tasks/{tid}/runs")
def api_task_runs(tid: int, limit: int = 20) -> list[dict]:
    if store.get_task(tid) is None:
        raise HTTPException(404, f"no task #{tid}")
    return store.list_runs(tid, limit)


# ── skills (miner → draft → OWNER publishes) ────────────────────────────────

@router.get("/api/skills")
def api_list_skills(status: str | None = None) -> list[dict]:
    return store.list_skills(status if status not in ("", "any")
                             else None)


@router.post("/api/skills/mine")
def api_mine_skills() -> dict:
    from . import learning
    drafts = learning.mine()
    return {"created": len(drafts),
            "drafts": [{"id": d["id"], "title": d["title"]}
                       for d in drafts]}


@router.post("/api/skills/{sid}/publish")
def api_publish_skill(sid: int) -> dict:
    try:
        return store.publish_skill(sid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/api/skills/{sid}/archive")
def api_archive_skill(sid: int) -> dict:
    try:
        return store.archive_skill(sid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


# ── Slack events (mention → Dot thread, allowlists — T10) ────────────────────

@router.post("/api/slack/events")
def api_slack_events(payload: dict = Body(...)) -> dict:
    """Slack events API endpoint: handshake echoes the challenge;
    everything else is gated by allowlists FIRST — a rejected event is
    acknowledged (200) and NOTHING executes (T10)."""
    from . import slack
    if isinstance(payload, dict) and payload.get("type") == \
            "url_verification":
        return {"challenge": payload.get("challenge", "")}
    # fast path for rejections happens inside; allowed work may take a
    # brain round-trip → background so Slack always gets its 200 fast
    return slack.handle_event(payload, background=True)


# ── voice calls (phase 1 core: session, captions, receipt) ───────────────────

@router.get("/api/calls")
def api_list_calls(status: str | None = None) -> list[dict]:
    from . import voice
    return voice.list_calls(status if status not in ("", "any")
                            else None)


@router.post("/api/calls", status_code=201)
def api_start_call(payload: dict = Body(...)) -> dict:
    from . import voice
    dot_id = payload.get("dot_id")
    if dot_id is None:
        raise HTTPException(400, "dot_id required")
    try:
        return voice.start(int(dot_id), payload.get("page_id"))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/api/calls/{cid}")
def api_get_call(cid: int) -> dict:
    from . import voice
    try:
        return voice.get(cid)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/api/calls/{cid}/captions")
def api_caption(cid: int, payload: dict = Body(...)) -> dict:
    from . import voice
    res = voice.caption(cid, payload.get("speaker", "user"),
                        payload.get("text"),
                        generate_reply=bool(
                            payload.get("generate_reply", True)))
    if "error" in res:
        status = 404 if "no call" in res["error"] else 400
        return _err(status, res["error"])
    return res


@router.post("/api/calls/{cid}/end")
def api_end_call(cid: int) -> dict:
    from . import voice
    try:
        return voice.end(cid)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.get("/api/calls/{cid}/transcript")
def api_call_transcript(cid: int, format: str = "json"):
    """Receipt: JSON list, or a downloadable text transcript."""
    from fastapi.responses import PlainTextResponse
    from . import voice
    if format not in ("json", "text"):
        return _err(400, "format must be json or text")
    try:
        data = voice.transcript(cid, format=format)
    except KeyError as e:
        raise HTTPException(404, str(e))
    if format == "text":
        return PlainTextResponse(
            data, headers={
                "Content-Disposition":
                    f"attachment; filename=call-{cid}-transcript.txt"})
    return data


@router.post("/api/calls/{cid}/background")
def api_call_background(cid: int, payload: dict = Body(...)) -> dict:
    from . import voice
    try:
        return voice.background(cid, payload.get("instruction"),
                                int(payload.get("every_seconds", 120)))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))


@router.get("/api/computers/{cid}/image")
def api_computer_image(cid: int, path: str = "") -> FileResponse:
    """Screenshot/image INSIDE the computer's work dir — served for the
    dashboard <img> tag (auth via header or ?token=, jailed path)."""
    from . import computer
    comp = computer.get(cid)
    if comp is None:
        raise HTTPException(404, f"no computer #{cid}")
    work = computer.dirs(cid)[0]
    try:
        target = computer._jail(path, work)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not target.is_file():
        raise HTTPException(404, f"no image {path!r}")
    media = {"png": "image/png", "jpg": "image/jpeg",
             "jpeg": "image/jpeg", "gif": "image/gif",
             "webp": "image/webp"}.get(
                 target.suffix.lower().lstrip("."), "application/octet-stream")
    return FileResponse(str(target), media_type=media)
