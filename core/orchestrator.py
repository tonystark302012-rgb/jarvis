"""
Agentic task orchestrator — plan → execute → verify → report.

The model (or a human) states a goal; a planner returns an ordered list of
steps, each step is a tool call, and the runner executes them one at a time,
recording every step in core.activity so the Mission Control timeline shows
exactly what happened. Failures stop the run by default instead of ploughing
on through a broken plan.

Safety model (deliberately conservative — "agentic, but not wrong"):
  * Destructive tools are REFUSED unless allow_destructive=True. The caller
    (main.py) only passes that when the user explicitly asked for it.
  * Every step is logged with its args before it runs.
  * stop_on_error=True by default; a failing step yields a report, not a
    cascade.

The planner is injectable: production passes a Gemini-backed planner, tests
pass a fake. No network, no UI, no dashboard imports in here.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from core import activity

# Tools that change or destroy state beyond the ordinary. Opening a file is
# fine; deleting a drive is not. The list is consulted by run_task() and is
# intentionally broad — a false positive costs a "not allowed", a false
# negative costs the user's data.
DESTRUCTIVE = frozenset({
    "shutdown_jarvis", "procman",
})

# Parameters that make an otherwise-safe tool destructive (checked per-step).
_DESTRUCTIVE_ARGS = {
    "file_controller": {"action": {"delete", "move", "rename", "write"}},
    "desktop_control": {"task": {"clean"}},
    "procman": {"action": {"kill", "end"}},
    "macro": {"action": {"replay"}},
}


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    why: str = ""


@dataclass
class StepResult:
    step: Step
    ok: bool
    result: str
    seconds: float


@dataclass
class TaskReport:
    goal: str
    steps: list[StepResult] = field(default_factory=list)
    planned: int = 0
    stopped_early: bool = False
    cancelled: bool = False          # set when should_stop fired (cooperative)
    started: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def done(self) -> int:
        return sum(1 for s in self.steps if s.ok)

    def text(self) -> str:
        head = f"Task: {self.goal}"
        lines = [head, ""]
        for i, r in enumerate(self.steps, 1):
            mark = "✓" if r.ok else "✗"
            args = ", ".join(f"{k}={str(v)[:40]}" for k, v in r.step.args.items())
            line = f"{i}. {mark} {r.step.tool}"
            if args:
                line += f"({args})"
            line += f" — {r.seconds:.1f}s"
            if r.step.why:
                line += f" · {r.step.why}"
            lines.append(line)
            if not r.ok and r.result:
                lines.append(f"   ↳ {r.result[:200]}")
        skipped = self.planned - len(self.steps)
        if skipped > 0:
            why = (" — cancelled by user" if self.cancelled
                   else " — stopped at first failure"
                   if self.stopped_early else "")
            lines.append(f"({skipped} planned step(s) skipped{why})")
        ok_n = self.done
        lines.append("")
        lines.append(f"{ok_n}/{len(self.steps)} steps completed.")
        return "\n".join(lines)


def is_destructive(tool: str, args: dict | None = None) -> bool:
    """True if running this tool with these args can destroy/alter state in a
    way that needs the caller's explicit allow_destructive flag."""
    if tool in DESTRUCTIVE:
        return True
    for t, spec in _DESTRUCTIVE_ARGS.items():
        if tool == t:
            for key, bad_values in spec.items():
                val = str((args or {}).get(key, "")).lower().strip()
                if val and val in bad_values:
                    return True
    return False


def run_task(
    goal: str,
    steps: list[Step],
    runner: Callable[[str, dict], str],
    *,
    allow_destructive: bool = False,
    stop_on_error: bool = True,
    max_retries: int = 0,
    should_stop: Callable[[], bool] | None = None,
) -> TaskReport:
    """Execute a plan.

    runner(tool, args) -> str result; it may raise, which counts as a failure.
    Destructive steps are skipped (reported as failed, not run) unless
    allow_destructive=True.

    should_stop: optional cooperative-cancel predicate, checked at every
    step BOUNDARY (never mid-step). When it fires, report.cancelled=True
    and remaining steps are skipped — the caller persists `cancelled`
    status and resume() can pick the pending steps up later.
    """
    report = TaskReport(goal=goal, planned=len(steps))
    activity.note("task", f"started: {goal}", steps=len(steps))

    for i, step in enumerate(steps, 1):
        if should_stop is not None:
            try:
                stop_now = bool(should_stop())
            except Exception:
                stop_now = False
            if stop_now:
                report.cancelled = True
                report.stopped_early = True
                activity.note("task", f"cancelled before step {i}: {step.tool}")
                break
        if is_destructive(step.tool, step.args) and not allow_destructive:
            report.steps.append(StepResult(
                step=step, ok=False,
                result="refused: destructive step — ask the user to confirm first",
                seconds=0.0))
            activity.note("task", f"step {i} refused (destructive): {step.tool}")
            if stop_on_error:
                report.stopped_early = True
                break
            continue

        ev = activity.begin("tool", step.tool, step.args)
        attempts = 0
        while True:
            attempts += 1
            t0 = time.time()
            try:
                result = runner(step.tool, step.args)
                elapsed = time.time() - t0
                # A runner returns its outcome as text; empty is still success
                # unless it raised. Trust the exception channel, not wording.
                activity.finish(ev, True, result)
                report.steps.append(StepResult(step, True, str(result or ""), elapsed))
                break
            except Exception as e:
                elapsed = time.time() - t0
                if attempts <= max_retries:
                    activity.note("task", f"step {i} retry {attempts}: {e}")
                    continue
                activity.fail(ev, e)
                report.steps.append(StepResult(step, False, str(e), elapsed))
                if stop_on_error:
                    report.stopped_early = True
                break

        if report.stopped_early:
            break

    activity.note("task",
                  f"finished: {report.done}/{len(report.steps)} ok"
                  + (" (stopped early)" if report.stopped_early else ""),
                  steps=report.done)
    return report


def planner_prompt(goal: str, tool_names: list[str]) -> str:
    """Prompt handed to the LLM planner. Instructs a strict JSON reply."""
    return (
        "You are the planning stage of a desktop automation agent. "
        "Break the goal into a short sequence of steps. Each step MUST be one "
        "of the available tools with simple JSON arguments.\n\n"
        f"Available tools: {', '.join(sorted(tool_names))}\n\n"
        f"Goal: {goal}\n\n"
        "Reply with ONLY a JSON array, no prose, max 8 steps, ordered:\n"
        '[{"tool": "name", "args": {"param": "value"}, "why": "one short reason"}]\n'
        "If the goal needs no tools, reply with []."
    )


def replan_prompt(
    goal: str,
    done: list[dict],
    failed_tool: str,
    failed_args: dict,
    failed_result: str,
    tool_names: list[str],
) -> str:
    """Prompt for the REPLAN stage: the plan failed at some step — emit ONLY
    the remaining work, given what already succeeded. Strict JSON reply."""
    done_txt = "\n".join(
        f"- {d.get('tool')}: {'OK' if d.get('ok') else 'FAILED'} "
        f"({str(d.get('result') or '')[:220]})"
        for d in done[-8:]
    ) or "- (nothing completed yet)"
    return (
        "You are the replanning stage of a desktop automation agent. "
        "A plan failed part-way. Look at what already succeeded and the "
        "failure, then propose the REMAINING steps to still achieve the "
        "goal — do NOT repeat completed work, do NOT try the exact failed "
        "step the same way (change approach), and prefer different tools "
        "if the failed tool keeps failing.\n\n"
        f"Goal: {goal}\n\n"
        f"Already attempted (most recent last):\n{done_txt}\n\n"
        f"Failed step: {failed_tool} {json.dumps(failed_args)[:300]}\n"
        f"Failure: {str(failed_result)[:500]}\n\n"
        f"Available tools: {', '.join(sorted(tool_names))}\n\n"
        "Reply with ONLY a JSON array, max 5 steps, ordered:\n"
        '[{"tool": "name", "args": {"param": "value"}, "why": "one short reason"}]\n'
        "If the goal is unreachable now, reply with []."
    )


def parse_plan(text: str, known_tools: set[str]) -> list[Step]:
    """Parse the planner's JSON reply into steps. Tolerates code fences and
    leading prose (models do that even when told not to)."""
    if not text:
        return []
    s = text.strip()
    # strip ```json fences
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```", 1)[0]
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        raw = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return []
    steps: list[Step] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool", "")).strip()
        if not tool or tool not in known_tools:
            continue
        args = item.get("args") or {}
        if not isinstance(args, dict):
            args = {}
        steps.append(Step(tool=tool, args=args,
                          why=str(item.get("why", ""))[:120]))
    return steps
