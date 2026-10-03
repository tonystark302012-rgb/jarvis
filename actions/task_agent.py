"""
task_agent — multi-step goals through the agentic orchestrator.

The model (or the user) states a goal; the planner (Gemini) turns it into an
ordered list of tool calls; core.orchestrator runs them one by one with the
session's own action registry, logging every step to Mission Control.

Safety: destructive steps are refused by the orchestrator unless the user
explicitly confirmed (allow_destructive only ever comes from a confirm flow).
The runner is injected by main.py — this file never imports main, so it stays
testable.
"""
from __future__ import annotations

from typing import Callable

from core import orchestrator

# Injected by main.py once the action registry exists: fn(tool, args) -> str.
_runner: Callable[[str, dict], str] | None = None


def set_runner(fn: Callable[[str, dict], str] | None) -> None:
    global _runner
    _runner = fn


def _plan_with_llm(goal: str, tool_names: list[str]) -> list[orchestrator.Step]:
    """Ask Gemini for the plan. Returns [] when no key/model — the caller
    reports that cleanly rather than guessing a plan itself."""
    from core import gemini
    reply = gemini.call(
        [orchestrator.planner_prompt(goal, tool_names)],
        tier=gemini.FAST, timeout_ms=20_000,
    )
    if reply is None or not getattr(reply, "text", None):
        return []
    return orchestrator.parse_plan(reply.text, set(tool_names))


def _plan_json(steps) -> list[dict]:
    return [{"tool": s.tool, "args": dict(s.args or {}),
             "why": getattr(s, "why", "") or ""} for s in steps]


def _results_json(report) -> list[dict]:
    out = []
    for i, sr in enumerate(report.steps):
        out.append({"index": i, "tool": sr.step.tool, "ok": bool(sr.ok),
                    "result": str(sr.result or "")[:500]})
    return out


def _history(params: dict) -> str:
    """List past runs (sqlite — survives restarts)."""
    from core import taskstore
    try:
        runs = taskstore.list_runs(int(params.get("limit", 8) or 8))
    except Exception as e:
        return f"Task history unavailable: {e}"
    if not runs:
        return "No task runs recorded yet."
    lines = []
    for r in runs:
        pend = len(taskstore._pending_indices(r))
        mark = {"done": "✓", "partial": "◐", "failed": "✗",
                "running": "…"}.get(r["status"], "?")
        extra = f", {pend} step(s) left" if pend and \
            r["status"] in ("partial", "failed") else ""
        lines.append(f"#{r['id']} {mark} {r['goal'][:60]} "
                     f"({len(r['plan'])} steps{extra})")
    return "Task runs:\n" + "\n".join(lines) + \
        "\nSay 'resume task N' to continue one."


def _resume(params: dict, player, session_memory) -> str:
    """Re-run only the steps of a partial/failed run that didn't succeed.
    Completed steps are NEVER re-executed (idempotent resume)."""
    from core import taskstore
    from core import orchestrator
    if _runner is None:
        return "Task agent is not wired to the action registry (running outside the app)."
    try:
        run_id = int(params.get("id") or 0)
    except (TypeError, ValueError):
        run_id = 0
    run = taskstore.get(run_id) if run_id else None
    if run is None:
        candidates = taskstore.resumable()
        if not candidates:
            return ("Nothing to resume — no partial or failed runs. "
                    "'task history' shows past runs.")
        run = candidates[0]
    if run["status"] == "done":
        return f"Task #{run['id']} already finished — nothing to resume."
    pending = taskstore._pending_indices(run)
    if not pending:
        return f"Task #{run['id']} has no pending steps."
    plan = run["plan"]
    steps = [orchestrator.Step(plan[i]["tool"], dict(plan[i].get("args") or {}),
                               why=plan[i].get("why", ""))
             for i in pending]
    # resume may only call tools the session actually has
    tool_names = set(_runner_names or [])
    bad = [s.tool for s in steps if s.tool not in tool_names]
    if bad:
        return (f"Can't resume — these tools no longer exist: "
                f"{', '.join(sorted(set(bad)))}.")
    taskstore.mark_running(run["id"])
    allow = bool(params.get("allow_destructive", False))
    report = orchestrator.run_task(
        run["goal"], steps, _runner,
        allow_destructive=allow, stop_on_error=True, max_retries=0,
    )
    # merge: old successes + this attempt, positions mapped back to plan
    merged = {r.get("index"): r for r in (run.get("results") or [])}
    for j, sr in enumerate(report.steps):
        merged[pending[j]] = {"index": pending[j], "tool": sr.step.tool,
                              "ok": bool(sr.ok),
                              "result": str(sr.result or "")[:500]}
    results = [merged[k] for k in sorted(merged)]
    if report.ok:
        status = "done"
    elif any(r["ok"] for r in results):
        status = "partial"
    else:
        status = "failed"
    try:
        taskstore.update(run["id"], status, results)
    except Exception as e:
        print(f"[TaskAgent] store update failed: {e}")
    text = (f"Resumed task #{run['id']} — {run['goal'][:60]}\n"
            + report.text())
    return text


def task_agent(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()
    if action in ("history", "list", "runs"):
        return _history(params)
    if action == "resume":
        return _resume(params, player, session_memory)

    goal = str(params.get("description", "")).strip()
    if not goal:
        return ("No goal given. Say what should be achieved, e.g. "
                "'clean my downloads folder and tell me what you moved'.")

    if _runner is None:
        return "Task agent is not wired to the action registry (running outside the app)."

    try:
        max_steps = max(1, min(8, int(params.get("max_steps", 6))))
    except (TypeError, ValueError):
        max_steps = 6

    # The agent may only call tools the session actually has.
    # _runner_names is injected alongside _runner by main.py.
    tool_names = [n for n in (_runner_names or []) if n != "task_agent"]
    if not tool_names:
        return "No tools available to the task agent."

    ev_note = f"planning: {goal[:80]}"
    from core import activity
    activity.note("task", ev_note)

    steps = _plan_with_llm(goal, tool_names)
    if not steps:
        return ("I could not build a plan for that right now (no planner "
                "answer, or the plan contained no usable tools). "
                "Try describing it as a single concrete action.")
    steps = steps[:max_steps]

    # Persist BEFORE executing: a crash mid-run leaves a `partial` with
    # completed steps recorded — that's what makes resume possible.
    run_id = None
    try:
        from core import taskstore
        run_id = taskstore.create(goal, _plan_json(steps))
    except Exception as e:
        print(f"[TaskAgent] store create failed: {e}")

    if player is not None:
        try:
            player.show_content(
                "TASK PLAN — " + goal[:40],
                "\n".join(f"{i+1}. {s.tool} — {s.why or s.args}"
                          for i, s in enumerate(steps)),
            )
        except Exception:
            pass

    allow = bool(params.get("allow_destructive", False))
    report = orchestrator.run_task(
        goal, steps, _runner,
        allow_destructive=allow,
        stop_on_error=True,
        max_retries=0,
    )
    if run_id is not None:
        try:
            from core import taskstore
            if report.ok:
                status = "done"
            elif any(sr.ok for sr in report.steps):
                status = "partial"
            else:
                status = "failed"
            taskstore.update(run_id, status, _results_json(report))
        except Exception as e:
            print(f"[TaskAgent] store update failed: {e}")
    text = report.text()
    if run_id is not None and not report.ok:
        text += f"\n(Persisted as task #{run_id} — say 'resume task " \
                f"{run_id}' to continue from where it stopped.)"
    if player is not None:
        try:
            player.show_content("TASK REPORT — " + goal[:40], text)
        except Exception:
            pass
    return text


# Names of tools the runner can dispatch — injected by main.py.
_runner_names: list[str] = []


def set_runner_names(names) -> None:
    global _runner_names
    _runner_names = list(names or [])


TOOL = {
    "name": "task_agent",
    "description": (
        "Executes a multi-step goal as an agentic task: plans ordered tool "
        "calls, runs them one by one, and reports each step. Use when the "
        "user asks for something that clearly needs SEVERAL tools in "
        "sequence (e.g. 'organize my downloads and free up space'). For a "
        "single tool call, call that tool directly instead. Destructive "
        "steps are refused unless the user confirmed."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "description": {
                "type": "STRING",
                "description": "The goal to achieve, concrete and tool-sized.",
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Max plan steps 1-8. Default 6.",
            },
            "allow_destructive": {
                "type": "BOOLEAN",
                "description": "Only true after the user explicitly confirmed destructive actions.",
            },
            "action": {
                "type": "STRING",
                "description": "run (default) | history (past runs) | resume "
                               "(continue a partial run — optional id=).",
            },
            "id": {
                "type": "INTEGER",
                "description": "Run id for resume — default: latest partial.",
            },
        },
        "required": ["description"],
    },
    "handler": task_agent,
}
