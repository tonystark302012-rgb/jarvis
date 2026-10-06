"""An unattended automation must never report a success that did not happen.

The bug this file locks down: a tool reports failure as a STRING, never as an
exception, and `rules._exec_rule` returned `True` for every string it got. So a
scheduled research run whose search came back empty — or whose call was refused
by the autonomy gate — was filed as healthy, cleared any incident, and told the
user "Rule 'X' fired". The Mission Control timeline marked the very same call
as failed. Two answers, one call.

The test predicate had also been copy-pasted into two more places. All three
now read `core.action_loader.classify_result`.
"""
from __future__ import annotations

import pytest

#: Real failure text, captured from the tools that produce it. If a tool
#: starts answering differently, these are what should be updated.
REFUSED_BY_PERMISSION = ("denied: 'search_web' needs the 'research' "
                         "permission — this dot has it off")
OBSERVE_BLOCK = ("Autonomy mode is OBSERVE (read-only) — 'dots' would change "
                 "state, so I didn't run it.")
EMPTY_SEARCH = "No results found for: AI research papers"
DIGEST = ("📄 5 papers mile: 1. Attention Is All You Need 2. Mamba: Linear-Time "
          "Sequence Modeling 3. RWKV 4. RetNet 5. Hyena Hierarchy")


class TestClassifyResult:
    """One function decides what a handler's return value means."""

    def test_plain_output_is_ok(self):
        from core.action_loader import RESULT_OK, classify_result
        assert classify_result("Model saved: box-64483.stl") == RESULT_OK

    def test_terminal_output_is_not_mistaken_for_failure(self):
        """terminal wraps stdout, so a command that PRINTS the word error must
        not mark itself failed. This is why the classifier matches prefixes and
        the two legacy windows instead of scanning for arbitrary keywords."""
        from core.action_loader import RESULT_OK, classify_result
        out = "$ grep -n error: app.log\n[exit 0]\nerror: could not open config\n"
        assert classify_result(out) == RESULT_OK

    @pytest.mark.parametrize("text", [
        "Tool 'web_search' failed: connection reset",
        "Action 'ghost_tool' is not available.",
        "error: dots failed (TimeoutError: research timed out)",
        "refused: that would delete the vault",
        "unknown tool: nope",
        REFUSED_BY_PERMISSION,
        "search failed: every Gemini model on the ladder failed",
    ])
    def test_failure_shapes(self, text):
        from core.action_loader import RESULT_FAILED, classify_result
        assert classify_result(text) == RESULT_FAILED

    def test_observe_mode_is_blocked_not_failed(self):
        """A policy refusal is its own verdict — the rule was not broken, the
        user's autonomy mode said no. Reporting it as 'failed' would send them
        debugging a tool that never ran."""
        from core.action_loader import RESULT_BLOCKED, classify_result
        assert classify_result(OBSERVE_BLOCK) == RESULT_BLOCKED

    @pytest.mark.parametrize("text", [
        EMPTY_SEARCH,
        "No files found.",
        "",
        "   ",
        None,
    ])
    def test_empty_shapes(self, text):
        from core.action_loader import RESULT_EMPTY, classify_result
        assert classify_result(text) == RESULT_EMPTY

    def test_failure_is_checked_before_empty(self):
        """A search that could not run must not be softened into 'found
        nothing' — the two need different words in front of the user."""
        from core.action_loader import RESULT_FAILED, classify_result
        assert classify_result(
            "search failed: no results for upstream") == RESULT_FAILED


class TestRuleFiringTellsTheTruth:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.rules as r
        monkeypatch.setattr(r, "_path", lambda: tmp_path / "rules.json")
        for attr in ("_RULES", "_HEALTH", "_DAY_KEYS", "_LAST_FIRE", "_STATE"):
            getattr(r, attr).clear()
        r._LOADED = False
        r.set_runner(None)
        r.set_notifier(None)
        self.fired: list[str] = []
        r.set_notifier(self.fired.append)
        yield
        r.set_runner(None)
        r.set_notifier(None)
        r._HEALTH.clear()

    def _rule(self, ret):
        import actions.rules as r
        r.set_runner(lambda tool, args, ret=ret: ret)
        rule = {"id": "r1", "label": "Daily research", "tool": "dots",
                "args": {}, "trigger": {"type": "time", "value": "08:00"}}
        # no _load(): the rule dict is handed straight to _exec_rule
        return r, rule

    def test_a_refused_call_is_not_a_success(self):
        r, rule = self._rule(REFUSED_BY_PERMISSION)
        ok, out = r._exec_rule(rule, "time 08:00")
        assert not ok
        assert "denied" in out
        r._note_health(rule, ok, out, 1_000_000.0)
        assert r._HEALTH[rule["id"]]["fails"] == 1     # visible + heal scheduled

    def test_an_empty_result_is_reported_instead_of_hidden(self):
        """The user's own requirement: 'agar kuch nahi mila to batao, chup na
        raho'. Nothing is broken, so the incident must stay clean — but the
        notification may not claim a successful digest."""
        r, rule = self._rule(EMPTY_SEARCH)
        ok, out = r._exec_rule(rule, "time 08:00")
        assert ok                       # the automation still works
        r._note_health(rule, ok, out, 1_000_000.0)
        assert rule["id"] not in r._HEALTH
        assert self.fired and "found nothing" in self.fired[0]

    def test_a_blocked_rule_is_visible(self):
        r, rule = self._rule(OBSERVE_BLOCK)
        ok, _ = r._exec_rule(rule, "time 08:00")
        assert not ok
        assert "BLOCKED" in self.fired[0]

    def test_the_digest_reaches_the_notification(self):
        """The other half of the goal: the notification has to carry the thing
        the rule produced. It used to send only "Rule 'X' fired"."""
        r, rule = self._rule(DIGEST)
        ok, out = r._exec_rule(rule, "time 08:00")
        assert ok and out == DIGEST
        assert "Attention Is All You Need" in self.fired[0]

    def test_output_is_flattened_and_capped(self):
        r, rule = self._rule("line one\nline two\n" + "x" * 5_000)
        r._exec_rule(rule, "time 08:00")
        msg = self.fired[0]
        assert "\n" not in msg and len(msg) < 300

    def test_failure_reason_reaches_the_health_report(self):
        r, rule = self._rule("error: dots failed (TimeoutError)")
        ok, out = r._exec_rule(rule, "time 08:00")
        r._note_health(rule, ok, out, 1_000_000.0)
        report = r.health_report()
        assert "Daily research" in report and "TimeoutError" in report
        assert "All automation rules healthy" not in report

    def test_a_raising_runner_still_reports_failure(self):
        """Back-compat: the exception path is what the old tests covered."""
        import actions.rules as r
        def boom(tool, args):
            raise RuntimeError("service down")
        r.set_runner(boom)
        rule = {"id": "r1", "label": "scan", "tool": "x", "args": {}}
        ok, out = r._exec_rule(rule, "time")
        assert not ok and "failed" in out


class TestScenesStopAtTheFirstFailingStep:
    """The docstring promised this and it only ever happened on an exception."""

    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.rules as r
        monkeypatch.setattr(r, "_path", lambda: tmp_path / "rules.json")
        for attr in ("_RULES", "_HEALTH", "_DAY_KEYS", "_LAST_FIRE", "_STATE"):
            getattr(r, attr).clear()
        r._LOADED = False
        r.set_runner(None)
        r.set_notifier(lambda m: None)
        yield
        r.set_runner(None)
        r.set_notifier(None)

    def test_later_steps_do_not_run_after_a_refusal(self):
        import actions.rules as r
        ran: list[str] = []
        def runner(tool, args):
            ran.append(tool)
            return {"a": "step a done", "b": REFUSED_BY_PERMISSION,
                    "c": "never"}[tool]
        r.set_runner(runner)
        rule = {"id": "s1", "label": "Morning scene",
                "steps": [{"tool": "a", "args": {}},
                          {"tool": "b", "args": {}},
                          {"tool": "c", "args": {}}]}
        ok, out = r._exec_rule(rule, "time 08:00")
        assert not ok
        assert ran == ["a", "b"]                  # c was never attempted
        assert "stopped at step 2/3" in out

    def test_a_clean_scene_still_completes(self):
        import actions.rules as r
        r.set_runner(lambda tool, args: f"{tool} done")
        rule = {"id": "s2", "label": "Scene",
                "steps": [{"tool": "a", "args": {}}, {"tool": "b", "args": {}}]}
        ok, out = r._exec_rule(rule, "time 08:00")
        assert ok and "2/2 steps done" in out


class TestTheVerdictHasOneHome:
    """The registry's audit chain, the Mission Control timeline and the rules
    engine must not be able to disagree about the same string again."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        from core import audit_chain as ac
        monkeypatch.setattr(ac, "_db_path", lambda: tmp_path / "audit.db")
        ac.reset_for_tests()
        yield
        ac.reset_for_tests()

    def _registry(self, name, ret):
        import core.action_loader as al
        rec = al.ActionRecord(name=name, description="d", parameters={},
                              handler=lambda parameters, ret=ret: ret,
                              valid=True)
        return al.ActionRegistry({name: rec}, logger=lambda m: None)

    def test_registry_audits_a_refusal_as_failed(self):
        from core import audit_chain as ac
        self._registry("dots", REFUSED_BY_PERMISSION).run("dots", {})
        assert "(failed)" in ac.recent(1)

    def test_registry_audits_an_empty_result_as_ok(self):
        """Finding nothing is not a failure — the timeline must not go red for
        a search that honestly returned zero hits."""
        import core.action_loader as al
        from core import audit_chain as ac
        self._registry("web_search", EMPTY_SEARCH).run("web_search", {})
        assert "(ok)" in ac.recent(1)
        assert al.classify_result(EMPTY_SEARCH) == al.RESULT_EMPTY
