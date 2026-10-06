# core/episode_memory.py
"""Episode memory — what actually happened last time, handed to the planner.

WHAT THIS IS, AND WHAT IT IS NOT
    The planner (`actions/task_agent`) sees the tool list and the goal, and
    nothing else. So every run starts from zero: it will cheerfully plan
    `terminal` for the thing that failed there yesterday, in the same way, and
    rediscover the same dead end. This module gives it a short, bounded note
    about comparable runs — the tools that worked, the ones that did not, and
    why.

    It is NOT learning. Nothing here changes a weight, a prompt or a rule. It
    is retrieval plus one paragraph of context: deterministic, auditable, and
    deletable (drop the run history and it forgets). Calling it "self-
    improvement" would be the kind of claim this codebase has been cleaning
    out — see tests/test_verdict_honesty.py for what that cost last time.

WHERE THE DATA COMES FROM
    `core.taskstore` already persists every run: goal, status, plan, results.
    So there is no second store to keep in sync — an episode is derived from a
    run row, and deleting the run deletes the lesson. `distill()` is pure so
    the rule for "what is worth remembering" is testable without a database.

PRIVACY
    Injected text goes to the planner, which is a cloud model, so privacy mode
    turns it off entirely (`lessons_for` returns ""). The current goal is
    already sent in every case; this module's whole contribution is sending
    MORE of the user's past phrasing, which is exactly what privacy mode is
    about. Off means off.
"""
from __future__ import annotations

from core.text_search import tokens

#: How many past runs to consider, how many to inject, and how long the block
#: may get. All three are deliberately small: this rides in a prompt that runs
#: on every planned goal, and a planner handed a wall of history plans worse,
#: not better.
_POOL = 150
_MAX_LESSONS = 2
_MAX_CHARS = 700
_REASON_CAP = 110
_LINE_CAP = 260

#: Same goal ten times is one lesson, not ten.
_MAX_PER_SIGNATURE = 1


def _signature(goal: str) -> str:
    return " ".join(sorted(set(tokens(goal))))


def _brief(text: object, cap: int = _REASON_CAP) -> str:
    one = " ".join(str(text or "").split())
    return one[:cap] + ("…" if len(one) > cap else "")


def distill(run: dict) -> dict | None:
    """One finished run → the lesson worth carrying forward. Pure.

    Returns None when the run carries no signal: still running, no steps, or
    cancelled before anything ran (the user changed their mind — not a fact
    about the tools).
    """
    goal = str(run.get("goal") or "").strip()
    if not goal:
        return None
    status = str(run.get("status") or "")
    if status in ("", "running"):
        return None
    results = [r for r in (run.get("results") or []) if isinstance(r, dict)]
    if not results:
        return None

    if status == "cancelled":      # the user changed their mind, not a fact
        return None

    ran = [str(r.get("tool") or "?") for r in results]
    at = next((i for i, r in enumerate(results) if not r.get("ok")), None)
    label = f'"{_brief(goal, 60)}"'

    if at is None:
        failed_tool = None
        failed_why = ""
        recovered = False
        lesson = f"{label} — worked as: {' → '.join(ran[:8])}"
    else:
        failed_tool = ran[at]
        failed_why = _brief(results[at].get("result"))
        # "Recovered" = something failed and the run still finished. That is
        # the most useful shape to remember: it names a tool that does not
        # work here AND a route that does.
        tail = [ran[j] for j, r in enumerate(results) if j > at and r.get("ok")]
        recovered = status == "done" and bool(tail)
        why = f" ({failed_why})" if failed_why else ""
        if recovered:
            lesson = (f"{label} — {failed_tool} failed{why}; "
                      f"got past it with: {' → '.join(tail[:5])}")
        else:
            reach = [t for t in ran[:at] if t != failed_tool]
            lesson = (f"{label} — failed at {failed_tool}{why}"
                      + (f"; reachable before it: {', '.join(reach)}"
                         if reach else ""))

    return {
        "goal": goal,
        "status": status,
        "workflow": ran,
        "failed_tool": failed_tool,
        "failed_why": failed_why,
        "recovered": recovered,
        "lesson": lesson[:_LINE_CAP],
    }


def _score(goal_tokens: set[str], run_goal: str) -> int:
    """Shared significant words. Long and short goals score alike — a hit is a
    hit, and normalising by length would bury the short precise ones."""
    return len(goal_tokens & set(tokens(run_goal)))


def lessons_for(goal: str, limit: int = _MAX_LESSONS) -> str:
    """A prompt fragment about comparable past runs, or "" when there is
    nothing to say. Never raises — a lesson that breaks planning is worse than
    no lesson."""
    try:
        from core import privacy
        if privacy.is_on():
            return ""
    except Exception:
        return ""
    try:
        from core import taskstore
        runs = taskstore.recent_episodes(_POOL)
    except Exception:
        return ""

    goal_tokens = set(tokens(goal))
    if not goal_tokens:
        return ""

    scored: list[tuple[int, int, dict]] = []
    seen: dict[str, int] = {}
    for run in runs:
        ep = distill(run)
        if ep is None:
            continue
        overlap = _score(goal_tokens, ep["goal"])
        if overlap <= 0:
            continue
        sig = _signature(ep["goal"])
        if seen.get(sig, 0) >= _MAX_PER_SIGNATURE:
            continue
        seen[sig] = seen.get(sig, 0) + 1
        # Failures first: a warning is worth more than a success, because the
        # success the planner can probably reach on its own.
        rank = (0 if ep["failed_tool"] else 1, -overlap)
        scored.append((rank[0], rank[1], ep))

    if not scored:
        return ""

    scored.sort(key=lambda t: (t[0], t[1]))
    lines: list[str] = []
    used = 0
    for _kind, _overlap, ep in scored[:limit]:
        line = f"- {ep['lesson']}"
        if used + len(line) > _MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    return (
        "Runs you have performed before that resemble this goal — learn from "
        "them, do not repeat a failure the same way:\n"
        + "\n".join(lines)
    )


def summary(limit: int = 8) -> str:
    """Human-readable view of what the planner would be told. Same
    distillation, no matching — for diagnostics and for the tests that assert
    the wording reaches a prompt."""
    try:
        from core import taskstore
        runs = taskstore.recent_episodes(_POOL)
    except Exception as e:
        return f"Episode memory unavailable: {e}"
    eps = [ep for ep in (distill(r) for r in runs) if ep]
    if not eps:
        return ("No episodes yet — they are recorded as task_agent runs "
                "finish.")
    head = f"Last {min(len(eps), limit)} episode(s):"
    return "\n".join([head] + [f"• {e['lesson']}" for e in eps[:limit]])
