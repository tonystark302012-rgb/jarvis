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


def task_agent(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
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
    text = report.text()
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
        },
        "required": ["description"],
    },
    "handler": task_agent,
}
