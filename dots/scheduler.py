# dots/scheduler.py
"""Recurring background instructions for Dots (doc §3.6).

A daemon ticker wakes every TICK_SECONDS and runs every `active` task
whose `next_run_at <= now`:

  * each run = the task's Dot brain on convo `task:<id>` (history
    accumulates), wrapped in a worker thread with a HARD
    DEADLINE_SECONDS (90 s) deadline → one `task_runs` row
    (ok | timeout | failed | cancelled);
  * scheduling never thundering-catches-up: `next_run_at += every`
    by whole multiples (missed windows are skipped), and it only
    advances for ok/failed/timeout — a cancelled run never reschedules;
  * `pause` stops future scheduling WITHOUT losing state (next_run_at
    stays, resume just flips status back); `cancel` flips status AND
    sets the live worker's cooperative abort flag — the run finishes
    as `cancelled`, its result discarded;
  * `retry` re-runs the last failed/timeout run immediately (owner
    only, honest refusals otherwise).

The ticker is lazily started (`ensure_started`) by the task endpoints
and the voice surface — never by module import, so tests control it.
"""
from __future__ import annotations

import threading
import time

from . import brain, store

TICK_SECONDS = 5
DEADLINE_SECONDS = 90          # spec hard cap per run
_SLICE = 0.25                  # abort-check granularity

_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()
_live: dict[int, dict] = {}    # task id -> {"abort": Event}


# ── lifecycle ────────────────────────────────────────────────────────────────
def ensure_started() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True,
                                   name="dots-scheduler")
        _thread.start()


def shutdown(timeout: float = 3.0) -> None:
    global _thread
    _stop.set()
    with _lock:
        th, _thread = _thread, None
    if th is not None and th.is_alive():
        th.join(timeout=timeout)


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def _loop() -> None:
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:            # never kill the ticker
            print(f"[Dots/scheduler] tick error: {e}")
        _stop.wait(TICK_SECONDS)          # reads the module global each


def tick(now: float | None = None) -> int:
    """Run everything due right now (spawn workers). → spawned count."""
    now = time.time() if now is None else now
    spawned = 0
    for task in store.due_tasks(now):
        if task["id"] in _live:
            continue                      # previous run still going
        _spawn(task, trigger="tick")
        spawned += 1
    return spawned


# ── run one task ─────────────────────────────────────────────────────────────
def _spawn(task: dict, trigger: str) -> None:
    ev = threading.Event()
    th = threading.Thread(target=_execute, args=(task, ev, trigger),
                          daemon=True, name=f"dot-task-{task['id']}")
    with _lock:
        _live[task["id"]] = {"abort": ev}
    th.start()


def _execute(task: dict, abort: threading.Event,
             trigger: str = "tick") -> None:
    tid = task["id"]
    started = time.time()
    _d = task.get("dot_id")
    dot = (store.get_dot(_d) if _d is not None else None) or {}
    try:
        # history first: the brain sees earlier runs on this convo
        try:
            store.add_message(f"task:{tid}", "user", task["instruction"])
        except Exception:
            pass
        holder: dict = {}

        def call() -> None:
            try:
                holder["text"] = brain.reply(
                    dot, f"task:{tid}", task["instruction"])
            except Exception as e:        # brain.reply never raises — belt
                holder["err"] = f"{type(e).__name__}: {e}"

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        deadline = started + DEADLINE_SECONDS
        while worker.is_alive() and time.time() < deadline:
            worker.join(_SLICE)
            if abort.is_set():
                break

        if abort.is_set() and worker.is_alive():
            status, output = "cancelled", \
                "(cancelled by owner — result discarded)"
        elif worker.is_alive():
            status, output = "timeout", \
                f"exceeded the {DEADLINE_SECONDS}s hard deadline — " \
                "result discarded"
        elif holder.get("err"):
            status, output = "failed", holder["err"]
        else:
            text = str(holder.get("text") or "")
            if any(text.startswith(p) for p in brain.HONEST_FAILURES):
                status, output = "failed", text
            else:
                status, output = "ok", text

        try:
            store.record_run(tid, status, started, output[:8000])
        except Exception as e:
            print(f"[Dots/scheduler] could not record run: {e}")
        if status in ("ok", "failed"):
            try:
                store.add_message(f"task:{tid}", "dot", output[:2000])
            except Exception:
                pass

        # reschedule only if the task is STILL active and the run was
        # not cancelled (pause keeps state; cancel stops forever)
        fresh = store.get_task(tid)
        if fresh is not None and fresh["status"] == "active" \
                and status != "cancelled":
            try:
                store.advance_next_run(tid, fresh["next_run_at"],
                                       time.time())
            except Exception as e:
                print(f"[Dots/scheduler] could not advance {tid}: {e}")
    finally:
        with _lock:
            _live.pop(tid, None)


# ── owner controls ───────────────────────────────────────────────────────────
def pause(tid: int) -> dict:
    t = store.get_task(tid)
    if t is None:
        raise KeyError(f"no task #{tid}")
    if t["status"] == "cancelled":
        raise ValueError(f"task #{tid} is cancelled — create a new one")
    return store.set_task_status(tid, "paused")


def resume(tid: int) -> dict:
    t = store.get_task(tid)
    if t is None:
        raise KeyError(f"no task #{tid}")
    if t["status"] == "cancelled":
        raise ValueError(f"task #{tid} is cancelled — create a new one")
    return store.set_task_status(tid, "active")


def cancel(tid: int) -> dict:
    t = store.get_task(tid)
    if t is None:
        raise KeyError(f"no task #{tid}")
    store.set_task_status(tid, "cancelled")
    with _lock:
        entry = _live.get(tid)
    aborted = False
    if entry is not None:
        entry["abort"].set()
        aborted = True
    out = store.get_task(tid)
    out["aborted_live_run"] = aborted
    return out


def retry(tid: int) -> dict:
    """Re-run the last failed/timeout run immediately (honest errors)."""
    t = store.get_task(tid)
    if t is None:
        raise KeyError(f"no task #{tid}")
    if t["status"] != "active":
        raise ValueError(f"task #{tid} is {t['status']} — resume it "
                         "before retrying")
    last = store.last_run(tid)
    if last is None:
        raise ValueError(f"task #{tid} has no runs yet")
    if last["status"] not in ("failed", "timeout"):
        raise ValueError(f"last run was {last['status']} — nothing to "
                         "retry (retry re-runs failures only)")
    with _lock:
        if tid in _live:
            raise ValueError(f"task #{tid} is already running")
    _spawn(t, trigger="retry")
    return {"ok": True, "task": t, "retried_run": last["id"]}
