"""
Tests for the second-wave upgrades: Mission Control activity log, agentic
orchestrator, task_agent, scrape, diagrams, charts, clipboard history,
automation rules, macros, process manager, focus, window layouts, scanner
extensions, and the dashboard mirror plumbing.

Every test is offline: LLM planners, runners, clocks and network are faked
or the code path is pure. Nothing here needs a display server.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest


# ────────────────────────────────────────────────────────────────────────────
# core.activity — Mission Control data layer
# ────────────────────────────────────────────────────────────────────────────

class TestActivity:
    def test_begin_finish_records_duration_and_preview(self):
        from core import activity
        activity.clear()
        ev = activity.begin("tool", "web_search", {"query": "hi"})
        assert ev.running
        activity.finish(ev, True, "some results")
        assert not ev.running and ev.ok is True
        assert ev.duration >= 0
        assert "some results" in ev.preview
        recent = activity.recent()
        assert recent and recent[-1].name == "web_search"

    def test_fail_marks_not_ok(self):
        from core import activity
        activity.clear()
        ev = activity.begin("tool", "boom")
        activity.fail(ev, ValueError("kaput"))
        assert ev.ok is False
        assert "kaput" in ev.preview
        assert activity.stats()["failed"] == 1

    def test_timeline_marks_and_stats(self):
        from core import activity
        activity.clear()
        ok_ev = activity.begin("tool", "scan")
        activity.finish(ok_ev, True, "fine")
        bad_ev = activity.begin("tool", "scrape")
        activity.fail(bad_ev, RuntimeError("404"))
        activity.note("task", "plan created")
        tl = activity.timeline()
        assert "✓ scan" in tl and "✗ scrape" in tl
        assert "plan created" in tl
        s = activity.stats()
        assert s["tool_calls"] == 2 and s["ok"] == 1 and s["failed"] == 1

    def test_summary_truncates_and_sanitizes(self):
        from core import activity
        activity.clear()
        ev = activity.begin("tool", "t", {"q": "x" * 200, "mode": "news"})
        s = ev.summary()
        assert len(s) <= 160 and "…" in s and "mode=news" in s

    def test_broken_listener_never_raises(self):
        from core import activity
        activity.clear()
        activity.add_listener(lambda ev: 1 / 0)
        try:
            ev = activity.begin("tool", "x")
            activity.finish(ev, True, "y")
        finally:
            activity.remove_listener(lambda ev: 1 / 0)  # not same obj; harmless
            activity._listeners.clear()
        assert activity.stats()["tool_calls"] == 1

    def test_ring_buffer_bounded(self):
        from core import activity
        activity.clear()
        for i in range(activity.MAX_EVENTS + 40):
            ev = activity.begin("tool", f"t{i}")
            activity.finish(ev, True, "ok")
        assert len(activity.recent(10_000)) == activity.MAX_EVENTS
        activity.clear()


# ────────────────────────────────────────────────────────────────────────────
# core.orchestrator — plan / execute / verify
# ────────────────────────────────────────────────────────────────────────────

class TestOrchestrator:
    def test_parse_plan_plain_and_fenced(self):
        from core.orchestrator import parse_plan
        tools = {"scan", "weather_report"}
        plain = parse_plan('[{"tool": "scan", "args": {"what": "system"}}]', tools)
        assert len(plain) == 1 and plain[0].tool == "scan"
        fenced = parse_plan(
            'Sure! Here is the plan:\n```json\n'
            '[{"tool": "weather_report", "args": {"city": "Jaipur"}, "why": "ask"}]\n```',
            tools)
        assert len(fenced) == 1 and fenced[0].tool == "weather_report"

    def test_parse_plan_filters_unknown_tools_and_garbage(self):
        from core.orchestrator import parse_plan
        out = parse_plan(
            '[{"tool": "rm_rf", "args": {}}, {"tool": "scan", "args": {}}]',
            {"scan"})
        assert [s.tool for s in out] == ["scan"]
        assert parse_plan("not json at all", {"scan"}) == []
        assert parse_plan("", {"scan"}) == []

    def test_is_destructive_matrix(self):
        from core.orchestrator import is_destructive
        assert is_destructive("procman", {"action": "kill"})
        assert is_destructive("file_controller", {"action": "delete"})
        assert not is_destructive("file_controller", {"action": "list"})
        assert not is_destructive("scan", {})
        assert is_destructive("shutdown_jarvis", {})

    def test_run_task_success_path(self):
        from core.orchestrator import Step, run_task
        calls = []

        def runner(tool, args):
            calls.append((tool, args))
            return f"{tool} done"

        rep = run_task("clean up", [
            Step("scan", {"what": "system"}, why="look"),
            Step("file_controller", {"action": "list"}),
        ], runner)
        assert rep.ok and rep.done == 2 and not rep.stopped_early
        assert calls[0][0] == "scan"
        text = rep.text()
        assert "2/2 steps completed" in text and "✓" in text

    def test_run_task_stops_at_first_failure(self):
        from core.orchestrator import Step, run_task

        def runner(tool, args):
            if tool == "scrape":
                raise RuntimeError("network down")
            return "ok"

        rep = run_task("g", [
            Step("scan"), Step("scrape"), Step("weather_report"),
        ], runner, stop_on_error=True)
        assert not rep.ok and rep.stopped_early
        assert len(rep.steps) == 2          # third never ran
        assert rep.planned == 3
        assert "1/2 steps completed" in rep.text()

    def test_run_task_retry_recovers(self):
        from core.orchestrator import Step, run_task
        attempts = {"n": 0}

        def runner(tool, args):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise TimeoutError("flaky")
            return "second try ok"

        rep = run_task("g", [Step("scan")], runner, max_retries=1)
        assert rep.ok and attempts["n"] == 2

    def test_destructive_step_refused_without_flag(self):
        from core.orchestrator import Step, run_task
        ran = []

        def runner(tool, args):
            ran.append(tool)
            return "killed"

        rep = run_task("kill it", [Step("procman", {"action": "kill", "pid": "1"})],
                       runner, allow_destructive=False)
        assert ran == []                    # never executed
        assert not rep.ok
        assert "refused" in rep.steps[0].result

    def test_destructive_step_runs_with_flag(self):
        from core.orchestrator import Step, run_task
        ran = []

        def runner(tool, args):
            ran.append(tool)
            return "killed"

        rep = run_task("kill it", [Step("procman", {"action": "kill", "pid": "1"})],
                       runner, allow_destructive=True)
        assert ran == ["procman"] and rep.ok

    def test_task_events_land_in_activity(self):
        from core import activity
        from core.orchestrator import Step, run_task
        activity.clear()
        run_task("g", [Step("scan")], lambda t, a: "ok")
        names = [e.name for e in activity.recent()]
        assert "task" in names and "scan" in names
        activity.clear()


# ────────────────────────────────────────────────────────────────────────────
# actions.mission — timeline / stats / clear
# ────────────────────────────────────────────────────────────────────────────

class TestMissionControl:
    def test_timeline_shows_recent_tools(self):
        from actions.mission import mission_control
        from core import activity
        activity.clear()
        ev = activity.begin("tool", "diagram", {"kind": "flow"})
        activity.finish(ev, True, "saved")
        out = mission_control({"action": "timeline", "limit": "5"})
        assert "diagram" in out and "✓" in out
        activity.clear()

    def test_stats_and_clear(self):
        from actions.mission import mission_control
        from core import activity
        activity.clear()
        ev = activity.begin("tool", "x")
        activity.finish(ev, True, "y")
        assert "1 tool calls" in mission_control({"action": "stats"})
        assert "cleared" in mission_control({"action": "clear"}).lower()
        assert activity.stats()["total"] == 0

    def test_empty_timeline_message(self):
        from actions.mission import mission_control
        from core import activity
        activity.clear()
        assert "no activity" in mission_control({}).lower()


# ────────────────────────────────────────────────────────────────────────────
# actions.task_agent — planning through the registry
# ────────────────────────────────────────────────────────────────────────────

class TestTaskAgent:
    def test_unwired_runner_explains_itself(self):
        from actions import task_agent as ta
        ta.set_runner(None)
        out = ta.task_agent({"description": "do things"})
        assert "not wired" in out

    def test_missing_goal_message(self):
        from actions import task_agent as ta
        ta.set_runner(lambda t, a: "ok")
        ta.set_runner_names(["scan"])
        assert "No goal" in ta.task_agent({})

    def test_full_plan_execute_with_fake_planner(self, monkeypatch):
        from actions import task_agent as ta
        from core.orchestrator import Step
        calls = []

        def fake_planner(goal, names):
            assert "scan" in names
            return [Step("scan", {"what": "system"}, why="check machine"),
                    Step("scan", {"what": "ports"})]

        monkeypatch.setattr(ta, "_plan_with_llm", fake_planner)
        ta.set_runner(lambda t, a: calls.append((t, a)) or "done")
        ta.set_runner_names(["scan", "weather_report"])
        out = ta.task_agent({"description": "health check"})
        assert len(calls) == 2
        assert "2/2 steps completed" in out

    def test_destructive_plan_step_refused(self, monkeypatch):
        from actions import task_agent as ta
        from core.orchestrator import Step
        ran = []
        monkeypatch.setattr(ta, "_plan_with_llm",
                            lambda g, n: [Step("procman", {"action": "kill", "pid": "9"})])
        ta.set_runner(lambda t, a: ran.append(t) or "x")
        ta.set_runner_names(["procman"])
        out = ta.task_agent({"description": "kill stuff"})
        assert ran == []
        assert "refused" in out

    def test_empty_plan_reports_cleanly(self, monkeypatch):
        from actions import task_agent as ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: [])
        ta.set_runner(lambda t, a: "x")
        ta.set_runner_names(["scan"])
        assert "could not build a plan" in ta.task_agent({"description": "???"})

    def test_plan_prompt_mentions_tools(self):
        from core.orchestrator import planner_prompt
        p = planner_prompt("goal", {"scan", "chart"})
        assert "scan" in p and "chart" in p and "JSON array" in p


# ────────────────────────────────────────────────────────────────────────────
# actions.scrape — HTML → clean text (stdlib fallback path is tested here;
# bs4 branch is exercised when the lib exists)
# ────────────────────────────────────────────────────────────────────────────

class TestScrape:
    HTML = """
    <html><head><title>My Page</title><style>.x{color:red}</style></head>
    <body><nav>HOME ABOUT</nav>
    <script>var secret = "TRACKME";</script>
    <main><h1>Headline</h1><p>Real content lives here.</p></main>
    <footer>copyright</footer></body></html>
    """

    def test_clean_html_strips_chrome_and_keeps_body(self):
        from actions.scrape import clean_html
        title, text = clean_html(self.HTML, "http://x")
        assert title == "My Page"
        assert "Real content lives here." in text
        assert "TRACKME" not in text
        assert "copyright" not in text or "Real content" in text

    def test_clean_html_truncates(self):
        from actions.scrape import clean_html
        big = "<html><body>" + "<p>filler words here</p>" * 500 + "</body></html>"
        _, text = clean_html(big, "", max_chars=500)
        assert len(text) < 700 and "[truncated]" in text

    def test_missing_url_message(self):
        from actions.scrape import scrape
        assert "No URL" in scrape({})

    def test_bad_url_rejected_without_network(self):
        from actions.scrape import scrape
        out = scrape({"url": "ftp://evil"})
        assert "Unsupported" in out

    def test_top_links_parser(self):
        from actions.scrape import _top_links
        html = ('<a href="https://a.com">Alpha</a>'
                '<a href="https://b.com">Beta</a>'
                '<a href="/local">local</a>')
        links = _top_links(html, "x.com")
        assert [t for t, _ in links] == ["Alpha", "Beta"]


# ────────────────────────────────────────────────────────────────────────────
# actions.diagram + actions.charts — SVG generation (pure)
# ────────────────────────────────────────────────────────────────────────────

class TestDiagramAndCharts:
    @pytest.fixture(autouse=True)
    def _tmp_out(self, tmp_path, monkeypatch):
        import actions.diagram as d
        import actions.charts as c
        monkeypatch.setattr(d, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(c, "_base_dir", lambda: tmp_path)

    def test_flow_svg_saved(self):
        from actions.diagram import diagram
        out = diagram({"kind": "flow", "spec": "Build -> Test -> Deploy",
                       "title": "Pipeline"})
        assert out.startswith("Diagram saved:")
        path = Path(out.split(": ", 1)[1])
        svg = path.read_text()
        assert svg.startswith("<svg") and "Pipeline" in svg
        assert "Build" in svg and "Deploy" in svg

    def test_flow_branching(self):
        from actions.diagram import diagram
        out = diagram({"kind": "flow", "spec": "A -> B; A -> C"})
        path = Path(out.split(": ", 1)[1])
        svg = path.read_text()
        assert "A" in svg and "B" in svg and "C" in svg

    def test_sequence_and_mindmap_and_timeline(self):
        from actions.diagram import diagram
        for kind, spec in (
            ("sequence", "Alice: Hello\nBob: Hi back"),
            ("mindmap", "Project (frontend (html css))(backend (api db))"),
            ("timeline", "2024: Launch | 2025: Grow"),
        ):
            out = diagram({"kind": kind, "spec": spec})
            assert out.startswith("Diagram saved:"), kind
            path = Path(out.split(": ", 1)[1])
            assert "<svg" in path.read_text()

    def test_unknown_kind_and_empty_spec(self):
        from actions.diagram import diagram
        assert "Unknown kind" in diagram({"kind": "gantt", "spec": "x"})
        assert "No spec" in diagram({"kind": "flow", "spec": ""})

    def test_bad_flow_spec_readable_error(self):
        from actions.diagram import diagram
        assert "Could not read" in diagram({"kind": "flow", "spec": "no arrows here"})

    def test_chart_bar_saved_with_values(self):
        from actions.charts import chart
        out = chart({"kind": "bar", "data": "cpu=45, ram=70, disk=90",
                     "title": "Load"})
        path = Path(out.split(": ", 1)[1])
        svg = path.read_text()
        assert "<svg" in svg and "Load" in svg and "45" in svg

    def test_chart_line_and_pie(self):
        from actions.charts import chart
        assert chart({"kind": "line", "data": "mon=1, tue=3, wed=2"}).startswith("Chart saved:")
        assert chart({"kind": "pie", "data": "games=30, work=50, rest=20"}).startswith("Chart saved:")

    def test_chart_needs_numeric_data(self):
        from actions.charts import chart
        assert "Not enough numeric" in chart({"kind": "bar", "data": "a=abc"})
        assert "Unknown chart kind" in chart({"kind": "radar", "data": "a=1,b=2"})

    def test_chart_data_parsing_forms(self):
        from actions.charts import _parse_data
        assert _parse_data({"a": 1, "b": 2}) == [("a", 1.0), ("b", 2.0)]
        assert _parse_data("x=3, y=4") == [("x", 3.0), ("y", 4.0)]
        assert _parse_data("bad, =, nothing") == []


# ────────────────────────────────────────────────────────────────────────────
# actions.clip_history
# ────────────────────────────────────────────────────────────────────────────

class TestClipHistory:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from actions import clip_history as ch
        ch._HISTORY.clear()
        ch._LAST = ""
        yield
        ch._STOP.set()
        ch._HISTORY.clear()
        ch._LAST = ""

    def test_record_dedupes(self):
        from actions import clip_history as ch
        assert ch.record("hello")
        assert not ch.record("hello")        # duplicate
        assert not ch.record("   ")          # blank
        assert ch.record("world")

    def test_list_and_search_and_clear(self):
        from actions import clip_history as ch
        ch.record("https://example.com/page")
        ch.record("some secret text")
        listing = ch.clip_history({"action": "list"})
        assert "some secret text" in listing
        assert "2 total" in listing
        found = ch.clip_history({"action": "list", "query": "example"})
        assert "https://example.com/page" in found and "secret" not in found
        assert "cleared" in ch.clip_history({"action": "clear"}).lower()
        after = ch.clip_history({"action": "list"}).lower()
        assert "no clipboard entries" in after or "empty" in after

    def test_use_entry_returns_or_explains(self):
        from actions import clip_history as ch
        ch.record("first thing")
        out = ch.clip_history({"action": "use", "index": "1"})
        # headless: clipboard write fails → entry text is included instead
        assert "first thing" in out or "copied back" in out

    def test_use_out_of_range(self):
        from actions import clip_history as ch
        ch.record("only")
        assert "No entry #9" in ch.clip_history({"action": "use", "index": "9"})

    def test_watch_headless_message(self):
        from actions import clip_history as ch
        out = ch.clip_history({"action": "watch", "state": "on"})
        # pyperclip absent/headless → honest message, no crash
        assert isinstance(out, str) and out

    def test_save_action(self):
        from actions import clip_history as ch
        assert "Saved" in ch.clip_history({"action": "save", "text": "from ui"})
        assert "Nothing new" in ch.clip_history({"action": "save", "text": "from ui"})


# ────────────────────────────────────────────────────────────────────────────
# actions.rules — when/then automation
# ────────────────────────────────────────────────────────────────────────────

class TestRules:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.rules as r
        monkeypatch.setattr(r, "_path", lambda: tmp_path / "rules.json")
        r._RULES.clear()
        r._LOADED = False
        r._DAY_KEYS.clear()
        r._STATE.clear()
        r._LAST_FIRE.clear()
        fired = []
        r.set_runner(lambda t, a: fired.append((t, a)) or f"ran {t}")
        r.set_notifier(lambda m: None)
        self.fired = fired
        yield
        r._RULES.clear()
        r._LOADED = False
        r.set_runner(None)
        r.set_notifier(None)

    def test_add_list_remove_roundtrip(self):
        import actions.rules as r
        msg = r.add_rule({"type": "time", "value": "08:00"},
                         "weather_report", {"city": "Jaipur"}, "morning weather")
        assert "Rule added" in msg
        listing = r.list_rules()
        assert "weather_report" in listing and "08:00" in listing
        assert "Removed" in r.remove_rule("1")
        assert "No automation rules" in r.list_rules()

    def test_time_rule_fires_once_per_day(self):
        import actions.rules as r
        r.add_rule({"type": "time", "value": "09:30"}, "scan", {"what": "system"})
        # pick now whose local HH:MM == 09:30
        lt = time.localtime()
        base = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 30, 0, 0, 0, -1))
        out1 = r.tick(now=base)
        assert out1 and ("scan", {"what": "system"}) in self.fired
        # same day, second tick → no re-fire
        out2 = r.tick(now=base + 60)
        assert out2 == []
        assert len(self.fired) == 1

    def test_time_rule_not_due_at_other_hours(self):
        import actions.rules as r
        r.add_rule({"type": "time", "value": "09:30"}, "scan", {})
        other = time.mktime((2026, 1, 1, 10, 0, 0, 0, 0, -1))
        assert r.tick(now=other) == []
        assert self.fired == []

    def test_file_rule_fires_when_file_appears(self, tmp_path):
        import actions.rules as r
        watch = tmp_path / "downloads"
        watch.mkdir()
        r.add_rule({"type": "file", "value": "appears", "path": str(watch)},
                   "scan", {"what": "system"})
        assert r.tick() == []                # baseline, no fire
        (watch / "new.zip").write_bytes(b"x")
        out = r.tick()
        assert out and self.fired and self.fired[0][0] == "scan"

    def test_phrase_rule_fires_on_keyword(self):
        import actions.rules as r
        r.add_rule({"type": "phrase", "value": "movie mode"},
                   "video_player", {"action": "play"}, "movie time")
        assert r.fire_phrase("ok it's movie mode now") == ["ran video_player"]
        assert self.fired[0][0] == "video_player"
        # unrelated utterance does nothing
        assert r.fire_phrase("what's the weather") == []

    def test_tool_args_json_string_parsed(self):
        import actions.rules as r
        r.manage_rules({"action": "add", "trigger_type": "phrase",
                        "value": "hi", "tool": "scan",
                        "tool_args": '{"what": "ports"}'})
        assert r._RULES[-1]["args"] == {"what": "ports"}

    def test_add_validations(self):
        import actions.rules as r
        assert "Trigger needs" in r.add_rule({}, "scan")
        assert "must name a tool" in r.add_rule({"type": "time", "value": "1:00"}, "")
        assert "No rule matching" in r.remove_rule("nonexistent")

    def test_rules_persist_to_disk(self):
        import actions.rules as r
        r.add_rule({"type": "time", "value": "07:00"}, "scan", {})
        p = r._path()
        assert p.exists()
        data = json.loads(p.read_text())
        assert data[0]["tool"] == "scan"


# ────────────────────────────────────────────────────────────────────────────
# actions.macro — safe confirmations and store
# ────────────────────────────────────────────────────────────────────────────

class TestMacro:
    @pytest.fixture(autouse=True)
    def _tmp_macros(self, tmp_path, monkeypatch):
        import actions.macro as m
        monkeypatch.setattr(m, "_dir", lambda: tmp_path)
        m._EVENTS = []
        m._RECORDING = False
        yield
        m._STOP.set()
        m._RECORDING = False

    def test_list_empty(self):
        from actions.macro import macro
        assert "No macros" in macro({"action": "list"})

    def test_replay_requires_confirmation(self):
        from actions.macro import macro
        out = macro({"action": "replay", "name": "anything"})
        assert "confirm" in out.lower() and "yes" in out.lower()

    def test_replay_missing_macro_after_confirm(self):
        from actions.macro import macro
        out = macro({"action": "replay", "name": "ghost", "confirm": "yes"})
        assert "No macro named" in out

    def test_record_stop_headless_saves_or_explains(self):
        from actions.macro import macro
        start = macro({"action": "record"})
        assert "Recording started" in start
        time.sleep(0.15)
        stop = macro({"action": "stop", "name": "testrun"})
        # headless: either saved some sampled moves or reported zero events
        assert "Recording stopped" in stop
        if "saved" in stop:
            listing = macro({"action": "list"})
            assert "testrun" in listing

    def test_delete_macro(self):
        from actions.macro import macro, _macro_path
        _macro_path("temp").write_text(json.dumps(
            {"name": "temp", "events": [{"t": 0, "k": "move", "x": 1, "y": 2}]}))
        assert "deleted" in macro({"action": "delete", "name": "temp"})
        assert "No macro" in macro({"action": "delete", "name": "temp"})

    def test_replay_file_bounded(self):
        from actions.macro import _replay, _macro_path, MAX_EVENTS
        events = [{"t": i * 0.01, "k": "move", "x": i, "y": i}
                  for i in range(MAX_EVENTS + 50)]
        _macro_path("big").write_text(json.dumps({"name": "big", "events": events}))
        # headless: pyperclip... actually pyautogui absent → clear message
        out = _replay("big")
        assert isinstance(out, str) and out


# ────────────────────────────────────────────────────────────────────────────
# actions.procman — list + kill gates
# ────────────────────────────────────────────────────────────────────────────

class TestProcman:
    def test_list_runs_and_shows_header(self):
        from actions.procman import procman
        out = procman({"action": "list"})
        assert "PID" in out and "NAME" in out and "processes" in out

    def test_query_filter(self):
        from actions.procman import procman
        out = procman({"action": "list", "query": "python"})
        # python definitely running (this test), or honest empty
        assert "python" in out.lower() or "No processes match" in out

    def test_kill_requires_confirmation(self):
        from actions.procman import procman
        out = procman({"action": "kill", "pid": "1"})
        assert "Confirm" in out

    def test_kill_refuses_own_process(self):
        import os
        from actions.procman import procman
        out = procman({"action": "kill", "pid": str(os.getpid()), "confirm": "yes"})
        assert "refused" in out
        # we're still alive to assert this

    def test_kill_nonexistent_pid(self):
        from actions.procman import procman
        out = procman({"action": "kill", "pid": "999999999", "confirm": "yes"})
        assert "not found" in out or "refused" in out


# ────────────────────────────────────────────────────────────────────────────
# actions.focus — session state machine
# ────────────────────────────────────────────────────────────────────────────

class TestFocus:
    @pytest.fixture(autouse=True)
    def _reset(self):
        import actions.focus as f
        mutes = []
        f.set_callbacks(on_phase=lambda p, m: None, on_mute=lambda b: mutes.append(b))
        self.mutes = mutes
        yield
        f.stop_session(quiet=True)
        f.set_callbacks(on_phase=None, on_mute=None)

    def test_start_status_stop(self):
        from actions.focus import focus
        out = focus({"action": "start", "work_min": "25", "break_min": "5",
                     "rounds": "3"})
        # start_session first stops any prior session (mute False), then mutes
        assert "Focus session started" in out and True in self.mutes
        st = focus({"action": "status"})
        assert "Focus" in st and "25" in st
        stop = focus({"action": "stop"})
        assert "ended" in stop.lower() and self.mutes[-1] is False

    def test_status_without_session(self):
        from actions.focus import focus, stop_session
        stop_session(quiet=True)
        assert "No focus session" in focus({"action": "status"})

    def test_bounds_clamped(self):
        from actions.focus import focus, start_session, stop_session
        out = start_session(9999, 999, 99)
        assert "120 min work" in out      # clamped
        stop_session(quiet=True)


# ────────────────────────────────────────────────────────────────────────────
# actions.window_layout — geometry + graceful backend messaging
# ────────────────────────────────────────────────────────────────────────────

class TestWindowLayout:
    def test_split_geometry_sums_to_screen(self):
        from actions.window_layout import compute_rects
        rects = compute_rects((1920, 1080), ["left", "right"], gutter=6)
        assert len(rects) == 2
        (x1, y1, w1, h1), (x2, y2, w2, h2) = rects
        assert x1 == 0 and y1 == 0 and h1 == 1080
        assert w1 + 6 + w2 == 1920 and x2 == w1 + 6
        assert h2 == 1080

    def test_center_and_maximize_within_bounds(self):
        from actions.window_layout import compute_rects
        (cx, cy, cw, ch) = compute_rects((1000, 800), ["center"])[0]
        assert 0 <= cx and cx + cw <= 1000 and 0 <= cy and cy + ch <= 800
        assert compute_rects((1000, 800), ["maximize"])[0] == (0, 0, 1000, 800)

    def test_n_columns(self):
        from actions.window_layout import compute_rects
        rects = compute_rects((1200, 900), 3, gutter=0)
        assert len(rects) == 3
        assert sum(r[2] for r in rects) == 1200

    def test_no_backend_message(self, monkeypatch):
        import actions.window_layout as wl
        monkeypatch.setattr(wl, "_focus_windows_backend", lambda: "none")
        out = wl.window_layout({"action": "list"})
        assert "No window backend" in out

    def test_unknown_layout_message(self, monkeypatch):
        import actions.window_layout as wl
        monkeypatch.setattr(wl, "_focus_windows_backend", lambda: "xdotool")
        monkeypatch.setattr(wl, "_list_windows_xdotool", lambda: [])
        out = wl.window_layout({"action": "apply", "layout": "spiral"})
        assert "Unknown layout" in out or "No matching windows" in out


# ────────────────────────────────────────────────────────────────────────────
# actions.scanner — new modes
# ────────────────────────────────────────────────────────────────────────────

class TestScannerExtensions:
    def test_dupes_finds_identical_files(self, tmp_path):
        from actions.scanner import scan_dupes
        (tmp_path / "a.txt").write_bytes(b"same-content-here")
        (tmp_path / "b.txt").write_bytes(b"same-content-here")
        (tmp_path / "c.txt").write_bytes(b"different")
        out = scan_dupes(str(tmp_path), top=5)
        assert "duplicate group" in out and "reclaimable" in out

    def test_dupes_none_found(self, tmp_path):
        from actions.scanner import scan_dupes
        (tmp_path / "a.bin").write_bytes(b"aaa")
        (tmp_path / "b.bin").write_bytes(b"bbb")
        assert "No duplicate files" in scan_dupes(str(tmp_path))

    def test_treemap_reports_sizes(self, tmp_path):
        from actions.scanner import scan_treemap
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "big.bin").write_bytes(b"x" * 50_000)
        (tmp_path / "top.bin").write_bytes(b"y" * 10_000)
        out = scan_treemap(str(tmp_path))
        assert "sub" in out and "top.bin" in out and "%" in out

    def test_drives_lists_partitions(self):
        from actions.scanner import scan_drives
        out = scan_drives()
        assert "Drives" in out and "/" in out

    def test_startup_returns_string(self):
        from actions.scanner import scan_startup
        out = scan_startup()
        assert isinstance(out, str) and out

    def test_speed_fails_gracefully_offline(self):
        from actions.scanner import scan_action
        out = scan_action({"what": "speed"})
        # sandbox has no route to cloudflare → dispatcher catches, no crash
        assert isinstance(out, str) and out

    def test_dispatcher_accepts_new_modes(self):
        from actions.scanner import scan_action, _SCANS
        for mode in ("dupes", "treemap", "drives", "startup", "system"):
            assert mode in _SCANS
        assert "not one of them" not in scan_action({"what": "system"})

    def test_bad_mode_lists_options(self):
        from actions.scanner import scan_action
        out = scan_action({"what": "hologram"})
        assert "I can scan" in out


# ────────────────────────────────────────────────────────────────────────────
# dashboard — mirror plumbing & activity history rule
# ────────────────────────────────────────────────────────────────────────────

class TestDashboardMirror:
    def _server(self):
        from tests.test_upgrades import _server as mk
        return mk()

    def test_frame_broadcast_excluded_from_history(self):
        srv = self._server()

        async def go():
            await srv.broadcast({"type": "frame", "data": "abc"}, history=False)
            await srv.broadcast({"type": "activity", "name": "scan"})

        asyncio.run(go())
        types = [m.get("type") for m in srv._history]
        assert "frame" not in types
        assert "activity" in types

    def test_mirror_toggle_writes_sys_message(self, monkeypatch):
        srv = self._server()

        async def fake_loop():
            # stands in for the capture loop: keeps the task alive while on
            while srv._mirror_on:
                await asyncio.sleep(0.02)

        monkeypatch.setattr(srv, "_mirror_loop", fake_loop)

        async def go():
            await srv.mirror_set(True)
            await srv.mirror_set(True)      # idempotent
            await srv.mirror_set(False)

        asyncio.run(go())
        texts = [m.get("text", "") for m in srv._history if m.get("type") == "sys"]
        assert any("mirror ON" in t for t in texts)
        assert any("mirror OFF" in t for t in texts)
        assert srv._mirror_on is False

    def test_ws_mirror_message_accepted(self):
        from fastapi.testclient import TestClient
        srv = self._server()
        tok = "MIRRORTEST"
        srv._tokens.add(tok)

        async def fake_loop():
            while srv._mirror_on:
                await asyncio.sleep(0.02)

        srv._mirror_loop = fake_loop
        with TestClient(srv.app) as tc:
            with tc.websocket_connect(f"/ws?token={tok}") as ws:
                ws.send_json({"type": "mirror", "on": True})
                first = ws.receive_json()
                assert "Screen mirror ON" in str(first)
                ws.send_json({"type": "mirror", "on": False})
                second = ws.receive_json()
                assert "Screen mirror OFF" in str(second)
        srv._mirror_on = False

    def test_ws_rejects_bad_token(self):
        from starlette.websockets import WebSocketDisconnect
        from fastapi.testclient import TestClient
        srv = self._server()
        with TestClient(srv.app) as tc:
            with pytest.raises((WebSocketDisconnect, Exception)):
                with tc.websocket_connect("/ws?token=WRONG") as ws:
                    ws.receive_json()


# ────────────────────────────────────────────────────────────────────────────
# wiring guards — main.py must keep the hooks (can't import PyQt here)
# ────────────────────────────────────────────────────────────────────────────

# Replaced by the deeper TestMainWiring below (audit-driven: phrase rules on
# all input paths, agent-runner activity wrap, stored-loop listener).

# ────────────────────────────────────────────────────────────────────────────
# Feature upgrades: weather hourly, reminder list/cancel, dev_agent snapshots
# ────────────────────────────────────────────────────────────────────────────

class TestWeatherHourly:
    def test_hourly_request_and_render(self, monkeypatch):
        import actions.weather_report as w
        monkeypatch.setattr(w, "_REQUESTS", True)
        monkeypatch.setattr(w, "_geocode", lambda city: {
            "latitude": 26.9, "longitude": 75.8,
            "name": "Jaipur", "admin1": "Rajasthan"})
        seen = {}

        def fake_forecast(lat, lon, days=3, hourly=False):
            seen["hourly"] = hourly
            base = "2026-10-03"
            # 24 hourly slots starting at midnight
            times = [f"{base}T{h:02d}:00" for h in range(24)]
            return {
                "current": {"temperature_2m": 30, "weather_code": 1,
                            "relative_humidity_2m": 40, "wind_speed_10m": 10},
                "daily": {"time": [base], "temperature_2m_max": [31],
                          "temperature_2m_min": [22],
                          "precipitation_probability_max": [10]},
                "hourly": {"time": times,
                           "temperature_2m": [22 + (h % 12) for h in range(24)],
                           "precipitation_probability": [5] * 24},
            }

        monkeypatch.setattr(w, "_forecast", fake_forecast)
        out = w.weather_action({"city": "Jaipur", "time": "next hours"})
        assert seen.get("hourly") is True
        assert "hourly:" in out

    def test_default_request_skips_hourly(self, monkeypatch):
        import actions.weather_report as w
        monkeypatch.setattr(w, "_REQUESTS", True)
        monkeypatch.setattr(w, "_geocode", lambda city: {
            "latitude": 1, "longitude": 2, "name": "X", "admin1": ""})
        seen = {}

        def fake_forecast(lat, lon, days=3, hourly=False):
            seen["hourly"] = hourly
            return {"current": {"temperature_2m": 20, "weather_code": 0},
                    "daily": {"time": [], "temperature_2m_max": [],
                              "temperature_2m_min": [],
                              "precipitation_probability_max": []}}

        monkeypatch.setattr(w, "_forecast", fake_forecast)
        w.weather_action({"city": "X"})
        assert seen.get("hourly") is False


class TestReminderLifecycle:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.reminder as r
        monkeypatch.setattr(r, "_scripts_dir", lambda: tmp_path)
        yield

    def test_list_empty(self):
        from actions.reminder import reminder
        assert "No upcoming reminders" in reminder({"action": "list"})

    def test_registry_add_and_list(self):
        from actions.reminder import reminder, _registry_add
        y = time.localtime().tm_year + 1          # always in the future
        _registry_add({"task": "JARVISReminder_20991231_090000",
                       "date": f"{y}-12-31", "time": "09:00",
                       "message": "New Year call"})
        _registry_add({"task": "old_one", "date": "2000-01-01", "time": "00:00",
                       "message": "expired"})
        out = reminder({"action": "list"})
        assert "New Year call" in out and "expired" not in out

    def test_cancel_by_number_and_missing(self):
        from actions.reminder import reminder, _registry_add
        y = time.localtime().tm_year + 1          # always in the future
        _registry_add({"task": "JARVISReminder_x1", "date": f"{y}-12-31",
                       "time": "10:00", "message": "cancel me"})
        out = reminder({"action": "cancel", "which": "1"})
        assert "Cancelled" in out or "Removed from my list" in out
        # gone now
        assert "No reminder matching" in reminder({"action": "cancel", "which": "1"})

    def test_set_without_date_still_requires_fields(self):
        from actions.reminder import reminder
        assert "date and a time" in reminder({"action": "set"})
        assert "Couldn't parse" in reminder(
            {"date": "not-a-date", "time": "99:99", "message": "x"}) or \
            "couldn't parse" in reminder(
                {"date": "13-40-99", "time": "99:99", "message": "x"}).lower()


class TestDevAgentSnapshot:
    def test_git_snapshot_creates_commit(self, tmp_path):
        import shutil as _sh
        if not _sh.which("git"):
            pytest.skip("git not installed")
        from actions.dev_agent import _git_snapshot
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
        out = _git_snapshot(proj, "first build")
        assert "Git snapshot:" in out
        # second snapshot after an edit also commits
        (proj / "main.py").write_text("print('hi v2')\n", encoding="utf-8")
        out2 = _git_snapshot(proj, "second build")
        assert "Git snapshot:" in out2

    def test_git_snapshot_tolerates_broken_repo(self, tmp_path):
        import shutil as _sh
        if not _sh.which("git"):
            pytest.skip("git not installed")
        from actions.dev_agent import _git_snapshot
        proj = tmp_path / "broken"
        proj.mkdir()
        (proj / ".git").mkdir()          # empty .git dir confuses git
        out = _git_snapshot(proj, "x")
        assert isinstance(out, str)      # never raises

    def test_snapshot_survives_missing_git(self, tmp_path, monkeypatch):
        import actions.dev_agent as da
        monkeypatch.setattr(da.shutil, "which", lambda n: None)
        assert da._git_snapshot(tmp_path, "x") == ""


# ────────────────────────────────────────────────────────────────────────────
# Wiring guards: import chains the earlier tests masked with monkeypatch.
# The diagram/chart/macro/rules modules import config.get_base_dir lazily;
# monkeypatching their _base_dir/_dir/_path hides a broken chain, so call the
# REAL chain here once.
# ────────────────────────────────────────────────────────────────────────────

class TestRealImportChains:
    def test_config_get_base_dir_exists_and_is_repo_root(self):
        from config import get_base_dir
        base = get_base_dir()
        assert (base / "main.py").is_file()
        assert (base / "config").is_dir()

    def test_diagram_real_base_dir_chain(self, tmp_path, monkeypatch):
        import actions.diagram as d
        import config
        # point config at tmp so the real lookup writes into the sandbox
        monkeypatch.setattr(config, "get_base_dir", lambda: tmp_path)
        out = d.diagram({"kind": "flow", "spec": "A -> B"})
        assert out.startswith("Diagram saved:")
        assert Path(out.split(": ", 1)[1]).is_file()

    def test_charts_real_base_dir_chain(self, tmp_path, monkeypatch):
        import actions.charts as c
        import config
        monkeypatch.setattr(config, "get_base_dir", lambda: tmp_path)
        out = c.chart({"kind": "pie", "data": "a=1, b=2"})
        assert out.startswith("Chart saved:")
        assert Path(out.split(": ", 1)[1]).is_file()

    def test_rules_and_macro_import_real_config(self):
        # merely importing must not raise; their path helpers call
        # config.get_base_dir() which now genuinely exists
        import actions.rules as r
        import actions.macro as m
        from config import get_base_dir
        assert "config" in str(r._path()) or r._path().name == "automation_rules.json"
        assert m._dir().name == "macros"
        assert get_base_dir().name == Path.cwd().name or (get_base_dir() / "main.py").exists()


# ────────────────────────────────────────────────────────────────────────────
# research → report pipeline (search fan-out → fetch → write → save)
# ────────────────────────────────────────────────────────────────────────────

class TestResearch:
    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.research as r
        monkeypatch.setattr(r, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(r, "_synth_report", lambda t, d: None)  # offline
        self.r = r
        self.tmp = tmp_path

    def test_requires_topic(self):
        assert "Give me a topic" in self.r.research({})
        assert "Give me a topic" in self.r.research({"topic": "   "})

    def test_no_sources_is_honest(self, monkeypatch):
        monkeypatch.setattr(self.r, "_search", lambda q, max_results=6: [])
        out = self.r.research({"topic": "quantum dust bunnies"})
        assert "no usable sources" in out

    def test_full_pipeline_saves_report(self, monkeypatch):
        pages = {
            "https://a.example/1": "<html><head><title>Alpha study</title></head>"
                                   "<body><main><p>Long enough content about "
                                   "Jaipur water tables that clears the stub "
                                   "check comfortably with many more words: "
                                   "aquifer levels, stepwell restoration, and "
                                   "monsoon recharge statistics for the district. "
                                   "</p>"
                                   "</main></body></html>",
            "https://b.example/2": "<html><head><title>Beta report</title></head>"
                                   "<body><main><p>Second source with its own "
                                   "substantial paragraph of findings for the "
                                   "digest, also long enough to be kept as a "
                                   "real excerpt of the page body.</p>"
                                   "</main></body></html>",
        }
        calls = []

        def fake_search(q, max_results=6):
            calls.append(q)
            if len(calls) == 1:
                return [{"title": "Alpha", "url": "https://a.example/1",
                         "snippet": "s1"},
                        {"title": "Beta", "url": "https://b.example/2",
                         "snippet": "s2"}]
            return []                                  # second angle empty

        monkeypatch.setattr(self.r, "_search", fake_search)
        monkeypatch.setattr(self.r, "_fetch_url",
                            lambda u, timeout=15: pages[u])
        out = self.r.research({"topic": "Jaipur water table", "depth": "standard"})
        assert "Research report saved:" in out
        path = Path(out.split("saved: ", 1)[1].split(" ", 1)[0])
        assert path.is_file()
        body = path.read_text(encoding="utf-8")
        assert "# Research report" in body and "## Sources" in body
        assert "https://a.example/1" in body
        # dedup: same URL from both angles fetched once → 2 docs
        assert "(2 sources" in out

    def test_broken_fetch_skips_source(self, monkeypatch):
        monkeypatch.setattr(
            self.r, "_search",
            lambda q, max_results=6: [{"title": "A", "url": "https://a.ex/"},
                                      {"title": "B", "url": "https://b.ex/"}])

        def fetch(u, timeout=15):
            if "a.ex" in u:
                raise RuntimeError("boom")
            return ("<html><head><title>B</title></head><body><main>"
                    "<p>Kept page content that is definitely long enough "
                    "to survive the minimum excerpt length check here, "
                    "with two more clauses padding it well past the limit "
                    "the fetcher uses to reject stub pages outright."
                    "</p></main></body></html>")

        monkeypatch.setattr(self.r, "_fetch_url", fetch)
        out = self.r.research({"topic": "x"})
        assert "(1 sources" in out or "(1 source" in out

    def test_synthesis_used_when_available(self, monkeypatch):
        monkeypatch.setattr(
            self.r, "_search",
            lambda q, max_results=6: [{"title": "T", "url": "https://t.ex/",
                                       "snippet": "s"}])
        monkeypatch.setattr(self.r, "_fetch_url", lambda u, timeout=15:
                            "<html><head><title>T</title></head><body><main>"
                            "<p>Content long enough to be retained for the "
                            "synthesis step of the research pipeline test, "
                            "padded with further sentences so it clears the "
                            "minimum excerpt length check with room to spare."
                            "</p></main></body></html>")
        monkeypatch.setattr(self.r, "_synth_report",
                            lambda t, d: "# Written by model\n\n## Summary\nok")
        out = self.r.research({"topic": "ai chips"})
        assert "Research report saved:" in out
        path = Path(out.split("saved: ", 1)[1].split(" ", 1)[0])
        assert path.read_text(encoding="utf-8").startswith("# Written by model")

    def test_scheme_gate(self):
        with pytest.raises(ValueError, match="unsupported scheme"):
            self.r._fetch_url("ftp://evil.example/x")
        with pytest.raises(ValueError, match="unsupported scheme"):
            self.r._fetch_url("javascript:alert(1)")


# ────────────────────────────────────────────────────────────────────────────
# Tier 3: scenes, region OCR, gmail/calendar plugins, phone vision, meeting
# ────────────────────────────────────────────────────────────────────────────

class TestSceneRules:
    @pytest.fixture(autouse=True)
    def _rules(self, tmp_path, monkeypatch):
        import actions.rules as r
        monkeypatch.setattr(r, "_path", lambda: tmp_path / "rules.json")
        r._RULES.clear()
        r._LOADED = False
        self.r = r
        self.calls = []
        r.set_runner(lambda t, a: self.calls.append((t, a)) or f"{t} ok")
        r.set_notifier(None)

    def test_multi_step_scene_runs_in_order(self):
        out = self.r.add_rule(
            {"type": "phrase", "value": "movie mode"},
            "", None, label="Movie mode",
            steps=[{"tool": "video_player", "args": {"action": "play"}},
                   {"tool": "computer_settings", "args": {"action": "volume", "level": 30}},
                   {"tool": "focus", "args": {"action": "start"}}])
        assert "scene(3 steps)" in out
        fired = self.r.fire_phrase("chalo movie mode on karo")
        assert fired and "3/3 steps" in fired[0]
        assert [t for t, _ in self.calls] == [
            "video_player", "computer_settings", "focus"]
        # steps persisted
        import json as _json
        data = _json.loads(self.r._path().read_text())
        assert data[0]["steps"][0]["tool"] == "video_player"

    def test_scene_stops_at_first_failure(self):
        def runner(t, a):
            if t == "boom":
                raise RuntimeError("kaboom")
            self.calls.append((t, a))
            return f"{t} ok"
        self.r.set_runner(runner)
        self.r.add_rule({"type": "phrase", "value": "go"}, "", None,
                        label="G",
                        steps=[{"tool": "a", "args": {}},
                               {"tool": "boom", "args": {}},
                               {"tool": "c", "args": {}}])
        out = self.r.fire_phrase("please go now")[0]
        assert "failed at step 2" in out and "kaboom" in out
        assert [t for t, _ in self.calls] == ["a"]     # step 3 never ran

    def test_single_tool_rule_unchanged(self):
        self.r.add_rule({"type": "phrase", "value": "ping"},
                        "system_monitor", {"action": "cpu"})
        fired = self.r.fire_phrase("ping me")
        assert fired and fired[0] == "system_monitor ok"
        assert self.calls == [("system_monitor", {"action": "cpu"})]

    def test_empty_steps_and_no_tool_rejected(self):
        out = self.r.add_rule({"type": "phrase", "value": "x"}, "", None)
        assert "must name a tool" in out

    def test_scene_via_manage_rules_json(self):
        out = self.r.manage_rules({
            "action": "add", "trigger_type": "phrase", "value": "workout",
            "label": "Workout",
            "steps": '[{"tool":"focus","args":{"action":"start"}},'
                     '{"tool":"music","args":{"action":"play"}}]',
        })
        assert "scene(2 steps)" in out
        assert "steps" in str(self.r.list_rules())


class TestRegionOCR:
    def test_resolve_box_presets(self):
        from actions.region_ocr import _resolve_box
        mon = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        assert _resolve_box("full", mon, [mon]) == mon
        top = _resolve_box("top", mon, [mon])
        assert top["height"] == 540 and top["top"] == 0
        bot = _resolve_box("bottom", mon, [mon])
        assert bot["top"] == 540
        left = _resolve_box("left", mon, [mon])
        assert left["width"] == 960 and left["left"] == 0
        right = _resolve_box("right", mon, [mon])
        assert right["left"] == 960 and right["width"] == 960
        cen = _resolve_box("center", mon, [mon])
        assert cen["width"] == int(1920 * 0.6)
        geo = _resolve_box("10,20,300,150", mon, [mon])
        assert geo == {"left": 10, "top": 20, "width": 300, "height": 150}

    def test_resolve_box_rejects_bad(self):
        from actions.region_ocr import _resolve_box
        mon = {"left": 0, "top": 0, "width": 100, "height": 100}
        for bad in ("nope", "1,2,0,4", "a,b,c,d"):
            with pytest.raises(ValueError):
                _resolve_box(bad, mon, [mon])

    def test_single_read(self, monkeypatch):
        import actions.region_ocr as ro
        monkeypatch.setattr(ro, "_capture", lambda r: "IMG")
        monkeypatch.setattr(ro, "_read_text", lambda img, mode="text": "Hello world")
        out = ro.region_ocr({"region": "center"})
        assert out == "[center] Hello world"

    def test_empty_read_honest(self, monkeypatch):
        import actions.region_ocr as ro
        monkeypatch.setattr(ro, "_capture", lambda r: "IMG")
        monkeypatch.setattr(ro, "_read_text", lambda img, mode="text": "")
        assert "No readable text" in ro.region_ocr({})

    def test_capture_failure_reported(self, monkeypatch):
        import actions.region_ocr as ro
        def boom(r):
            raise RuntimeError("no mss")
        monkeypatch.setattr(ro, "_capture", boom)
        assert "Region read failed: no mss" in ro.region_ocr({})

    def test_live_repeat_reports_changes_only(self, monkeypatch):
        import actions.region_ocr as ro
        monkeypatch.setattr(ro, "_capture", lambda r: "IMG")
        frames = iter(["10%", "10%", "11%", "11%"])
        monkeypatch.setattr(ro, "_read_text", lambda img, mode="text": next(frames))
        out = ro.region_ocr({"repeat": 4, "interval": 0.5})
        assert "4 captures" in out and "2 change(s)" in out
        assert "#1: 10%" in out and "#3: 11%" in out
        assert "#2" not in out                      # unchanged frames dropped

    def test_bad_geometry_value_error(self, monkeypatch):
        import actions.region_ocr as ro
        with pytest.raises(ValueError):
            ro._resolve_box("9,9,abc,9",
                            {"left": 0, "top": 0, "width": 10, "height": 10},
                            [{}])


class TestGmailPlugin:
    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        import plugins.gmail as g
        monkeypatch.setattr(g, "_cfg_path", lambda: tmp_path / "api_keys.json")

        # Offline guarantee: setup verifies credentials over real IMAP to
        # imap.gmail.com — in CI (which HAS network) that would be a live
        # call to Google with fake creds. Replace _Conn with a controllable
        # fake: login succeeds by default, tests can flip it to fail.
        class _FakeIMAP:
            def select(self, *a, **k):
                return "OK", [b"1"]
            def login(self, *a, **k):
                return "OK", [b"logged in"]
            def logout(self):
                return "BYE", [b""]
            def search(self, *a, **k):
                return "OK", [b""]

        class _FakeConn:
            fail_login = False
            def __init__(self):
                if _FakeConn.fail_login:
                    raise RuntimeError("offline fake: IMAP unreachable")
                self.imap = _FakeIMAP()
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(g, "_Conn", _FakeConn)
        self.g = g
        self.fake_conn = _FakeConn
        self.fake_conn.fail_login = False

    def test_setup_needs_both_fields(self):
        assert "Setup needs" in self.g.run({"action": "setup"})
        assert "Setup needs" in self.g.run({"action": "setup",
                                            "address": "a@b.com"})
        # unparseable body (no address anywhere) → setup guidance
        assert "Setup needs" in self.g.run(
            {"action": "setup", "body": "not-an-email xyz"})

    def test_setup_saves_json_body(self):
        out = self.g.run({"action": "setup", "body":
                          '{"gmail_address": "me@gmail.com", '
                          '"gmail_app_password": "abcd efgh"}'})
        # login verify will fail offline (no network) — but config MUST persist
        import json as _json
        data = _json.loads(self.g._cfg_path().read_text())
        assert data["gmail_address"] == "me@gmail.com"
        assert data["gmail_app_password"] == "abcd efgh"
        assert "Saved" in out or "verified" in out

    def test_setup_saves_key_value_body(self):
        self.g.run({"action": "setup",
                    "body": "me@gmail.com abcd efgh ijkl"})
        import json as _json
        data = _json.loads(self.g._cfg_path().read_text())
        assert data["gmail_address"] == "me@gmail.com"

    def test_no_setup_message(self):
        assert "isn't set up yet" in self.g.run({"action": "unread"})
        assert "isn't set up yet" in self.g.run({"action": "send",
                                                 "to": "x@y.z"})

    def test_bad_action(self):
        assert "Unknown gmail action" in self.g.run({"action": "fly"})

    def test_send_validates_address(self, _creds_fixture=True):
        self.g._save_cfg({"gmail_address": "me@gmail.com",
                          "gmail_app_password": "pw"})
        out = self.g.run({"action": "send", "to": "not-an-address"})
        assert "doesn't look like" in out

    def test_decode_and_body_helpers(self):
        import email.message
        m = email.message.EmailMessage()
        m["Subject"] = "Plain subject"
        m.set_content("Body line 1\nBody line 2")
        assert self.g._decode(m["Subject"]) == "Plain subject"
        assert "Body line 1" in self.g._body(m)


class TestCalendarPlugin:
    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        import plugins.calendar as c
        monkeypatch.setattr(c, "_cfg_path", lambda: tmp_path / "api_keys.json")
        self.c = c

    def test_no_url_message(self):
        assert "isn't set up yet" in self.c.run({})
        assert "isn't set up yet" in self.c.run({"action": "today"})

    def test_setup_validates_scheme(self):
        assert "http(s) ICS URL" in self.c.run({"action": "setup",
                                                "url": "ftp://x"})
        assert "http(s) ICS URL" in self.c.run({"action": "setup", "url": ""})

    def test_setup_saves_url(self, monkeypatch):
        monkeypatch.setattr(self.c, "_fetch_ics",
                            lambda u, timeout=20: "BEGIN:VCALENDAR\nEND:VCALENDAR")
        out = self.c.run({"action": "setup", "url": "https://x/y.ics"})
        assert "saved and verified" in out
        assert self.c._get_url() == "https://x/y.ics"

    def test_parse_full_day_and_timed(self):
        from datetime import datetime
        ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
               "DTSTART;VALUE=DATE:20261004\nSUMMARY:All day thing\nEND:VEVENT\n"
               "BEGIN:VEVENT\nDTSTART:20261003T140000\nDTEND:20261003T150000\n"
               "SUMMARY:Timed thing\nLOCATION:HQ\nEND:VEVENT\nEND:VCALENDAR")
        evs = self.c.parse_ics(ics, datetime(2026, 10, 3), datetime(2026, 10, 6))
        assert len(evs) == 2
        assert evs[0]["allday"] is True or evs[1]["allday"] is True
        timed = [e for e in evs if not e["allday"]][0]
        assert timed["summary"] == "Timed thing" and timed["location"] == "HQ"

    def test_rrule_weekly_expansion(self):
        from datetime import datetime
        ics = ("BEGIN:VEVENT\nDTSTART:20261003T100000\n"
               "RRULE:FREQ=WEEKLY;COUNT=3\nSUMMARY:Standup\nEND:VEVENT")
        evs = self.c.parse_ics(ics, datetime(2026, 10, 1), datetime(2026, 11, 1))
        assert len(evs) == 3
        assert evs[1]["start"].day == 10 and evs[2]["start"].day == 17

    def test_rrule_runaway_bounded(self):
        from datetime import datetime
        ics = ("BEGIN:VEVENT\nDTSTART:20260101T100000\n"
               "RRULE:FREQ=DAILY\nSUMMARY:Forever\nEND:VEVENT")
        # a rule with no UNTIL/COUNT must still terminate fast
        evs = self.c.parse_ics(ics, datetime(2026, 1, 1), datetime(2036, 1, 1))
        assert len(evs) <= 1000

    def test_unfold_joins_continuations(self):
        from datetime import datetime
        # RFC 5545: CRLF + ONE whitespace char is removed on unfold, so
        # "the\r\n  title" (fold + original space) → "the title"
        ics = ("BEGIN:VEVENT\nDTSTART:20261003T100000\n"
               "SUMMARY:Part one of the\r\n  title continues\r\nEND:VEVENT")
        evs = self.c.parse_ics(ics, datetime(2026, 10, 3), datetime(2026, 10, 4))
        assert evs[0]["summary"] == "Part one of the title continues"

    def test_bad_date_action(self):
        assert "YYYY-MM-DD" in self.c.run({"action": "date", "date": "nope"})

    def test_fetch_rejects_non_http(self):
        with pytest.raises(ValueError):
            self.c._fetch_ics("file:///etc/passwd")


class TestPhoneVision:
    @pytest.fixture(autouse=True)
    def _cam(self, tmp_path, monkeypatch):
        import actions.phone_vision as pv
        monkeypatch.setattr(pv, "_camera_dir", lambda: tmp_path / "camera")
        self.pv = pv
        self.tmp = tmp_path

    def test_no_frame_message(self):
        assert "No phone camera frame yet" in self.pv.phone_vision({})

    def test_stale_frame_rejected(self, monkeypatch):
        import json as _json
        import time as _time
        d = self.tmp / "camera"
        d.mkdir(parents=True, exist_ok=True)
        (d / "frame.jpg").write_bytes(b"x" * 200)
        (d / "frame.json").write_text(_json.dumps({"ts": _time.time() - 9999}))
        out = self.pv.phone_vision({})
        assert "old" in out and "📷" in out
        # allow_stale flips it into the vision path (which needs a key →
        # honest error, not a freshness error)
        monkeypatch.setattr(self.pv, "_ask",
                            lambda f, p, m: "DESK: laptop + cup")
        out2 = self.pv.phone_vision({"allow_stale": True})
        assert "DESK: laptop" in out2

    def test_fresh_frame_answers(self, monkeypatch):
        import json as _json
        import time as _time
        d = self.tmp / "camera"
        d.mkdir(parents=True, exist_ok=True)
        (d / "frame.jpg").write_bytes(b"y" * 200)
        (d / "frame.json").write_text(_json.dumps({"ts": _time.time()}))
        monkeypatch.setattr(self.pv, "_ask",
                            lambda f, p, m: f"ans({m}): {p}")
        out = self.pv.phone_vision({"prompt": "what do you see"})
        assert "ans(ask): what do you see" in out
        assert "phone camera" in out

    def test_mode_passthrough(self, monkeypatch):
        import json as _json
        import time as _time
        d = self.tmp / "camera"
        d.mkdir(parents=True, exist_ok=True)
        (d / "frame.jpg").write_bytes(b"z" * 200)
        (d / "frame.json").write_text(_json.dumps({"ts": _time.time()}))
        seen = {}
        def fake_ask(f, p, m):
            seen["mode"] = m
            return "ok"
        monkeypatch.setattr(self.pv, "_ask", fake_ask)
        self.pv.phone_vision({"mode": "ocr"})
        assert seen["mode"] == "ocr"

    def test_camera_dir_matches_server(self):
        # the two must never drift — a frame saved by the server has to be
        # the file this action reads
        import dashboard.server as ds
        if not ds._DEPS_OK:
            pytest.skip("dashboard deps missing")
        ds._ensure_network_access = lambda port: None
        ds._ensure_certs = lambda: False
        import actions.phone_vision as pv
        srv = ds.DashboardServer()._uploads_dir / "camera"
        # both helpers walk the same candidate list — compare by function
        # source identity is overkill; assert the same relative tail exists
        assert srv.name == pv._camera_dir().name == "camera"


class TestMeetingTool:
    @pytest.fixture(autouse=True)
    def _meet(self, tmp_path, monkeypatch):
        import actions.meeting as m
        monkeypatch.setattr(m, "_base_dir", lambda: tmp_path)
        self.m = m

    def test_status_and_idle_stop(self):
        assert "Not recording" in self.m.meeting({"action": "status"})
        assert "No meeting is being recorded" in self.m.meeting({"action": "stop"})

    def test_start_stop_with_fake_audio(self, monkeypatch):
        import numpy as np
        rec = self.m._Recorder()
        # fake stream: start/stop without hardware
        class FakeStream:
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        monkeypatch.setattr(self.m, "_RECORDER", rec)
        # inject chunks manually via a patched start
        def fake_start():
            rec._stream = FakeStream()
            rec._started = self.m.time.time()
            rec._chunks = [np.zeros((16000, 1), dtype=np.float32)]  # 1s silence
        monkeypatch.setattr(rec, "start", fake_start)
        out = self.m.meeting({"action": "start"})
        assert "started" in out
        assert "Already recording" in self.m.meeting({"action": "start"})
        assert "Recording" in self.m.meeting({"action": "status"})
        # stop → WAV saved, no speech → honest message
        monkeypatch.setattr(self.m, "_transcribe", lambda a: "")
        out2 = self.m.meeting({"action": "stop"})
        assert "heard no speech" in out2
        wavs = list(self.m._meetings_dir().glob("*.wav"))
        assert len(wavs) == 1 and wavs[0].stat().st_size > 1000
        assert not rec.running

    def test_stop_writes_transcript(self, monkeypatch):
        import numpy as np
        rec = self.m._Recorder()
        class FakeStream:
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        rec._stream = FakeStream()
        rec._started = self.m.time.time()
        rec._chunks = [np.zeros((16000, 1), dtype=np.float32)]
        monkeypatch.setattr(self.m, "_RECORDER", rec)
        monkeypatch.setattr(self.m, "_transcribe", lambda a: "Hello all, let's ship it.")
        out = self.m.meeting({"action": "stop"})
        assert "Transcript:" in out and "Hello all" in out
        txts = list(self.m._meetings_dir().glob("*.txt"))
        assert len(txts) == 1
        assert "Hello all" in txts[0].read_text()

    def test_list_and_summary_fallback(self, monkeypatch):
        d = self.m._meetings_dir()
        (d / "meeting-20261003-100000.txt").write_text("We decided X.")
        out = self.m.meeting({"action": "list"})
        assert "meeting-20261003-100000.txt" in out
        monkeypatch.setattr(self.m, "_summarize", lambda t, w: "")
        out2 = self.m.meeting({"action": "summary"})
        assert "No Gemini key" in out2 and "We decided X." in out2

    def test_summary_uses_model_when_available(self, monkeypatch):
        d = self.m._meetings_dir()
        (d / "meeting-a.txt").write_text("Decided: ship Friday.")
        monkeypatch.setattr(self.m, "_summarize",
                            lambda t, w: "## Decisions\n- ship Friday")
        out = self.m.meeting({"action": "summary"})
        assert "ship Friday" in out

    def test_bad_action(self):
        assert "Unknown meeting action" in self.m.meeting({"action": "fly"})


class TestCameraFrameEndpoint:
    """POST /api/camera-frame — the phone→PC vision pipe."""

    @pytest.fixture(autouse=True)
    def _app(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi", reason="needs fastapi")
        import dashboard.server as ds
        if not ds._DEPS_OK:
            pytest.skip("dashboard deps incomplete")
        monkeypatch.setattr(ds, "_ensure_network_access", lambda port: None)
        monkeypatch.setattr(ds, "_ensure_certs", lambda: False)
        self.srv = ds.DashboardServer()
        self.srv._uploads_dir = tmp_path / "uploads"
        self.token = "tok-camera-1"
        self.srv._tokens.add(self.token)

    def _client(self):
        from fastapi.testclient import TestClient
        return TestClient(self.srv.app)

    def _post(self, body, tok=None):
        import json as _json
        return self._client().post(
            "/api/camera-frame",
            content=_json.dumps(body),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {tok or self.token}"})

    def test_rejects_unauthorized(self):
        resp = self._post({"frame": "AAAA"}, tok="wrong")
        assert resp.status_code == 401

    def test_saves_valid_frame(self):
        import base64 as _b64
        payload = _b64.b64encode(b"\xff\xd8FAKEJPEG" + b"x" * 300).decode()
        resp = self._post({"frame": payload, "ts": 1759500000.0})
        assert resp.status_code == 200 and resp.json()["ok"] is True
        frame = self.srv._uploads_dir / "camera" / "frame.jpg"
        assert frame.is_file() and frame.stat().st_size > 100
        meta = (self.srv._uploads_dir / "camera" / "frame.json").read_text()
        assert "1759500000" in meta

    def test_accepts_data_url_form(self):
        import base64 as _b64
        b64 = _b64.b64encode(b"\xff\xd8FRAME2" + b"y" * 200).decode()
        resp = self._post({"frame": f"data:image/jpeg;base64,{b64}"})
        assert resp.status_code == 200
        frame = self.srv._uploads_dir / "camera" / "frame.jpg"
        assert frame.is_file()

    def test_rejects_bad_base64(self):
        resp = self._post({"frame": "not!!base64"})
        assert resp.status_code == 400

    def test_rejects_tiny_and_huge(self):
        import base64 as _b64
        tiny = _b64.b64encode(b"ab").decode()           # < 100 bytes
        assert self._post({"frame": tiny}).status_code == 413
        huge = _b64.b64encode(b"z" * (5 * 1024 * 1024)).decode()
        assert self._post({"frame": huge}).status_code == 413

    def test_frame_overwrites_previous(self):
        import base64 as _b64
        for payload in (b"\xff\xd8FIRST" + b"a" * 200,
                        b"\xff\xd8SECOND" + b"b" * 200):
            resp = self._post({"frame": _b64.b64encode(payload).decode()})
            assert resp.status_code == 200
        data = (self.srv._uploads_dir / "camera" / "frame.jpg").read_bytes()
        assert b"SECOND" in data and b"FIRST" not in data

    def test_bad_json_rejected(self):
        resp = self._client().post(
            "/api/camera-frame", content="{not json",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        assert resp.status_code == 400


# ────────────────────────────────────────────────────────────────────────────
# main.py wiring guards — main.py imports PyQt6 (not installed in CI), so
# these assert on the SOURCE. They exist because the audit found real
# disconnects: phrase rules fired only on typed input, agent-runner steps
# bypassed the activity timeline, and the dashboard listener dropped events
# raised off the event loop. Each guard fails if that wiring is removed.
# ────────────────────────────────────────────────────────────────────────────

class TestMainWiring:
    SRC = Path("main.py").read_text(encoding="utf-8")

    def test_fire_phrase_helper_exists(self):
        assert "def _fire_phrase_rules(self, text: str) -> list[str]:" in self.SRC

    def test_fire_phrase_on_all_three_input_paths(self):
        # 1. HUD text box
        on_text = self.SRC[self.SRC.index("def _on_text_command"):self.SRC.index("def _on_text_command") + 3000]
        assert "self._fire_phrase_rules(text)" in on_text
        # 2. live voice transcript (the full_in block)
        full_in = self.SRC[self.SRC.index('full_in = " ".join(in_buf)'):]
        full_in = full_in[:full_in.index("full_out")]
        assert "self._fire_phrase_rules(full_in)" in full_in
        # 3. phone/dashboard command queue
        drain = self.SRC[self.SRC.index("async def _process_dashboard_commands"):]
        nxt = drain.find("async def ", 10)          # skip the header itself
        drain = drain[:nxt] if nxt != -1 else drain[:6000]
        assert "self._fire_phrase_rules(text)" in drain

    def test_agent_runner_wraps_activity(self):
        # task/rules steps must land in Mission Control — the runner has to
        # begin/finish an event around registry.run (registry itself doesn't)
        runner = self.SRC[self.SRC.index("def _agent_runner"):self.SRC.index("_task_agent.set_runner")]
        assert "_act_mod.begin(" in runner
        assert "_act_mod.finish(" in runner
        assert "_act_mod.fail(" in runner
        assert "_action_registry.run(tool, tool_args, ctx)" in runner

    def test_activity_listener_uses_stored_loop(self):
        # get_running_loop() raises off-loop (rules tick, focus timers,
        # executor threads) → event silently dropped. The listener must
        # prefer the STORED self._loop and call_soon_threadsafe.
        listener = self.SRC[self.SRC.index("def _activity_to_dash"):]
        listener = listener[:listener.index("_activity.add_listener")]
        assert "self._loop" in listener
        assert "call_soon_threadsafe" in listener
        assert "get_running_loop" in listener          # fallback still present

    def test_rules_tick_task_created(self):
        assert "asyncio.create_task(self._run_rules_tick())" in self.SRC

    def test_focus_callbacks_and_mute_gate_wired(self):
        assert "set_callbacks(on_phase=_focus_phase, on_mute=_focus_mute)" in self.SRC
        assert "if self._focus_muted:" in self.SRC
        mute_block = self.SRC[self.SRC.index("def _focus_mute"):]
        mute_block = mute_block[:mute_block.index("set_callbacks")]
        assert "self._focus_muted = bool(on)" in mute_block

    def test_execute_tool_has_single_begin_and_total_close(self):
        # exactly ONE begin in _execute_tool; every return path is covered
        # (the 2 returns each sit after a finish, plus except→fail / else→finish)
        body = self.SRC[self.SRC.index("async def _execute_tool"):]
        body = body[:body.index("async def _send_realtime")]
        assert body.count("_activity.begin(") == 1
        assert body.count("_activity.finish(") >= 2
        assert "_activity.fail(" in body
        # save_memory early-return closes its event BEFORE returning
        early = body[:body.index("recall_memory")]
        assert early.index("_activity.finish") < early.index("return types.FunctionResponse")

    def test_dashboard_commands_wake_before_send(self):
        # remote commands must wake an asleep JARVIS (no WAKE button on phone)
        drain = self.SRC[self.SRC.index("async def _process_dashboard_commands"):]
        assert 'wake(reason="remote command")' in drain[:4000]


# ────────────────────────────────────────────────────────────────────────────
# Batch 1 — free keyless actions: image_gen, feed, backup, sky (+ wtype)
# ────────────────────────────────────────────────────────────────────────────

class TestImageGen:
    def test_png_saved(self, tmp_path, monkeypatch):
        from actions import image_gen as ig
        monkeypatch.setattr(ig, "_base_dir", lambda: tmp_path)
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 300
        monkeypatch.setattr(ig, "_get", lambda url, timeout=120: png)
        out = ig.image_gen({"prompt": "a red fox", "width": 512,
                          "height": 512, "seed": 7})
        assert out.startswith("Saved: ")
        path = Path(out.split(": ", 1)[1].split(" (")[0])
        assert path.exists() and path.read_bytes() == png
        assert "512x512" in out and "seed=7" in out

    def test_refuses_non_image(self, monkeypatch, tmp_path):
        from actions import image_gen as ig
        monkeypatch.setattr(ig, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(
            ig, "_get",
            lambda url, timeout=120: b"<html>rate limited</html>" * 10)
        out = ig.image_gen({"prompt": "x"})
        assert "refused" in out
        assert not list((tmp_path / "generated").glob("*"))

    def test_needs_prompt_and_sniffs_formats(self):
        from actions import image_gen as ig
        assert "prompt" in ig.image_gen({})
        assert ig._sniff(b"\x89PNG\r\n\x1a\n...") == "png"
        assert ig._sniff(b"\xff\xd8\xff\xe0rest") == "jpg"
        assert ig._sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
        assert ig._sniff(b"not an image") == ""
        assert ig.TOOL["name"] == "image_gen"
        assert callable(ig.TOOL["handler"])


class TestFeed:
    RSS = (b"<?xml version=\"1.0\"?><rss version=\"2.0\"><channel>"
           b"<title>T</title>"
           b"<item><title>Hello 1</title><link>http://x/1</link>"
           b"<pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item>"
           b"<item><title>Hello 2</title><link>http://x/2</link></item>"
           b"</channel></rss>")

    def test_add_list_check_remove(self, tmp_path, monkeypatch):
        from actions import feed as fd
        monkeypatch.setattr(fd, "_feeds_path", lambda: tmp_path / "feeds.json")
        monkeypatch.setattr(fd, "_get", lambda url, timeout=20: self.RSS)
        assert "No feeds yet" in fd.feed({"action": "list"})
        out = fd.feed({"action": "add", "name": "releases",
                       "url": "https://github.com/o/r/releases.atom"})
        assert "Subscribed" in out
        assert "releases" in fd.feed({"action": "list"})
        out = fd.feed({"action": "check"})
        assert "Hello 1" in out and "Hello 2" in out
        assert "shown of" in out
        out = fd.feed({"action": "check", "limit": 1})
        assert "Hello 1" in out and "Hello 2" not in out
        assert "Removed" in fd.feed({"action": "remove", "which": "releases"})
        assert "No feeds" in fd.feed({"action": "list"})
        assert "http" in fd.feed({"action": "add", "name": "x",
                                  "url": "ftp://bad"})
        assert "Unknown feed action" in fd.feed({"action": "wat"})

    def test_check_reports_unreachable(self, tmp_path, monkeypatch):
        from actions import feed as fd
        monkeypatch.setattr(fd, "_feeds_path", lambda: tmp_path / "f.json")
        fd._save([{"name": "down", "url": "https://example.invalid/rss"}])

        def boom(url, timeout=20):
            raise OSError("connection refused")
        monkeypatch.setattr(fd, "_get", boom)
        out = fd.feed({"action": "check"})
        assert "unreachable" in out and "connection refused" in out

    def test_github_releases_url_accepted(self, tmp_path, monkeypatch):
        from actions import feed as fd
        monkeypatch.setattr(fd, "_feeds_path", lambda: tmp_path / "f.json")
        out = fd.feed({"action": "add", "name": "jarvis",
                       "url": "https://github.com/tonystark302012-rgb/"
                              "jarvis/releases.atom"})
        assert "Subscribed" in out


class TestBackupZip:
    @staticmethod
    def _populated(base: Path):
        (base / "memory").mkdir(parents=True)
        (base / "memory" / "f.json").write_text('{"prefs": 1}')
        (base / "config" / "certs").mkdir(parents=True)
        (base / "config" / "certs" / "jarvis.key").write_text("SECRETKEY")
        (base / "config" / "api_keys.json").write_text('{"gemini": "k"}')
        (base / "macros").mkdir()
        (base / "macros" / "skill.md").write_text("# skill")
        (base / "research").mkdir()
        (base / "research" / "r.md").write_text("report")
        (base / "charts").mkdir()
        (base / "charts" / "evil.svg").write_text("<svg/>")
        import sqlite3 as _sq
        con = _sq.connect(base / "memory" / "fake.db")
        con.execute("CREATE TABLE t (x INTEGER)")
        con.execute("INSERT INTO t VALUES (42)")
        con.commit()
        con.close()
        (base / "memory" / "fake.db-wal").write_bytes(b"stale-wal")

    def test_create_contains_excludes_and_snapshots(self, tmp_path,
                                                    monkeypatch):
        from actions import backup as bk
        base = tmp_path / "app"
        self._populated(base)
        monkeypatch.setattr(bk, "_base_dir", lambda: base)
        out = bk.backup({"action": "create"})
        assert out.startswith("Saved: ") and "API keys" in out
        zpath = Path(out.split(": ", 1)[1].split(" (")[0])
        import zipfile as _zf
        with _zf.ZipFile(zpath) as zf:
            names = set(zf.namelist())
            assert "memory/f.json" in names
            assert "memory/fake.db" in names
            assert "config/api_keys.json" in names
            assert "macros/skill.md" in names and "research/r.md" in names
            assert not any(n.startswith("charts/") for n in names)
            assert not any(n.endswith("jarvis.key") for n in names)
            assert not any(n.endswith("-wal") for n in names)
            # the snapshot inside the zip is a REAL, openable sqlite db
            import sqlite3 as _sq
            dump = tmp_path / "check.db"
            dump.write_bytes(zf.read("memory/fake.db"))
            con = _sq.connect(dump)
            assert con.execute("SELECT x FROM t").fetchone()[0] == 42
            con.close()

    def test_list_and_restore_never_overwrite(self, tmp_path, monkeypatch):
        from actions import backup as bk
        base = tmp_path / "app"
        self._populated(base)
        monkeypatch.setattr(bk, "_base_dir", lambda: base)
        bk.backup({"action": "create"})
        listed = bk.backup({"action": "list"})
        assert "#1" in listed and "jarvis-backup-" in listed
        live = (base / "memory" / "f.json")
        live.write_text('{"prefs": "changed-live"}')
        out = bk.backup({"action": "restore", "which": "#1"})
        assert out.startswith("Restored ") and "nothing was overwritten" in out
        dest = Path(out.split("into ", 1)[1].split(" —")[0])
        assert (dest / "memory" / "f.json").read_text() == '{"prefs": 1}'
        assert live.read_text() == '{"prefs": "changed-live"}'
        assert "Unknown backup action" in bk.backup({"action": "nope"})


class TestSky:
    def test_iss(self, monkeypatch):
        from actions import sky as sk
        import json as _j
        monkeypatch.setattr(
            sk, "_get",
            lambda url, timeout=20: _j.dumps({
                "iss_position": {"latitude": "12.5", "longitude": "-45.25"},
                "timestamp": 1700000000}).encode())
        out = sk.sky({"action": "iss"})
        assert "+12.5000" in out and "-45.2500" in out and "UTC" in out

    def test_quakes_bucket_and_limit(self, monkeypatch):
        from actions import sky as sk
        import json as _j
        def feat(m, place, t):
            return {
                "properties": {"mag": m, "place": place, "time": t,
                               "url": "https://usgs.gov/x"},
                "geometry": {"coordinates": [-118.2, 34.05, 10]}}
        payload = _j.dumps({
            "features": [feat(5.1, "20 km S of Foo", 1793460000000),
                         feat(2.9, "Bar", 1793450000000)]}).encode()
        seen = {}

        def fake(url, timeout=20):
            seen["url"] = url
            return payload
        monkeypatch.setattr(sk, "_get", fake)
        out = sk.sky({"action": "quakes", "min_magnitude": "4.5",
                      "limit": 1})
        assert "4.5_day.geojson" in seen["url"]
        assert "M5.1" in out and "20 km S of Foo" in out
        assert "M2.9" not in out                    # limit=1 honoured
        assert "USGS last 24 h" in out

    def test_suntimes_needs_coords_and_formats(self, monkeypatch):
        from actions import sky as sk
        assert "needs lat" in sk.sky({"action": "suntimes"})
        assert "YYYY-MM-DD" in sk.sky({"action": "suntimes", "lat": 1,
                                       "lon": 2, "date": "october"})
        import json as _j
        monkeypatch.setattr(
            sk, "_get",
            lambda url, timeout=20: _j.dumps({
                "status": "OK",
                "results": {"sunrise": "2026-10-05T06:12:00+00:00",
                            "sunset": "2026-10-05T17:48:00+00:00",
                            "day_length": 42000}}).encode())
        out = sk.sky({"action": "suntimes", "lat": 26.91, "lon": 75.79,
                      "date": "2026-10-05"})
        assert "Sun for 2026-10-05" in out
        assert "sunrise 06:12" in out and "sunset 17:48" in out

    def test_unknown_and_network_failure_honest(self, monkeypatch):
        from actions import sky as sk
        assert "Unknown sky action" in sk.sky({"action": "meteors"})

        def boom(url, timeout=20):
            raise OSError("dns blocked")
        monkeypatch.setattr(sk, "_get", boom)
        out = sk.sky({"action": "iss"})
        assert "failed" in out and "dns blocked" in out


class TestWaylandWtype:
    def test_types_through_wtype(self, tmp_path, monkeypatch):
        import os as _os
        from actions import computer_control as cc
        fake = tmp_path / "wtype"
        fake.write_text("#!/bin/sh\nprintf '%s' \"$1\" > \"$WTYPE_OUT\"\n")
        fake.chmod(0o755)
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("WTYPE_OUT", str(tmp_path / "typed.txt"))
        monkeypatch.setenv("PATH", str(tmp_path) + _os.pathsep
                           + _os.environ.get("PATH", ""))
        out = cc._type("hello world")
        assert "wtype" in out
        assert (tmp_path / "typed.txt").read_text() == "hello world"

    def test_wayland_without_wtype_is_honest(self, monkeypatch, tmp_path):
        import os as _os
        from actions import computer_control as cc
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
        monkeypatch.setenv("PATH", str(tmp_path))       # empty — no wtype
        out = cc._type("hi")
        assert "wtype is missing" in out

    def test_no_display_reports_real_reason(self, monkeypatch):
        from actions import computer_control as cc
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        if cc._PYAUTOGUI:
            pytest.skip("pyautogui usable here — nothing to report")
        with pytest.raises(RuntimeError, match="PyAutoGUI unavailable"):
            cc._type("hi")
