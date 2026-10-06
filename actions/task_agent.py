"""
task_agent — multi-step goals through the agentic orchestrator.

The model (or the user) states a goal; the planner (Gemini) turns it into an
ordered list of tool calls; core.orchestrator runs them one by one with the
session's own action registry, logging every step to Mission Control.

AGENT BRAIN (this upgrade):
  * clarify    — an ambiguous goal comes back as ONE question instead of a
                 reckless plan (stateless: the model relays it and re-calls).
  * replan     — a failed chunk triggers bounded re-planning of ONLY the
                 remaining work (max_replans, default 2). Policy refusals
                 (destructive without allow) are NOT re-planned — a "no" is
                 a boundary, not a technical failure.
  * cancel     — cooperative cancellation at step boundaries via
                 core/agent_runtime; cancelled runs are resumable.
  * observe    — autonomy observe returns a PLAN PREVIEW, executes nothing.
  * chunked persistence — taskstore is updated after every chunk, so a
                 crash/restart loses at most the in-flight chunk.

Safety: destructive steps are refused by the orchestrator unless the user
explicitly confirmed (allow_destructive only ever comes from a confirm flow
or autonomy=auto). The runner is injected by main.py — this file never
imports main, so it stays testable.
"""
from __future__ import annotations

from typing import Callable

from core import orchestrator
from core.action_loader import RESULT_BLOCKED, classify_result

# Injected by main.py once the action registry exists: fn(tool, args) -> str.
_runner: Callable[[str, dict], str] | None = None

_MAX_REPLANS_DEFAULT = 2
_MAX_TOTAL_STEPS = 18          # hard ceiling across all chunks


def set_runner(fn: Callable[[str, dict], str] | None) -> None:
    global _runner
    _runner = fn


# ── planner / replanner ─────────────────────────────────────────────────

def _parse_clarify(text: str) -> str:
    """Returns the clarification question when the planner replies with
    {'clarify': '...'}, else ''."""
    if not text:
        return ""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```")[0]
    if '"clarify"' not in s and "'clarify'" not in s:
        return ""
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return ""
    try:
        import json
        obj = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return ""
    if isinstance(obj, dict) and obj.get("clarify"):
        return str(obj["clarify"])[:300]
    return ""


def _plan_with_llm(goal: str, tool_names: list[str]
                   ) -> tuple[list[orchestrator.Step], str]:
    """Ask Gemini for the plan. Returns (steps, clarify_question); both are
    empty when no key/model — the caller reports that cleanly rather than
    guessing a plan itself."""
    from core import gemini
    try:
        reply = gemini.call(
            [orchestrator.planner_prompt(goal, tool_names)],
            tier=gemini.FAST, timeout_ms=20_000,
        )
    except Exception:
        return [], ""
    if reply is None or not getattr(reply, "text", None):
        return [], ""
    clarify = _parse_clarify(reply.text)
    if clarify:
        return [], clarify
    return orchestrator.parse_plan(reply.text, set(tool_names)), ""


def _replan_llm(goal: str, done: list[dict], failed: orchestrator.Step,
                failed_result: str, tool_names: list[str]
                ) -> list[orchestrator.Step]:
    """Bounded re-plan of the remaining work. Returns [] on any doubt —
    never loops, never guesses."""
    from core import gemini
    try:
        reply = gemini.call(
            [orchestrator.replan_prompt(goal, done, failed.tool,
                                        dict(failed.args or {}),
                                        failed_result, tool_names)],
            tier=gemini.FAST, timeout_ms=20_000,
        )
    except Exception:
        return []
    if reply is None or not getattr(reply, "text", None):
        return []
    return orchestrator.parse_plan(reply.text, set(tool_names))


# ── helpers ─────────────────────────────────────────────────────────────

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
                "running": "…", "cancelled": "⛔"}.get(r["status"], "?")
        extra = f", {pend} step(s) left" if pend and \
            r["status"] in ("partial", "failed", "cancelled") else ""
        lines.append(f"#{r['id']} {mark} {r['goal'][:60]} "
                     f"({len(r['plan'])} steps{extra})")
    return "Task runs:\n" + "\n".join(lines) + \
        "\nSay 'resume task N' to continue one."


def _cancel(params: dict) -> str:
    """Cooperative cancel: flags the active run; it stops at the next step
    boundary. Stale `running` rows (crashed process) flip to `cancelled`
    so they stay resumable instead of stuck."""
    from core import agent_runtime as rt
    from core import taskstore
    try:
        run_id = int(params.get("id") or 0)
    except (TypeError, ValueError):
        run_id = 0
    active = rt.active()
    if run_id:
        key = str(run_id)
        if key in active:
            if rt.cancel(key):
                return (f"Cancelling task #{run_id} — it will stop at the "
                        f"next step boundary.")
        # not in this process → maybe a stale/crashed row
        run = taskstore.get(run_id)
        if run and run["status"] == "running":
            try:
                taskstore.update(run_id, "cancelled", run.get("results") or [])
            except Exception as e:
                return f"Could not mark task #{run_id} cancelled: {e}"
            return f"Task #{run_id} was not running here — marked cancelled."
        if run:
            return f"Task #{run_id} is {run['status']} — nothing to cancel."
        return f"No task #{run_id}."
    if not active:
        return "No task is running right now — nothing to cancel."
    if len(active) > 1:
        return ("Multiple runs active (" + ", ".join(active) +
                ") — cancel a specific one with id=.")
    if rt.cancel(active[0]):
        return (f"Cancelling task #{active[0]} — it will stop at the next "
                f"step boundary.")
    return "Nothing to cancel."


def _resume(params: dict, player, session_memory) -> str:
    """Re-run only the steps of a partial/failed/cancelled run that didn't
    succeed. Completed steps are NEVER re-executed (idempotent resume)."""
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
    from core import agent_runtime as rt
    rt.begin(str(run["id"]))
    try:
        report = orchestrator.run_task(
            run["goal"], steps, _runner,
            allow_destructive=allow, stop_on_error=True, max_retries=0,
            should_stop=lambda: rt.is_cancelled(str(run["id"])),
        )
    finally:
        rt.finish(str(run["id"]))
    # merge: old successes + this attempt, positions mapped back to plan
    merged = {r.get("index"): r for r in (run.get("results") or [])}
    for j, sr in enumerate(report.steps):
        merged[pending[j]] = {"index": pending[j], "tool": sr.step.tool,
                              "ok": bool(sr.ok),
                              "result": str(sr.result or "")[:500]}
    results = [merged[k] for k in sorted(merged)]
    if report.cancelled:
        status = "cancelled"
    elif report.ok:
        status = "done"
    elif any(r["ok"] for r in results):
        status = "partial"
    else:
        status = "failed"
    try:
        taskstore.update(run["id"], status, results)
    except Exception as e:
        print(f"[TaskAgent] store update failed: {e}")
    head = (f"CANCELLED task #{run['id']} — {run['goal'][:60]}"
            if report.cancelled else
            f"Resumed task #{run['id']} — {run['goal'][:60]}")
    return head + "\n" + report.text()


# ── the run loop (plan → chunks → replan → report) ──────────────────────

def _execute_goal(goal: str, steps: list[orchestrator.Step],
                  params: dict, player) -> str:
    from core import agent_runtime as rt
    from core import taskstore

    try:
        max_replans = max(0, min(3, int(params.get(
            "max_replans", _MAX_REPLANS_DEFAULT))))
    except (TypeError, ValueError):
        max_replans = _MAX_REPLANS_DEFAULT
    allow = bool(params.get("allow_destructive", False))

    plan_rows = _plan_json(steps)
    run_id = None
    try:
        run_id = taskstore.create(goal, plan_rows)
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

    key = str(run_id) if run_id is not None else "task"
    rt.begin(key)
    merged: dict[int, dict] = {}
    replans = 0
    executed_total = 0
    last_report: orchestrator.TaskReport | None = None
    pending: list[orchestrator.Step] = list(steps)
    notes: list[str] = []
    try:
        while pending:
            if executed_total >= _MAX_TOTAL_STEPS:
                notes.append("(step budget exhausted)")
                break
            pending = pending[:max(1, _MAX_TOTAL_STEPS - executed_total)]
            # pending is always the TAIL of plan_rows → absolute offset:
            offset = len(plan_rows) - len(pending)
            report = orchestrator.run_task(
                goal, pending, _runner,
                allow_destructive=allow, stop_on_error=True, max_retries=0,
                should_stop=lambda: rt.is_cancelled(key),
            )
            last_report = report
            executed_total += len(report.steps)
            for j, sr in enumerate(report.steps):
                merged[offset + j] = {"index": offset + j,
                                      "tool": sr.step.tool, "ok": bool(sr.ok),
                                      "result": str(sr.result or "")[:500]}
            if run_id is not None:
                _persist(run_id, "running", merged)

            if report.cancelled:
                _persist(run_id, "cancelled", merged)
                return _with_notes(_final_text(
                    run_id, goal, merged, plan_rows, report, cancelled=True),
                    notes)
            if report.ok:
                _persist(run_id, "done", merged)
                return _with_notes(_final_text(
                    run_id, goal, merged, plan_rows, report, cancelled=False),
                    notes)
            if not report.steps:
                break

            # ── failure: policy refusal → STOP (a "no" is not a bug);
            #             tool error → bounded replan of remaining work ──
            first_fail = next(i for i, sr in enumerate(report.steps)
                              if not sr.ok)
            failed_sr = report.steps[first_fail]
            failed_text = str(failed_sr.result or "")
            # A "no" is not a bug. Replanning is for tools that failed; a
            # destructive step waiting on the user's confirmation, or a policy
            # layer (observe mode) refusing, will refuse the same way however
            # many times we re-plan it — so stop and report instead of burning
            # the replan budget.
            policy_no = (classify_result(failed_text) == RESULT_BLOCKED
                         or failed_text.startswith("refused:"))
            if policy_no or replans >= max_replans or _runner is None:
                break
            replan = _replan_llm(
                goal,
                [merged[k] for k in sorted(merged)],
                failed_sr.step,
                str(failed_sr.result or ""),
                [n for n in (_runner_names or []) if n != "task_agent"])
            if not replan:
                notes.append("(replan unavailable — stopped)")
                break
            still = pending[first_fail:]
            if _same_plan(replan, still):
                notes.append("(replan produced the same plan — stopped)")
                break
            # accepted: keep plan prefix + this chunk's OK prefix; drop the
            # failed step and its un-run tail (the new plan supersedes both,
            # having been told what already happened).
            replans += 1
            keep = offset + first_fail          # one past last kept row
            new_rows = _plan_json(replan)
            plan_rows = plan_rows[:keep] + new_rows
            merged = {k: v for k, v in merged.items() if k < keep}
            if run_id is not None:
                try:
                    taskstore.replace_plan(run_id, plan_rows)
                    _persist(run_id, "running", merged)
                except Exception as e:
                    print(f"[TaskAgent] replace_plan failed: {e}")
            notes.append(f"(replan #{replans} after {failed_sr.step.tool} "
                         f"failed — {len(replan)} new step(s))")
            pending = replan
    finally:
        rt.finish(key)

    status = "partial" if any(r.get("ok") for r in merged.values()) \
        else "failed"
    _persist(run_id, status, merged)
    text = _final_text(run_id, goal, merged, plan_rows, last_report,
                       cancelled=False) if last_report else "No steps ran."
    if notes:
        text += "\n" + "\n".join(notes)
    return text


def _same_plan(a: list[orchestrator.Step], b: list[orchestrator.Step]) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return all(x.tool == y.tool and dict(x.args or {}) == dict(y.args or {})
               for x, y in zip(a, b))


def _with_notes(text: str, notes: list[str]) -> str:
    if notes:
        text += "\n" + "\n".join(notes)
    return text


def _persist(run_id, status: str, merged: dict) -> None:
    if run_id is None:
        return
    try:
        from core import taskstore
        results = [merged[k] for k in sorted(merged)]
        taskstore.update(run_id, status, results)
    except Exception as e:
        print(f"[TaskAgent] store update failed: {e}")


def _final_text(run_id, goal: str, merged: dict, plan_rows: list[dict],
                report: orchestrator.TaskReport | None,
                cancelled: bool) -> str:
    if report is None:
        return "No steps ran."
    lines = [f"{'CANCELLED' if cancelled else 'Task'}: {goal}"]
    lines.append("")
    for i, r in enumerate((merged[k] for k in sorted(merged)), 1):
        mark = "\u2713" if r.get("ok") else "\u2717"
        tool = r.get("tool", "?")
        lines.append(f"{i}. {mark} {tool}")
        if not r.get("ok") and r.get("result"):
            lines.append(f"   \u21b3 {str(r['result'])[:200]}")
    ok_n = sum(1 for r in merged.values() if r.get("ok"))
    lines.append("")
    lines.append(f"{ok_n}/{len(merged)} steps completed"
                 + (" (stopped at first failure)"
                    if report.stopped_early and not cancelled else "") + ".")
    text = "\n".join(lines)
    if run_id is not None and not (report.ok and not cancelled):
        pend = len(plan_rows) - len(merged)
        if cancelled:
            text += (f"\n(Persisted as task #{run_id} — cancelled with "
                     f"{pend} step(s) left; say 'resume task {run_id}' to "
                     f"continue.)")
        else:
            text += (f"\n(Persisted as task #{run_id} — say 'resume task "
                     f"{run_id}' to continue from where it stopped.)")
    return text


# ── handler ─────────────────────────────────────────────────────────────

def task_agent(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()
    if action in ("history", "list", "runs"):
        return _history(params)
    if action == "cancel":
        return _cancel(params)
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

    from core import activity
    activity.note("task", f"planning: {goal[:80]}")

    result = _plan_with_llm(goal, tool_names)
    if isinstance(result, tuple) and len(result) == 2:
        steps, clarify = result
    else:                      # legacy seam fakes return a bare list
        steps, clarify = result, ""
    if clarify:
        return (f"[NEEDS CLARIFY] {clarify}\n\n"
                "Ask the user this ONE question in their own language, then "
                "call task_agent again with description = the original goal "
                "plus their answer. Do not plan anything yet.")
    if not steps:
        return ("I could not build a plan for that right now (no planner "
                "answer, or the plan contained no usable tools). "
                "Try describing it as a single concrete action.")
    steps = steps[:max_steps]

    # Observe mode: PLAN PREVIEW only — nothing is created, nothing runs.
    from core import autonomy
    if autonomy.get_mode() == "observe":
        lines = [f"OBSERVE mode — plan preview for: {goal}", ""]
        for i, st in enumerate(steps):
            kv = ", ".join(f"{k}={str(v)[:40]}"
                           for k, v in (st.args or {}).items())
            lines.append(f"{i + 1}. {st.tool}({kv}) — {st.why or ''}")
        lines.append("")
        lines.append("Nothing was executed (observe = read-only). Say "
                     "'autonomy ask' or 'autonomy auto' to run it.")
        return "\n".join(lines)

    return _execute_goal(goal, steps, params, player)


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
        "steps are refused unless the user confirmed. If the planner "
        "replies [NEEDS CLARIFY], ask the user that one question first. "
        "A failed step triggers bounded replanning of the remaining work. "
        "Actions: run (default) | history | resume (id=) | cancel (id=, "
        "default: the single active run) — cancelling takes effect at the "
        "next step boundary."
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
            "max_replans": {
                "type": "INTEGER",
                "description": "Bounded re-plans after failures 0-3. Default 2.",
            },
            "allow_destructive": {
                "type": "BOOLEAN",
                "description": "Only true after the user explicitly confirmed destructive actions.",
            },
            "action": {
                "type": "STRING",
                "description": "run (default) | history (past runs) | resume "
                               "(continue a partial run — optional id=) | "
                               "cancel (stop the active run — optional id=).",
            },
            "id": {
                "type": "INTEGER",
                "description": "Run id for resume/cancel — default: latest / the active one.",
            },
        },
        "required": ["description"],
    },
    "handler": task_agent,
}
