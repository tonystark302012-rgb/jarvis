"""Episode memory — what happened last time, handed to the next planner.

The planner sees a tool list and a goal and nothing else, so every run starts
from zero and walks back into the same dead end. `core/episode_memory` derives
a one-line lesson from each finished run in `core.taskstore` and injects a
couple of comparable ones into the planner prompt.

These tests pin three things: the distillation rule (what is worth
remembering), the ranking (a warning beats a success), and the two boundaries
that matter operationally — privacy mode silences it entirely, and an empty
history produces a prompt byte-identical to the one this codebase sent before
the feature existed.
"""
from __future__ import annotations

import pytest


def _run(goal, status, results, run_id=1):
    return {"id": run_id, "goal": goal, "status": status,
            "plan": [], "results": results}


def _step(tool, ok, result=""):
    return {"tool": tool, "ok": ok, "result": result}


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from core import taskstore
    monkeypatch.setattr(taskstore, "_db_path", lambda: tmp_path / "tasks.db")
    taskstore.reset_for_tests()
    yield taskstore
    taskstore.reset_for_tests()


@pytest.fixture(autouse=True)
def _no_privacy(monkeypatch):
    """Privacy off by default so the tests exercise matching, not the gate."""
    from core import privacy
    monkeypatch.setattr(privacy, "is_on", lambda: False)


# ────────────────────────────────────────────────────────────────────────────
# distillation — what is worth remembering
# ────────────────────────────────────────────────────────────────────────────

class TestDistill:
    def test_a_clean_run_remembers_the_sequence(self):
        from core.episode_memory import distill
        ep = distill(_run("scan aaj ke papers", "done", [
            _step("web_search", True), _step("save_memory", True)]))
        assert ep["failed_tool"] is None and not ep["recovered"]
        assert "web_search → save_memory" in ep["lesson"]

    def test_a_failed_run_names_the_tool_and_the_reason(self):
        from core.episode_memory import distill
        ep = distill(_run("papers scan karo", "failed", [
            _step("web_search", True),
            _step("terminal", False, "error: dots failed (TimeoutError)")]))
        assert ep["failed_tool"] == "terminal"
        assert "TimeoutError" in ep["lesson"]
        assert "web_search" in ep["lesson"]      # what was reachable

    def test_a_recovery_is_the_most_useful_shape(self):
        """A tool that does not work here AND the route that does — this is
        the whole reason the module exists."""
        from core.episode_memory import distill
        ep = distill(_run("papers scan karo", "done", [
            _step("terminal", False, "error: no such command"),
            _step("web_search", True, "5 papers")]))
        assert ep["recovered"] is True
        assert "terminal failed" in ep["lesson"]
        assert "got past it with: web_search" in ep["lesson"]

    def test_matching_a_tool_by_name_uses_position_not_first_hit(self):
        """The same tool twice: the failure is the SECOND call, and the steps
        before it are both of the first ones."""
        from core.episode_memory import distill
        ep = distill(_run("retry thing", "failed", [
            _step("web_search", True), _step("file_processor", True),
            _step("web_search", False, "quota exhausted")]))
        assert ep["failed_tool"] == "web_search"
        assert "file_processor" in ep["lesson"]
        assert "quota exhausted" in ep["lesson"]

    @pytest.mark.parametrize("status", ["running", "cancelled", ""])
    def test_nothing_to_learn(self, status):
        """Cancelled is the user changing their mind, not a fact about the
        tools — remembering it would poison the next plan."""
        from core.episode_memory import distill
        assert distill(_run("x", status, [_step("a", True)])) is None

    def test_a_run_with_no_steps_is_not_an_episode(self):
        from core.episode_memory import distill
        assert distill(_run("x", "done", [])) is None

    def test_a_run_with_no_goal_is_not_an_episode(self):
        from core.episode_memory import distill
        assert distill(_run("   ", "done", [_step("a", True)])) is None

    def test_the_line_is_bounded(self):
        from core.episode_memory import distill
        ep = distill(_run("g" * 900, "failed", [_step("a", False, "e" * 900)]))
        assert len(ep["lesson"]) <= 260


# ────────────────────────────────────────────────────────────────────────────
# retrieval — which episodes are worth showing
# ────────────────────────────────────────────────────────────────────────────

class TestLessonsFor:
    def _seed(self, db, goal, status, results):
        rid = db.create(goal, [])
        db.update(rid, status, results)
        return rid

    def test_no_history_says_nothing(self, db):
        from core.episode_memory import lessons_for
        assert lessons_for("scan papers") == ""

    def test_an_unrelated_goal_says_nothing(self, db):
        from core.episode_memory import lessons_for
        self._seed(db, "install steam games", "done", [_step("game_updater", True)])
        assert lessons_for("aaj ke papers scan karo") == ""

    def test_a_related_goal_is_surfaced(self, db):
        from core.episode_memory import lessons_for
        self._seed(db, "papers scan karo aur summary banao", "failed",
                   [_step("terminal", False, "no such command")])
        out = lessons_for("papers scan karo")
        assert "terminal" in out and "failed" in out

    def test_a_warning_outranks_a_success(self, db):
        """The planner can probably find a working path on its own; what it
        cannot know is which path is already known to be dead."""
        from core.episode_memory import lessons_for
        self._seed(db, "papers scan karo", "done", [_step("web_search", True)])
        self._seed(db, "papers scan karo aur save karo", "failed",
                   [_step("terminal", False, "boom")])
        out = lessons_for("papers scan karo", limit=1)
        assert "terminal" in out and "web_search" not in out

    def test_the_same_goal_repeated_is_one_lesson(self, db):
        from core.episode_memory import lessons_for
        for _ in range(5):
            self._seed(db, "papers scan karo", "done", [_step("web_search", True)])
        out = lessons_for("papers scan karo", limit=2)
        assert out.count("- ") == 1

    def test_the_block_is_bounded(self, db):
        from core.episode_memory import _MAX_CHARS, lessons_for
        for i in range(20):
            self._seed(db, f"papers scan karo variant {i}", "failed",
                       [_step("terminal", False, "e" * 200)])
        out = lessons_for("papers scan karo", limit=10)
        assert len(out) <= _MAX_CHARS + 200          # + the header line

    def test_privacy_mode_silences_it(self, db, monkeypatch):
        """Injected lessons are sent to a cloud planner. Privacy mode means
        the assistant stops shipping the user's content out on their behalf —
        their past phrasing included."""
        from core import privacy
        from core.episode_memory import lessons_for
        self._seed(db, "papers scan karo", "failed", [_step("a", False, "x")])
        monkeypatch.setattr(privacy, "is_on", lambda: True)
        assert lessons_for("papers scan karo") == ""

    def test_a_broken_store_is_not_a_broken_plan(self, monkeypatch):
        from core import episode_memory, taskstore
        def boom(*a, **k):
            raise RuntimeError("db is on fire")
        monkeypatch.setattr(taskstore, "recent_episodes", boom)
        assert episode_memory.lessons_for("anything at all") == ""

    def test_an_empty_goal_says_nothing(self, db):
        from core.episode_memory import lessons_for
        self._seed(db, "papers scan karo", "done", [_step("a", True)])
        assert lessons_for("   ") == ""


# ────────────────────────────────────────────────────────────────────────────
# the boundaries — the prompt, and the wiring
# ────────────────────────────────────────────────────────────────────────────

class TestPlannerPrompt:
    def test_no_lessons_builds_exactly_the_old_prompt(self):
        """Back-compat is the point: with no history (and for every other
        caller of planner_prompt) the string must be what it always was."""
        from core.orchestrator import planner_prompt
        assert planner_prompt("g", ["a", "b"]) == planner_prompt("g", ["a", "b"], lessons="")
        body = planner_prompt("g", ["a", "b"])
        assert body.count("Runs you have performed before") == 0
        assert "Goal: g" in body and "Available tools: a, b" in body

    def test_lessons_land_above_the_goal(self):
        from core.orchestrator import planner_prompt
        body = planner_prompt("g", ["a"], lessons="Runs you have performed before: - x")
        assert "Runs you have performed before" in body
        assert body.index("Runs you have performed") < body.index("Goal: g")

    def test_task_agent_feeds_lessons_into_the_real_prompt(self, monkeypatch):
        """The wiring, not the unit: `_plan_with_llm` must actually pass what
        episode memory returns, all the way into the prompt string."""
        from actions import task_agent as ta
        from core import episode_memory, gemini
        seen: list[str] = []
        monkeypatch.setattr(episode_memory, "lessons_for",
                            lambda goal, limit=2: "Runs you have performed before:\n- X failed")
        monkeypatch.setattr(gemini, "call",
                            lambda prompt, **kw: seen.append(prompt[0]) or None)
        ta._plan_with_llm("scan papers", ["web_search"])
        assert seen and "X failed" in seen[0]
        assert "Goal: scan papers" in seen[0]

    def test_a_privacy_silence_reaches_the_prompt_as_nothing(self, db, monkeypatch):
        from actions import task_agent as ta
        from core import gemini, privacy
        self_rid = db.create("papers scan karo", [])
        db.update(self_rid, "failed", [_step("terminal", False, "boom")])
        monkeypatch.setattr(privacy, "is_on", lambda: True)
        seen: list[str] = []
        monkeypatch.setattr(gemini, "call",
                            lambda prompt, **kw: seen.append(prompt[0]) or None)
        ta._plan_with_llm("papers scan karo", ["terminal"])
        assert seen and "failed at terminal" not in seen[0]


class TestTheLoopCloses:
    """The headline claim, end to end and without a network: run a goal, watch
    it fail, then run the same goal again and read the planner's actual
    prompt. The second plan must see what the first one learned."""

    class _Reply:
        def __init__(self, text):
            self.text = text

    def _wire(self, monkeypatch, ta, prompts, plan_json):
        from core import gemini
        def fake_call(prompt, **kw):
            prompts.append(prompt[0])
            return self._Reply(plan_json)
        monkeypatch.setattr(gemini, "call", fake_call)
        monkeypatch.setattr(ta, "_replan_llm", lambda *a, **k: [])
        ta.set_runner_names(["terminal", "web_search"])

    def test_a_failed_run_teaches_the_next_plan(self, db, monkeypatch):
        from actions import task_agent as ta

        # ── run 1: the plan uses `terminal`, and `terminal` fails ──────────
        prompts: list[str] = []
        self._wire(monkeypatch, ta, prompts,
                   '[{"tool":"terminal","args":{"command":"scan"},"why":"first"}]\n')
        ta.set_runner(lambda t, a: "error: no such command: scan")
        first = ta.task_agent({"description": "papers scan karo"})
        assert "terminal" in first and "✗" in first, first
        assert "resemble this goal" not in prompts[0], (
            "the first run had no history to learn from")
        assert db.recent_episodes(), "the run was never recorded as an episode"

        # ── run 2: same goal — the failure must now be in the prompt ───────
        prompts.clear()
        ta.set_runner(lambda t, a: "done")
        ta.task_agent({"description": "papers scan karo"})
        assert "resemble this goal" in prompts[0], prompts[0]
        assert "failed at terminal" in prompts[0]
        assert "no such command" in prompts[0]

    def test_a_clean_run_teaches_the_sequence_instead(self, db, monkeypatch):
        from actions import task_agent as ta
        prompts: list[str] = []
        self._wire(monkeypatch, ta, prompts,
                   '[{"tool":"web_search","args":{"query":"x"},"why":"look"}]\n')
        ta.set_runner(lambda t, a: "5 papers found")
        ta.task_agent({"description": "papers scan karo"})

        prompts.clear()
        ta.task_agent({"description": "papers scan karo"})
        assert "worked as: web_search" in prompts[0]
