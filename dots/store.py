# dots/store.py
"""Typed data access for the platform. Every write goes through here so
the revision/approval invariants live in exactly one place.

Invariants (see docs/DOT_PLATFORM.md §3.2):
  * owner saves carry base_rev — stale saves conflict, never overwrite;
  * dot writes ONLY create pending_changes rows;
  * approve applies iff pending.base_rev == page.rev, else 'stale';
  * every applied save writes a page_revisions row.
"""
from __future__ import annotations

import json
import time

from . import db
from .blocks import blocks_to_md, md_to_blocks, validate_blocks


def _now() -> float:
    return time.time()


def _row(r) -> dict:
    return dict(r)


# ── dots ─────────────────────────────────────────────────────────────────────

def create_dot(name: str, role: str = "",
               permissions: dict | None = None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("Dot needs a name.")
    perms = permissions if isinstance(permissions, dict) else {}
    now = _now()
    with db._LOCK:
        c = db._conn()
        clash = c.execute("SELECT name FROM dots WHERE lower(name) = lower(?)",
                          (name,)).fetchone()
        if clash is not None:
            raise ValueError(f"A dot named {clash['name']!r} already exists.")
        cur = c.execute(
            "INSERT INTO dots (name, role_instructions, permissions_json,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (name, str(role or ""), json.dumps(perms), now, now))
        c.commit()
        dot_id = cur.lastrowid
    return get_dot(dot_id)


def get_dot(dot_id: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM dots WHERE id = ?",
                               (int(dot_id),)).fetchone()
    if r is None:
        return None
    d = _row(r)
    d["permissions"] = json.loads(d.pop("permissions_json") or "{}")
    return d


def find_dot_by_name(name: str) -> dict | None:
    with db._LOCK:
        r = db._conn().execute(
            "SELECT * FROM dots WHERE lower(name) = lower(?)",
            (str(name or "").strip(),)).fetchone()
    return get_dot(r["id"]) if r else None


def list_dots() -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT id FROM dots ORDER BY id").fetchall()
    return [get_dot(r["id"]) for r in rows]


def update_dot(dot_id: int, name=None, role=None,
               permissions=None) -> dict:
    d = get_dot(dot_id)
    if d is None:
        raise KeyError(f"no dot #{dot_id}")
    name = d["name"] if name is None else str(name).strip()
    if not name:
        raise ValueError("Dot needs a name.")
    role = d["role_instructions"] if role is None else str(role)
    perms = d["permissions"] if permissions is None else permissions
    if not isinstance(perms, dict):
        raise ValueError("permissions must be an object")
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE dots SET name=?, role_instructions=?,"
                  " permissions_json=?, updated_at=? WHERE id=?",
                  (name, role, json.dumps(perms), _now(), int(dot_id)))
        c.commit()
    return get_dot(dot_id)


def delete_dot(dot_id: int) -> bool:
    with db._LOCK:
        c = db._conn()
        cur = c.execute("DELETE FROM dots WHERE id = ?", (int(dot_id),))
        c.commit()
        return cur.rowcount > 0


# ── spaces ───────────────────────────────────────────────────────────────────

def create_space(name: str) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("Space needs a name.")
    with db._LOCK:
        c = db._conn()
        cur = c.execute("INSERT INTO spaces (name, created_at) VALUES (?, ?)",
                        (name, _now()))
        c.commit()
        sid = cur.lastrowid
    return {"id": sid, "name": name}


def list_spaces() -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM spaces ORDER BY id").fetchall()
    return [_row(r) for r in rows]


def get_space(space_id: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM spaces WHERE id = ?",
                               (int(space_id),)).fetchone()
    return _row(r) if r else None


# ── pages ────────────────────────────────────────────────────────────────────

def _normalize_content(content_json=None, content_md=None
                       ) -> tuple[str, str, str | None]:
    """→ (content_json_str, content_md, error). JSON canonical; md input
    is imported through the block parser."""
    if content_json is not None:
        blocks, err = validate_blocks(content_json)
        if err:
            return "", "", err
        js = json.dumps(blocks, ensure_ascii=False)
        return js, blocks_to_md(blocks), None
    if content_md is not None:
        blocks = md_to_blocks(str(content_md))
        js = json.dumps(blocks, ensure_ascii=False)
        return js, blocks_to_md(blocks), None
    return "[]", "", None


def _page_dict(r) -> dict:
    p = _row(r)
    try:
        p["blocks"] = json.loads(p.get("content_json") or "[]")
    except ValueError:
        p["blocks"] = []
    try:
        p["sources"] = json.loads(p.get("sources_json") or "[]")
    except ValueError:
        p["sources"] = []
    return p


def get_page(page_id: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM pages WHERE id = ?",
                               (int(page_id),)).fetchone()
    return _page_dict(r) if r else None


def list_pages(space_id: int) -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM pages WHERE space_id = ? ORDER BY id",
            (int(space_id),)).fetchall()
    return [_page_dict(r) for r in rows]


def _write_page(space_id, parent_id, title, content_json_str, content_md,
                author: str, rev: int, note: str, page_id: int | None,
                created_at: float | None = None,
                sources_json_str: str | None = None) -> dict:
    """Internal: apply a page write + revision row. Caller holds no lock —
    we take it here so rev increments are atomic. sources_json_str=None on
    an UPDATE keeps the page's existing sources (owner saves never clobber
    research links); on a CREATE it defaults to '[]'."""
    now = _now()
    with db._LOCK:
        c = db._conn()
        if page_id is None:
            cur = c.execute(
                "INSERT INTO pages (space_id, parent_id, title, content_json,"
                " content_md, rev, created_by, sources_json, created_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (space_id, parent_id, title, content_json_str, content_md,
                 rev, author, sources_json_str or "[]",
                 created_at or now, now))
            page_id = cur.lastrowid
        else:
            if sources_json_str is None:
                c.execute(
                    "UPDATE pages SET title=?, content_json=?, content_md=?,"
                    " rev=?, updated_at=? WHERE id=?",
                    (title, content_json_str, content_md, rev, now, page_id))
            else:
                c.execute(
                    "UPDATE pages SET title=?, content_json=?, content_md=?,"
                    " rev=?, sources_json=?, updated_at=? WHERE id=?",
                    (title, content_json_str, content_md, rev,
                     sources_json_str, now, page_id))
        c.execute(
            "INSERT INTO page_revisions (page_id, rev, title, content_json,"
            " content_md, author, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (page_id, rev, title, content_json_str, content_md, author,
             note, now))
        c.commit()
    return get_page(page_id)


def owner_create_page(space_id: int, title: str, content_json=None,
                      content_md=None, parent_id: int | None = None,
                      author: str = "owner",
                      sources=None) -> tuple[dict | None, str | None]:
    if get_space(space_id) is None:
        return None, f"no space #{space_id}"
    js, md, err = _normalize_content(content_json, content_md)
    if err:
        return None, err
    if parent_id is not None and get_page(parent_id) is None:
        return None, f"no parent page #{parent_id}"
    title = str(title or "Untitled").strip() or "Untitled"
    try:
        src = _norm_sources(sources)
    except ValueError as e:
        return None, str(e)
    return _write_page(int(space_id), parent_id, title, js, md,
                       author, 1, "created", None,
                       sources_json_str=src), None


def owner_save_page(page_id: int, base_rev: int, title=None,
                    content_json=None, content_md=None
                    ) -> tuple[dict | None, dict | None]:
    """→ (page, None) on success or (None, conflict_dict). The conflict
    path NEVER writes (T1/T12)."""
    page = get_page(page_id)
    if page is None:
        return None, {"error": f"no page #{page_id}"}
    try:
        base_rev = int(base_rev)
    except (TypeError, ValueError):
        return None, {"error": "base_rev must be an integer"}
    if base_rev != page["rev"]:
        return None, {"conflict": True, "current_rev": page["rev"],
                      "current_title": page["title"],
                      "current_content_json": page["content_json"],
                      "current_content_md": page["content_md"]}
    new_title = page["title"] if title is None else str(title).strip()
    if content_json is None and content_md is None:
        js, md = page["content_json"], page["content_md"]
    else:
        js, md, err = _normalize_content(content_json, content_md)
        if err:
            return None, {"error": err}
    applied = _write_page(page["space_id"], page["parent_id"], new_title,
                          js, md, "owner", page["rev"] + 1, "owner save",
                          page["id"])
    return applied, None


def list_revisions(page_id: int) -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT id, rev, title, author, note, created_at"
            " FROM page_revisions WHERE page_id = ? ORDER BY rev DESC",
            (int(page_id),)).fetchall()
    return [_row(r) for r in rows]


def get_revision(page_id: int, rev: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute(
            "SELECT * FROM page_revisions WHERE page_id = ? AND rev = ?",
            (int(page_id), int(rev))).fetchone()
    if r is None:
        return None
    p = _row(r)
    p["blocks"] = json.loads(p.get("content_json") or "[]")
    return p


# ── pending changes (human-in-the-loop) ──────────────────────────────────────

def _pending_dict(r) -> dict:
    p = _row(r)
    try:
        p["blocks"] = json.loads(p.get("content_json") or "[]")
    except ValueError:
        p["blocks"] = []
    raw = p.get("sources_json")
    if raw is None:
        p["sources"] = None          # "not provided" — keep page's own
    else:
        try:
            p["sources"] = json.loads(raw or "[]")
        except ValueError:
            p["sources"] = []
    return p


def _norm_sources(sources) -> str | None:
    """Normalise research links for storage → JSON str, or None when the
    caller did not provide sources (nullable = "leave as-is" on approve)."""
    if sources is None:
        return None
    if isinstance(sources, str):
        try:
            sources = json.loads(sources)
        except ValueError:
            sources = [sources]
    if not isinstance(sources, list):
        raise ValueError("sources must be a list of links")
    out = []
    for item in sources[:20]:
        if isinstance(item, str):
            u = item.strip()
            if u:
                out.append({"title": "", "url": u})
        elif isinstance(item, dict):
            u = str(item.get("url") or "").strip()
            if u:
                out.append({"title": str(item.get("title") or ""), "url": u})
    return json.dumps(out)


def propose_edit(dot_id: int, page_id: int, base_rev: int,
                 title=None, content_json=None, content_md=None,
                 reason: str = "", sources=None) -> dict:
    """A dot's edit_space_page — proposal ONLY (T2). No page bytes move."""
    page = get_page(page_id)
    if page is None:
        raise KeyError(f"no page #{page_id}")
    if get_dot(dot_id) is None:
        raise KeyError(f"no dot #{dot_id}")
    if content_json is None and content_md is None:
        js, md = page["content_json"], page["content_md"]
        title = page["title"] if title is None else str(title)
    else:
        js, md, err = _normalize_content(content_json, content_md)
        if err:
            raise ValueError(err)
        title = page["title"] if title is None else str(title)
    now = _now()
    src = _norm_sources(sources)
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO pending_changes (kind, page_id, dot_id, base_rev,"
            " title, content_json, content_md, reason, sources_json,"
            " created_at) VALUES ('edit',?,?,?,?,?,?,?,?,?)",
            (int(page_id), int(dot_id), int(base_rev), title, js, md,
             str(reason or ""), src, now))
        c.commit()
        pid = cur.lastrowid
    return get_pending(pid)


def propose_create(dot_id: int, space_id: int, title: str,
                   content_json=None, content_md=None,
                   parent_id: int | None = None,
                   reason: str = "", sources=None) -> dict:
    """A dot's create_space_page — a create is a write too, so it is a
    proposal (decision Q3 in the doc)."""
    if get_space(space_id) is None:
        raise KeyError(f"no space #{space_id}")
    if get_dot(dot_id) is None:
        raise KeyError(f"no dot #{dot_id}")
    js, md, err = _normalize_content(content_json, content_md)
    if err:
        raise ValueError(err)
    now = _now()
    src = _norm_sources(sources)
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO pending_changes (kind, page_id, space_id, parent_id,"
            " dot_id, base_rev, title, content_json, content_md, reason,"
            " sources_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("create", None, int(space_id), parent_id, int(dot_id), 0,
             str(title or "Untitled").strip() or "Untitled", js, md,
             str(reason or ""), src, now))
        c.commit()
        pid = cur.lastrowid
    return get_pending(pid)


def get_pending(pid: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM pending_changes WHERE id = ?",
                               (int(pid),)).fetchone()
    return _pending_dict(r) if r else None


def list_pending(status: str = "pending") -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM pending_changes WHERE status = ?"
            " ORDER BY id DESC", (str(status),)).fetchall()
    return [_pending_dict(r) for r in rows]


def approve_pending(pid: int) -> dict:
    """Owner-side apply. Stale base_rev → status 'stale', nothing written
    (T3). Double-decide → honest refusal (T4/T5 family)."""
    p = get_pending(pid)
    if p is None:
        return {"error": f"no proposal #{pid}"}
    if p["status"] != "pending":
        return {"error": f"proposal #{pid} already {p['status']}"}
    now = _now()
    if p["kind"] == "create":
        page, err = owner_create_page(
            p["space_id"], p["title"], content_json=p["content_json"],
            parent_id=p["parent_id"],
            author=f"dot:{p['dot_id']}",
            sources=p.get("sources") or [])
        if err:
            return {"error": err}
        with db._LOCK:
            c = db._conn()
            c.execute("UPDATE pending_changes SET status='approved',"
                      " decided_at=? WHERE id=?", (now, int(pid)))
            c.commit()
        return {"ok": True, "status": "approved", "page": page}

    page = get_page(p["page_id"])
    if page is None:
        return {"error": f"page #{p['page_id']} vanished"}
    if p["base_rev"] != page["rev"]:
        with db._LOCK:
            c = db._conn()
            c.execute("UPDATE pending_changes SET status='stale',"
                      " decided_at=? WHERE id=?", (now, int(pid)))
            c.commit()
        return {"conflict": True, "status": "stale",
                "reason": (f"page moved to rev {page['rev']} — proposal "
                           f"was based on rev {p['base_rev']}; nothing "
                           f"was overwritten"),
                "current_rev": page["rev"]}
    applied = _write_page(page["space_id"], page["parent_id"], p["title"],
                          p["content_json"], p["content_md"],
                          f"dot:{p['dot_id']}", page["rev"] + 1,
                          p["reason"] or "approved proposal", page["id"],
                          sources_json_str=_norm_sources(p.get("sources")))
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE pending_changes SET status='approved',"
                  " decided_at=? WHERE id=?", (now, int(pid)))
        c.commit()
    return {"ok": True, "status": "approved", "page": applied}


def decline_pending(pid: int) -> dict:
    p = get_pending(pid)
    if p is None:
        return {"error": f"no proposal #{pid}"}
    if p["status"] != "pending":
        return {"error": f"proposal #{pid} already {p['status']}"}
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE pending_changes SET status='declined',"
                  " decided_at=? WHERE id=?", (_now(), int(pid)))
        c.commit()
    return {"ok": True, "status": "declined"}


# ── messages (conversations: per dot / per page / slack / call) ──────────────

def add_message(convo_key: str, role: str, content: str,
                meta: dict | None = None) -> dict:
    if not convo_key:
        raise ValueError("convo_key required")
    if role not in ("user", "dot", "system"):
        raise ValueError("role must be user | dot | system")
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO messages (convo_key, role, content, meta_json,"
            " created_at) VALUES (?,?,?,?,?)",
            (str(convo_key), role, str(content),
             json.dumps(meta or {}), _now()))
        c.commit()
        mid = cur.lastrowid
        r = c.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    return _row(r)


def list_messages(convo_key: str, limit: int = 50) -> list[dict]:
    limit = max(1, min(500, int(limit)))
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM messages WHERE convo_key = ?"
            " ORDER BY id DESC LIMIT ?", (str(convo_key), limit)).fetchall()
    out = [_row(r) for r in rows]
    out.reverse()
    for m in out:
        try:
            m["meta"] = json.loads(m.pop("meta_json") or "{}")
        except ValueError:
            m["meta"] = {}
    return out


# ── preferences (memory) ─────────────────────────────────────────────────────

def list_prefs() -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM preferences ORDER BY key").fetchall()
    out = []
    for r in rows:
        p = _row(r)
        try:
            p["allowed"] = json.loads(p.pop("allowed_json") or '"*"')
        except ValueError:
            p["allowed"] = "*"
        out.append(p)
    return out


def set_pref(key: str, value: str, allowed="*") -> dict:
    key = str(key or "").strip()
    if not key:
        raise ValueError("preference needs a key")
    if not (allowed == "*" or (isinstance(allowed, list) and
                               all(isinstance(a, str) for a in allowed))):
        raise ValueError("allowed must be '*' or a list of dot names")
    now = _now()
    with db._LOCK:
        c = db._conn()
        c.execute(
            "INSERT INTO preferences (key, value, allowed_json, created_at,"
            " updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " allowed_json=excluded.allowed_json, updated_at=excluded.updated_at",
            (key, str(value), json.dumps(allowed), now, now))
        c.commit()
    return get_pref(key)


def get_pref(key: str) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM preferences WHERE key = ?",
                               (str(key),)).fetchone()
    if r is None:
        return None
    p = _row(r)
    p["allowed"] = json.loads(p.pop("allowed_json") or '"*"')
    return p


def delete_pref(pref_id: int) -> bool:
    with db._LOCK:
        c = db._conn()
        cur = c.execute("DELETE FROM preferences WHERE id = ?",
                        (int(pref_id),))
        c.commit()
        return cur.rowcount > 0


def prefs_for_dot(dot: dict) -> list[dict]:
    """Preferences this dot is permitted to read (permissions.memory off →
    none; allowed list matches the dot's name, case-insensitive)."""
    if not isinstance(dot, dict) or not dot.get("permissions", {}).get(
            "memory", True):
        return []
    name = str(dot.get("name") or "").lower()
    out = []
    for p in list_prefs():
        allowed = p.get("allowed")
        if allowed == "*" or (isinstance(allowed, list) and
                              name in [str(a).lower() for a in allowed]):
            out.append(p)
    return out


# ── tasks (recurring instructions) + runs ───────────────────────────────────
def create_task(name: str, instruction: str, every_seconds,
                dot_id: int, next_run_at: float | None = None) -> dict:
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ValueError("task needs an instruction")
    try:
        every = int(every_seconds)
    except (TypeError, ValueError):
        raise ValueError("every_seconds must be an integer")
    if every < 1:
        raise ValueError("every_seconds must be >= 1")
    if get_dot(dot_id) is None:
        raise KeyError(f"no dot #{dot_id}")
    name = str(name or "").strip() or instruction[:40]
    now = _now()
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO tasks (name, instruction, every_seconds, dot_id,"
            " status, next_run_at, created_at) VALUES (?,?,?,?,'active',?,?)",
            (name, instruction, every, int(dot_id),
             float(next_run_at if next_run_at is not None else now), now))
        c.commit()
        tid = cur.lastrowid
    return get_task(tid)


def get_task(tid: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM tasks WHERE id = ?",
                               (int(tid),)).fetchone()
    return dict(r) if r else None


def list_tasks() -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM tasks ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def due_tasks(now: float) -> list[dict]:
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM tasks WHERE status = 'active'"
            " AND next_run_at <= ? ORDER BY next_run_at",
            (float(now),)).fetchall()
    return [dict(r) for r in rows]


def set_task_status(tid: int, status: str) -> dict:
    if status not in ("active", "paused", "cancelled"):
        raise ValueError(f"bad task status {status!r}")
    t = get_task(tid)
    if t is None:
        raise KeyError(f"no task #{tid}")
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE tasks SET status = ? WHERE id = ?",
                  (status, int(tid)))
        c.commit()
    return get_task(tid)


def advance_next_run(tid: int, base: float, finished_at: float) -> float:
    """next_run_at += every, skipping missed windows — NO thundering
    catch-up: jump by whole multiples of `every` until > now."""
    t = get_task(tid)
    if t is None:
        raise KeyError(f"no task #{tid}")
    every = int(t["every_seconds"])
    nxt = float(base) + every
    while nxt <= finished_at:
        nxt += every
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE tasks SET next_run_at = ?, last_run_at = ?"
                  " WHERE id = ?", (nxt, float(finished_at), int(tid)))
        c.commit()
    return nxt


def record_run(task_id: int, status: str, started_at: float,
               output: str = "") -> dict:
    if status not in ("ok", "timeout", "failed", "cancelled"):
        raise ValueError(f"bad run status {status!r}")
    now = _now()
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO task_runs (task_id, started_at, finished_at,"
            " status, output, created_at) VALUES (?,?,?,?,?,?)",
            (int(task_id), float(started_at), now, status,
             str(output or ""), now))
        c.commit()
        rid = cur.lastrowid
        r = c.execute("SELECT * FROM task_runs WHERE id = ?",
                      (rid,)).fetchone()
    return dict(r)


def list_runs(task_id: int, limit: int = 20) -> list[dict]:
    limit = max(1, min(200, int(limit)))
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM task_runs WHERE task_id = ?"
            " ORDER BY id DESC LIMIT ?", (int(task_id), limit)).fetchall()
    return [dict(r) for r in rows]


def last_run(task_id: int) -> dict | None:
    rows = list_runs(task_id, limit=1)
    return rows[0] if rows else None


# ── skills (draft → owner publish → published) ──────────────────────────────
def create_skill_draft(title: str, body_md: str,
                       source_note: str = "") -> dict:
    now = _now()
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO skills (title, body_md, source_note, status,"
            " created_at) VALUES (?,?,?,'draft',?)",
            (str(title or "Untitled skill").strip() or "Untitled skill",
             str(body_md or ""), str(source_note or ""), now))
        c.commit()
        sid = cur.lastrowid
    return get_skill(sid)


def get_skill(sid: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM skills WHERE id = ?",
                               (int(sid),)).fetchone()
    return dict(r) if r else None


def list_skills(status: str | None = None) -> list[dict]:
    with db._LOCK:
        if status is None:
            rows = db._conn().execute(
                "SELECT * FROM skills ORDER BY id DESC").fetchall()
        else:
            rows = db._conn().execute(
                "SELECT * FROM skills WHERE status = ? ORDER BY id DESC",
                (str(status),)).fetchall()
    return [dict(r) for r in rows]


def find_skill_by_fp(fp: str) -> dict | None:
    """Any skill (draft/published/archived) already mined from this
    shape → miner must not create a duplicate draft."""
    note = f"fp:{fp}"
    with db._LOCK:
        r = db._conn().execute(
            "SELECT * FROM skills WHERE source_note = ? OR"
            " source_note LIKE ? LIMIT 1",
            (note, note + " %")).fetchone()
    return dict(r) if r else None


def publish_skill(sid: int) -> dict:
    s = get_skill(sid)
    if s is None:
        raise KeyError(f"no skill #{sid}")
    if s["status"] == "published":
        raise ValueError(f"skill #{sid} is already published")
    now = _now()
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE skills SET status='published', published_at=?"
                  " WHERE id = ?", (now, int(sid)))
        c.commit()
    return get_skill(sid)


def archive_skill(sid: int) -> dict:
    s = get_skill(sid)
    if s is None:
        raise KeyError(f"no skill #{sid}")
    if s["status"] == "archived":
        raise ValueError(f"skill #{sid} is already archived")
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE skills SET status='archived' WHERE id = ?",
                  (int(sid),))
        c.commit()
    return get_skill(sid)


def scan_for_mining(convo_prefixes=("dot:", "page:", "task:")) -> list[dict]:
    """Ordered (id, convo_key, role, content) rows for the miner —
    only conversation messages, never system noise."""
    marks = tuple(str(p) for p in convo_prefixes)
    where = " OR ".join("convo_key LIKE ?" for _ in marks)
    params = tuple(p + "%" for p in marks)
    with db._LOCK:
        rows = db._conn().execute(
            f"SELECT id, convo_key, role, content FROM messages"
            f" WHERE {where} ORDER BY id", params).fetchall()
    return [dict(r) for r in rows]
