# core/taskstore.py
"""Task persistence — sqlite-backed run history + resume for the agent.

Roadmap: "Task persistence — sqlite (resume bhi karo)". Today
task_agent plans, runs, and forgets: kill the app mid-goal and the
plan (and every completed step) is gone. This store keeps, per run:

    id, goal, status (running|done|failed|partial), created/updated,
    the PLAN as JSON [{tool, args, why}], and per-step results as JSON.

Resume contract: a `partial`/`failed` run keeps its plan and the results
of steps that already succeeded; `resume()` re-runs only from the first
step that didn't succeed (destructive steps keep their refusal unless
explicitly allowed — same safety as the orchestrator).

sqlite = the roadmap's requirement; one file under memory/, WAL not
needed (single process, short transactions), busy_timeout so a tick
that overlaps a save never raises.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_CONN: sqlite3.Connection | None = None
MAX_RETAINED = 200          # oldest finished runs get pruned


def _db_path() -> Path:
    from config import get_base_dir
    d = get_base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "tasks.db"


def reset_for_tests() -> None:
    """Drop the cached connection (tests re-point _db_path first).
    Same seam as core.audit_chain.reset_for_tests."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        c = sqlite3.connect(_db_path(), check_same_thread=False,
                            timeout=10)
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            " id INTEGER PRIMARY KEY,"
            " goal TEXT NOT NULL,"
            " status TEXT NOT NULL,"          # running|done|failed|partial
            " plan_json TEXT NOT NULL,"       # [{"tool","args","why"}]
            " results_json TEXT NOT NULL,"    # [{"tool","ok","result"}]
            " created REAL NOT NULL,"
            " updated REAL NOT NULL)"
        )
        c.commit()
        _CONN = c
    return _CONN


def _row_to_run(row) -> dict:
    return {
        "id": row[0], "goal": row[1], "status": row[2],
        "plan": json.loads(row[3] or "[]"),
        "results": json.loads(row[4] or "[]"),
        "created": row[5], "updated": row[6],
    }


def create(goal: str, plan: list[dict]) -> int:
    """Register a run as `running` — call before executing steps."""
    now = time.time()
    with _LOCK:
        c = _conn()
        cur = c.execute(
            "INSERT INTO runs (goal, status, plan_json, results_json,"
            " created, updated) VALUES (?, 'running', ?, '[]', ?, ?)",
            (goal, json.dumps(plan, ensure_ascii=False), now, now))
        c.commit()
        return int(cur.lastrowid)


def update(run_id: int, status: str, results: list[dict]) -> None:
    """Finalize/refresh a run. results = one entry per EXECUTED step."""
    if status not in ("running", "done", "failed", "partial", "cancelled"):
        raise ValueError(f"bad status {status!r}")
    with _LOCK:
        c = _conn()
        c.execute(
            "UPDATE runs SET status = ?, results_json = ?, updated = ?"
            " WHERE id = ?",
            (status, json.dumps(results, ensure_ascii=False),
             time.time(), run_id))
        c.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs"
            " ORDER BY updated DESC LIMIT ?)", (MAX_RETAINED,))
        c.commit()


def get(run_id: int) -> dict | None:
    with _LOCK:
        row = _conn().execute(
            "SELECT id, goal, status, plan_json, results_json,"
            " created, updated FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _row_to_run(row) if row else None


def list_runs(limit: int = 10) -> list[dict]:
    with _LOCK:
        rows = _conn().execute(
            "SELECT id, goal, status, plan_json, results_json,"
            " created, updated FROM runs ORDER BY updated DESC LIMIT ?",
            (max(1, min(50, limit)),)).fetchall()
    return [_row_to_run(r) for r in rows]


def recent_episodes(limit: int = 150) -> list[dict]:
    """Runs to mine for lessons — same rows as list_runs, deeper pool.

    `list_runs` caps at 50 because it feeds the history view, which is for
    reading. Lesson matching wants to reach further back: a goal the user
    repeats every few weeks should still find its own history. Only the four
    fields the distillation reads are selected, so a deep pool stays cheap.
    """
    with _LOCK:
        rows = _conn().execute(
            "SELECT id, goal, status, plan_json, results_json,"
            " created, updated FROM runs"
            " WHERE status != 'running' ORDER BY updated DESC LIMIT ?",
            (max(1, min(1000, limit)),)).fetchall()
    return [_row_to_run(r) for r in rows]


def resumable() -> list[dict]:
    """Partial/failed runs whose plan still has unexecuted or failed steps."""
    with _LOCK:
        rows = _conn().execute(
            "SELECT id, goal, status, plan_json, results_json,"
            " created, updated FROM runs"
            " WHERE status IN ('partial', 'failed', 'cancelled')"
            " ORDER BY updated DESC LIMIT 20").fetchall()
    out = []
    for row in rows:
        run = _row_to_run(row)
        if _pending_indices(run):
            out.append(run)
    return out


def _pending_indices(run: dict) -> list[int]:
    """Plan indices that still need execution: everything after the last
    successful step, plus any step whose recorded result was a failure —
    recomputed from the plan vs results so a resumed run converges."""
    results = run.get("results") or []
    # map by plan position: results carry "index"
    done_ok = {r.get("index") for r in results if r.get("ok")}
    pending = [i for i in range(len(run.get("plan") or []))
               if i not in done_ok]
    return pending


def replace_plan(run_id: int, plan: list[dict]) -> None:
    """Atomically swap a run's plan (replan flow: the failed chunk's tail
    is superseded by the new plan; completed prefix stays)."""
    with _LOCK:
        c = _conn()
        c.execute(
            "UPDATE runs SET plan_json = ?, updated = ? WHERE id = ?",
            (json.dumps(plan, ensure_ascii=False), time.time(), run_id))
        c.commit()


def mark_running(run_id: int) -> None:
    with _LOCK:
        c = _conn()
        c.execute("UPDATE runs SET status = 'running', updated = ?"
                  " WHERE id = ?", (time.time(), run_id))
        c.commit()


def sweep_stale(max_age_hours: float = 24.0,
                now: float | None = None) -> list[dict]:
    """Boot recovery: a run still marked `running` whose last touch is
    older than `max_age_hours` cannot actually be running — the process
    died mid-goal. Flip those to `cancelled` (resumable by design) and
    return them so the caller can tell the user. Live runs are safe:
    every executed step refreshes `updated`."""
    now = time.time() if now is None else float(now)
    cutoff = now - max(60.0, float(max_age_hours) * 3600.0)
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT id, goal, status, plan_json, results_json,"
            " created, updated FROM runs"
            " WHERE status = 'running' AND updated < ?",
            (cutoff,)).fetchall()
        out = [_row_to_run(r) for r in rows]
        if out:
            c.execute(
                "UPDATE runs SET status = 'cancelled', updated = ?"
                " WHERE status = 'running' AND updated < ?",
                (now, cutoff))
            c.commit()
    return out


__all__ = ["create", "update", "get", "list_runs", "resumable",
           "mark_running", "replace_plan", "sweep_stale", "MAX_RETAINED"]
