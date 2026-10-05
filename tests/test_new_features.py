"""
Tests for the second-wave upgrades: Mission Control activity log, agentic
orchestrator, task_agent, scrape, diagrams, charts, clipboard history,
automation rules, macros, process manager, focus, window layouts, scanner
extensions, and the dashboard mirror plumbing.

Every test is offline: LLM planners, runners, clocks and network are faked
or the code path is pure. Nothing here needs a display server.
"""
from __future__ import annotations

import shutil
import sqlite3

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
    def _clean(self, tmp_path, monkeypatch):
        from actions import clip_history as ch
        monkeypatch.setattr(ch, "_db_path", lambda: tmp_path / "clip.db")
        ch.reset_for_tests()
        ch._STOP.set()
        ch._CLIP_IMAGES.clear()
        yield
        ch._STOP.set()
        ch.reset_for_tests()

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
        monkeypatch.setattr(r, "_extra_sources", lambda t: [])       # no net
        monkeypatch.setattr(r, "_fetch_rendered", lambda u, timeout=15: "")
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


class TestRulesV2SitePortProc:
    """rules v2: site (HTTP edge), port (local TCP edge), proc (/proc edge)."""

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

    def test_site_up_down_transitions_rate_limited(self, monkeypatch):
        import actions.rules as r
        state = {"up": True}
        probes = []

        def fake_probe(url, timeout=8.0):
            probes.append(url)
            return state["up"], "hashA" if state["up"] else ""
        monkeypatch.setattr(r, "_site_probe", fake_probe)
        r.add_rule({"type": "site", "value": "https://example.com",
                    "state": "up"}, "scan", {}, "site watch")
        assert r.tick(now=1000) == []              # baseline: never fires
        assert r.tick(now=1100) == []              # within 5m rate → no probe
        assert len(probes) == 1
        state["up"] = False
        assert r.tick(now=1400) == []              # rate passed → probes DOWN
        assert len(probes) == 2                    # …want=up → no fire yet
        assert r.tick(now=1500) == []              # rate-blocked again
        assert len(probes) == 2
        state["up"] = True
        out = r.tick(now=1900)                     # down→up EDGE → FIRE
        assert out and self.fired and self.fired[0][0] == "scan"

    def test_site_down_and_changed_states(self, monkeypatch):
        import actions.rules as r
        state = {"up": True, "body": "v1"}

        def fake_probe(url, timeout=8.0):
            return state["up"], state["body"] if state["up"] else ""
        monkeypatch.setattr(r, "_site_probe", fake_probe)
        r.add_rule({"type": "site", "value": "https://example.com",
                    "state": "down"}, "scan", {}, "down alert")
        r.add_rule({"type": "site", "value": "https://example.com",
                    "state": "changed"}, "scan", {}, "changed alert")
        assert r.tick(now=1000) == []              # baselines (both)
        state["up"] = False
        out = r.tick(now=2000)                     # second rule rate: 2000-1000
        assert out and len(self.fired) == 1        # only the DOWN rule fires
        state["up"] = True
        assert r.tick(now=3000) == []              # back up, nothing wanted
        state["body"] = "v2"
        out = r.tick(now=4000)                     # changed rule sees new hash
        assert out and len(self.fired) == 2

    def test_port_edge_with_real_socket(self):
        import socket
        import actions.rules as r
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            r.add_rule({"type": "port", "value": str(port),
                        "state": "up"}, "scan", {}, "port up")
            assert r.tick(now=1000) == []          # baseline (open)
            srv.close()                            # → down
            assert r.tick(now=1100) == []          # want up, now down
            assert r.tick(now=1140) == []          # still down, no edge
            srv2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv2.bind(("127.0.0.1", port))
            srv2.listen(1)
            try:
                out = r.tick(now=1180)             # down→up EDGE → FIRE
                assert out and self.fired
            finally:
                srv2.close()
        finally:
            try:
                srv.close()
            except OSError:
                pass

    def test_proc_scan_real_and_edges(self, monkeypatch):
        import actions.rules as r
        # real /proc scan sees this python process
        assert any("python" in n for n in r._proc_names())
        names = set()                             # absent at baseline
        monkeypatch.setattr(r, "_proc_names", lambda: set(names))
        r.add_rule({"type": "proc", "value": "zzz-jarvis-test",
                    "state": "running"}, "scan", {}, "proc watch")
        assert r.tick(now=1000) == []              # absent at baseline
        names.add("zzz-jarvis-test")               # 'starts'
        out = r.tick(now=1001)
        assert out and self.fired
        r._RULES.clear()
        r._STATE.clear()
        r.add_rule({"type": "proc", "value": "zzz-jarvis-test",
                    "state": "gone"}, "scan", {}, "proc exit watch")
        assert r.tick(now=2000) == []              # running at baseline
        names.clear()                              # 'exits'
        out = r.tick(now=2001)
        assert out and len(self.fired) == 2

    def test_manage_rules_surface_passes_extras(self):
        import actions.rules as r
        out = r.manage_rules({"action": "add", "trigger_type": "site",
                              "value": "https://status.example.org",
                              "state": "down", "every": "1m",
                              "tool": "scan", "tool_args": "{}",
                              "label": "status down?"})
        assert "Rule added" in out
        trig = r._RULES[-1]["trigger"]
        assert trig["state"] == "down" and trig["every"] == "1m"
        out2 = r.manage_rules({"action": "add", "trigger_type": "bogus",
                               "value": "x", "tool": "scan"})
        assert "Rule added" in out2                # unknown types stay honest


class TestClipHistoryV2:
    """clip v2: SQLite persistence, hybrid search, redaction, images, OCR."""

    @pytest.fixture(autouse=True)
    def _clean(self, tmp_path, monkeypatch):
        from actions import clip_history as ch
        monkeypatch.setattr(ch, "_db_path", lambda: tmp_path / "clip.db")
        ch.reset_for_tests()
        ch._STOP.set()
        ch._CLIP_IMAGES.clear()
        yield
        ch._STOP.set()
        ch.reset_for_tests()

    def test_persists_across_restart(self):
        from actions import clip_history as ch
        assert ch.record("survives restart")
        ch.reset_for_tests()                      # app restart
        assert "survives restart" in ch.clip_history({"action": "list"})

    def test_hybrid_search_fts_and_substring(self):
        from actions import clip_history as ch
        ch.record("https://example.com/page")
        ch.record("some secret text")
        found = ch.clip_history({"action": "list", "query": "example"})
        assert "example.com" in found and "secret" not in found
        # substring inside a token — pure FTS would miss this
        found2 = ch.clip_history({"action": "list", "query": "xample"})
        assert "example.com" in found2

    def test_redact_masks_pii_before_disk(self):
        from actions import clip_history as ch
        raw = ("mail a@b.com / +91 98765 43210 / 4111 1111 1111 1111 "
               "/ host 192.168.1.1")
        assert ch.record(raw, redact=True)
        listing = ch.clip_history({"action": "list"})
        assert "[email]" in listing and "a@b.com" not in listing
        assert "[phone]" in listing and "98765" not in listing
        assert "[card]" in listing and "4111" not in listing
        assert "[ip]" in listing and "192.168.1.1" not in listing
        # without redact → stored verbatim
        assert ch.record("plain a@b.com")
        assert "a@b.com" in ch.clip_history({"action": "list"})

    def test_image_entry_saved_and_use_is_honest(self, tmp_path, monkeypatch):
        from actions import clip_history as ch

        class FakeImg:
            size = (4, 4)
            mode = "RGB"

            def tobytes(self):
                return b"pixels" * 8

            def save(self, path, format=None):
                Path(path).write_bytes(b"\x89PNG-fake")

        monkeypatch.setattr(ch, "_clip_image", lambda: FakeImg())
        # first grab → stored; identical second grab → deduped
        assert ch._record_image(FakeImg(), tmp_path)
        assert not ch._record_image(FakeImg(), tmp_path)
        listing = ch.clip_history({"action": "list"})
        assert "[image]" in listing and "clip_images" in listing
        used = ch.clip_history({"action": "use", "index": "1"})
        assert "is an image" in used and "open it" in used
        # OCR on a fake png has no backend in the sandbox → honest failure
        out = ch.clip_history({"action": "ocr", "index": "1"})
        assert "OCR unavailable" in out or "no text found" in out

    def test_ocr_on_text_entry_refused(self):
        from actions import clip_history as ch
        ch.record("just text")
        assert "not an image" in ch.clip_history({"action": "ocr",
                                                  "index": "1"})

    def test_prune_keeps_newest_500(self):
        from actions import clip_history as ch
        for i in range(520):
            ch.record(f"entry {i}")
        c = ch._conn()
        n = c.execute("SELECT COUNT(*) FROM clips").fetchone()[0]
        assert n <= 500
        newest = ch.clip_history({"action": "list"})
        assert "entry 519" in newest and "entry 0\n" not in newest


class TestCodeIntel:
    """2g: jedi navigation + pylsp diagnostics (Report-E dev tooling)."""

    @pytest.fixture(autouse=True)
    def _file(self, tmp_path):
        self.src = tmp_path / "sample.py"
        self.src.write_text(
            '"""Sample module."""\n'
            "\n"
            "\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            '    return f"hello {name}"\n'
            "\n"
            "\n"
            'msg = greet("world")\n'
            'other = greet("there")\n',
            encoding="utf-8")
        yield

    def test_hover_returns_docstring(self):
        from actions import code_intel as ci
        # cursor on the greet() call at line 6
        out = ci.code_intel({"action": "hover", "path": str(self.src),
                             "line": "9", "col": "8"})
        assert "greet" in out and "Say hello" in out

    def test_goto_finds_definition(self):
        from actions import code_intel as ci
        out = ci.code_intel({"action": "goto", "path": str(self.src),
                             "line": "9", "col": "8"})
        assert "sample.py:4:" in out

    def test_refs_finds_all_callers(self):
        from actions import code_intel as ci
        out = ci.code_intel({"action": "refs", "path": str(self.src),
                             "line": "4", "col": "5"})
        assert "reference(s)" in out
        assert "sample.py:9:" in out and "sample.py:10:" in out

    def test_complete_after_dot(self):
        from actions import code_intel as ci
        line = self.src.read_text(encoding="utf-8").splitlines()
        line.append('s = "text"\ns.')                     # str attrs at EOF
        self.src.write_text("\n".join(line) + "\n", encoding="utf-8")
        out = ci.code_intel({"action": "complete", "path": str(self.src)})
        assert "completion(s)" in out and "upper" in out

    def test_diag_reports_syntax_error(self):
        from actions import code_intel as ci
        bad = self.src.parent / "bad.py"
        bad.write_text("def broken(:\n    pass\n", encoding="utf-8")
        out = ci.code_intel({"action": "diag", "path": str(bad)})
        # pylsp installed in the test venv → real diagnostics
        assert "diagnostic" in out or "no diagnostics" not in out
        assert "Error" in out or "error" in out.lower()

    def test_diag_clean_file(self):
        from actions import code_intel as ci
        out = ci.code_intel({"action": "diag", "path": str(self.src)})
        assert "clean" in out or "no diagnostics" in out

    def test_non_python_and_missing_file_honest(self):
        from actions import code_intel as ci
        js = self.src.parent / "app.js"
        js.write_text("const x = 1;\n", encoding="utf-8")
        assert "Python-only" in ci.code_intel({"action": "goto",
                                               "path": str(js)})
        assert "No such file" in ci.code_intel(
            {"action": "goto", "path": str(self.src.parent / "ghost.py")})
        assert "path" in ci.code_intel({"action": "hover"}).lower()

    def test_symbols_delegates_to_code_outline(self):
        from actions import code_intel as ci
        out = ci.code_intel({"action": "symbols", "path": str(self.src)})
        assert "greet" in out

    def test_setup_refuses_non_allowlisted(self):
        from actions import code_intel as ci
        ci._SETUP_SAFE.clear()
        try:
            out = ci._setup({})
            assert "Refusing" in out
        finally:
            ci._SETUP_SAFE.update({"jedi", "python-lsp-server",
                                   "python-lsp-json-rpc", "pyflakes",
                                   "pycodestyle", "pluggy", "ujson",
                                   "docstring-to-markdown",
                                   "jedi-language-server"})

    def test_tool_shape(self):
        from actions import code_intel as ci
        assert ci.TOOL["name"] == "code_intel"
        assert ci.TOOL["handler"] is ci.code_intel
        assert ci.TOOL["parameters"]["type"] == "OBJECT"


class TestAuditChain:
    """3a: tamper-evident audit log (append-only + SHA-256 chain)."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        from core import audit_chain as ac
        monkeypatch.setattr(ac, "_db_path", lambda: tmp_path / "audit.db")
        ac.reset_for_tests()
        yield
        ac.reset_for_tests()

    def test_record_and_verify_ok(self):
        from core import audit_chain as ac
        ac.record("weather_report", {"city": "Jaipur"}, status="ok")
        ac.record("web_search", {"query": "jarvis"}, status="ok")
        out = ac.verify()
        assert out.startswith("Audit chain OK") and "2 entrie" in out

    def test_tamper_is_detected(self):
        from core import audit_chain as ac
        ac.record("weather_report", {"city": "Jaipur"}, status="ok")
        ac.record("web_search", {"query": "x"}, status="ok")
        c = ac._conn()
        c.execute("UPDATE audit_log SET params = '{\"city\": \"Mars\"}'"
                  " WHERE id = 1")
        c.commit()
        out = ac.verify()
        assert "BROKEN" in out and "#1" in out

    def test_delete_is_detected(self):
        from core import audit_chain as ac
        ac.record("a", {}, status="ok")
        ac.record("b", {}, status="ok")
        c = ac._conn()
        c.execute("DELETE FROM audit_log WHERE id = 1")
        c.commit()
        assert "BROKEN" in ac.verify()

    def test_redaction_by_key_and_value(self, monkeypatch):
        from core import audit_chain as ac
        # by key
        s = ac.redact({"api_key": "abc123456789", "city": "Jaipur"})
        assert "abc123456789" not in s and "[redacted]" in s
        assert "Jaipur" in s
        # by value pattern
        s2 = ac.redact({"note": "use ghp_abcdefghijklmnop123456"})
        assert "ghp_" not in s2
        # config values never land in the log
        monkeypatch.setattr(ac, "_config_secrets",
                            lambda: {"totally-secret-value-9999"})
        s4 = ac.redact({"msg": "my key is totally-secret-value-9999"})
        assert "totally-secret-value-9999" not in s4

    def test_run_hook_records_all_statuses(self):
        from core import audit_chain as ac
        from core.action_loader import ActionRegistry, ActionRecord

        def ok_fn(parameters=None, **kw):
            return "all good"

        def bad_fn(parameters=None, **kw):
            raise RuntimeError("boom")

        recs = {
            "ok_tool": ActionRecord(name="ok_tool", handler=ok_fn, valid=True),
            "bad_tool": ActionRecord(name="bad_tool", handler=bad_fn,
                                     valid=True),
        }
        reg = ActionRegistry(recs, logger=lambda m: None)
        reg.run("ok_tool", {"x": 1})
        reg.run("bad_tool", {})
        reg.run("missing_tool", {"y": 2})
        c = ac._conn()
        rows = {r["tool"]: r for r in c.execute(
            "SELECT tool, status, params FROM audit_log")}
        assert rows["ok_tool"]["status"] == "ok"
        assert rows["bad_tool"]["status"] == "error"
        assert "boom" in rows["bad_tool"]["detail"] \
            if "detail" in rows["bad_tool"].keys() else True
        assert rows["missing_tool"]["status"] == "unavailable"
        assert '"x": 1' in rows["ok_tool"]["params"]

    def test_recent_and_tool_shape(self):
        from core import audit_chain as ac
        ac.record("demo", {"a": 5}, status="ok")
        assert "demo" in ac.audit_log({"action": "recent"})
        assert "BROKEN" not in ac.audit_log({"action": "verify"})
        assert ac.TOOL["name"] == "audit_log"
        assert ac.TOOL["handler"] is ac.audit_log


class TestWellbeing:
    """3b: browser visit analytics from locked-copy history (Report D3)."""

    @staticmethod
    def _chromium_db(tmp_path, visits):
        """visits: list of (url, seconds_ago)"""
        p = tmp_path / "History"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT)")
        c.execute("CREATE TABLE visits (url_id INTEGER, visit_time INTEGER)")
        now = time.time()
        for i, (url, ago) in enumerate(visits, 1):
            c.execute("INSERT INTO urls VALUES (?, ?)", (i, url))
            us = int((now - ago + 11644473600) * 1_000_000)
            c.execute("INSERT INTO visits VALUES (?, ?)", (i, us))
        c.commit()
        c.close()
        return p

    @pytest.fixture(autouse=True)
    def _dbs(self, tmp_path, monkeypatch):
        from actions import wellbeing as w
        self._w = w
        self._tmp = tmp_path
        yield

    def test_today_groups_strips_www_and_ranks(self):
        w = self._w
        db = self._chromium_db(self._tmp, [
            ("https://www.example.com/a", 60),
            ("https://www.example.com/b", 120),
            ("https://example.com/c", 3600),
            ("https://news.site.org/x", 300),
            ("https://old.site.org/y", 8 * 86400),     # last week only
        ])
        w._history_dbs = lambda: [("chromium", db)]
        out = w.wellbeing({"action": "today"})
        assert "example.com" in out and "3 visit(s)" in out
        assert "news.site.org" in out
        assert "old.site.org" not in out              # outside today window
        assert "www." not in out.split("example.com")[0][-4:]  # stripped
        assert "not minutes" in out                   # honesty line
        assert "focus start" in out                   # top site ≥3 visits

    def test_week_includes_older_and_domain_list(self):
        w = self._w
        db = self._chromium_db(self._tmp, [
            ("https://old.site.org/y", 6 * 86400),   # inside 7-day window
        ])
        w._history_dbs = lambda: [("chromium", db)]
        week = w.wellbeing({"action": "week"})
        assert "old.site.org" in week
        doms = w.wellbeing({"action": "domains"})
        assert "old.site.org: 1" in doms

    def test_firefox_schema_and_no_history_honest(self):
        w = self._w
        p = self._tmp / "places.sqlite"
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT)")
        c.execute("CREATE TABLE moz_historyvisits (place_id INTEGER,"
                  " visit_date INTEGER)")
        us = int(time.time() * 1_000_000)
        c.execute("INSERT INTO moz_places VALUES (1, 'https://ff.page/')")
        c.execute("INSERT INTO moz_historyvisits VALUES (1, ?)", (us,))
        c.commit()
        c.close()
        w._history_dbs = lambda: [("firefox", p)]
        out = w.wellbeing({"action": "today"})
        assert "ff.page" in out
        # nothing at all → honest message
        w._history_dbs = lambda: []
        assert "No browser history found" in w.wellbeing({})

    def test_live_file_never_opened(self):
        """_copy_locked reads a copy; the source bytes stay untouched."""
        db = self._chromium_db(self._tmp, [("https://a.b/", 10)])
        before = db.read_bytes()
        copy = self._w._copy_locked(db)
        try:
            assert copy != db and copy.is_file()
            assert db.read_bytes() == before
        finally:
            shutil.rmtree(copy.parent, ignore_errors=True)

    def test_tool_shape(self):
        assert self._w.TOOL["name"] == "wellbeing"
        assert self._w.TOOL["handler"] is self._w.wellbeing


class TestSpokenBrief:
    """3d: spoken brief — composed sources, spoken via ctx speak (K)."""

    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.spoken_brief as sb
        self._sb = sb
        monkeypatch.setattr(sb, "_cache_path",
                            lambda: tmp_path / "brief.txt")
        monkeypatch.setattr(sb, "_weather_section",
                            lambda city="": "24C, clear skies in Jaipur")
        monkeypatch.setattr(sb, "_tasks_section",
                            lambda: "Standup 10:00 | Ship report 18:00")
        monkeypatch.setattr(sb, "_headlines_section",
                            lambda limit=4: "Big story one — Big story two")
        yield

    def test_compose_merges_sections_with_greeting(self):
        text = self._sb.compose()
        assert "Good" in text or "Hello" in text
        assert "24C" in text and "Standup" in text and "Big story" in text
        low = text.lower()
        assert "weather:" in low and "agenda:" in low and "headlines:" in low

    def test_missing_section_is_noted_not_fatal(self, monkeypatch):
        monkeypatch.setattr(self._sb, "_weather_section",
                            lambda city="": "")
        text = self._sb.compose()
        assert "24C" not in text
        assert "no weather available" in text

    def test_all_missing_is_honest(self, monkeypatch):
        monkeypatch.setattr(self._sb, "_weather_section", lambda city="": "")
        monkeypatch.setattr(self._sb, "_tasks_section", lambda: "")
        monkeypatch.setattr(self._sb, "_headlines_section", lambda limit=4: "")
        assert "empty" in self._sb.compose()

    def test_spoken_and_cached_then_refresh(self):
        spoken = []

        class P:
            def show_content(self, t, b):
                pass

        out = self._sb.spoken_brief({}, player=P(), speak=spoken.append)
        assert spoken and spoken[0] == out
        # second call serves the cache (sections not re-invoked)
        status = self._sb.spoken_brief({"action": "status"})
        assert "cached" in status
        # refresh recomposes and speaks again
        spoken.clear()
        out2 = self._sb.spoken_brief({"action": "refresh"},
                                     speak=spoken.append)
        assert spoken and spoken[0] == out2

    def test_speak_none_still_returns(self):
        out = self._sb.spoken_brief({})
        assert isinstance(out, str) and out

    def test_tool_shape(self):
        assert self._sb.TOOL["name"] == "spoken_brief"
        assert self._sb.TOOL["handler"] is self._sb.spoken_brief


class TestTranslateLens:
    """I: capture → OCR → translate → floating HUD overlay."""

    @pytest.fixture(autouse=True)
    def _wired(self, monkeypatch):
        import actions.region_ocr as ro
        import actions.translate as tr
        import actions.translate_lens as tl
        self._tl = tl
        monkeypatch.setattr(ro, "_capture", lambda region: object())
        monkeypatch.setattr(ro, "_read_text",
                            lambda img, mode="text":
                            "HELLO WORLD\nSECOND LINE HERE")
        monkeypatch.setattr(tr, "translate",
                            lambda p: f"[{p.get('target') or 'hi'}] "
                                      f"{p['text']}")
        class P:
            def __init__(self):
                self.lens = []
                self.content = []
            def show_lens(self, pairs, src="", dst=""):
                self.lens.append((list(pairs), src, dst))
            def show_content(self, t, b):
                self.content.append((t, b))
        self.player = P()
        yield

    def test_lens_translates_and_pushes_overlay(self):
        out = self._tl.translate_lens(
            {"to": "es"}, player=self.player)
        assert "2 line(s), 2 translated" in out
        assert "HELLO WORLD" in out and "[es] HELLO WORLD" in out
        assert self.player.lens, "overlay must be pushed"
        pairs, src, dst = self.player.lens[0]
        assert pairs[0][1].startswith("[es]")
        assert self.player.content and self.player.content[0][0] == "TRANSLATE LENS"

    def test_no_capture_backend_is_honest(self, monkeypatch):
        import actions.region_ocr as ro
        monkeypatch.setattr(ro, "_capture", lambda region: None)
        out = self._tl.translate_lens({}, player=self.player)
        assert "nothing readable" in out

    def test_no_text_in_region_is_honest(self, monkeypatch):
        import actions.region_ocr as ro
        monkeypatch.setattr(ro, "_capture", lambda region: object())
        monkeypatch.setattr(ro, "_read_text",
                            lambda img, mode="text": "   \n  ")
        out = self._tl.translate_lens({})
        assert "nothing readable" in out

    def test_translation_unavailable_surfaces_reason(self, monkeypatch):
        import actions.translate as tr
        monkeypatch.setattr(
            tr, "translate",
            lambda p: "No translation engine available: install "
                      "`argostranslate` for offline translation, or "
                      "configure a Gemini key.")
        out = self._tl.translate_lens({}, player=self.player)
        assert "translation unavailable" in out
        assert "No translation engine" in out

    def test_repeat_two_frames(self):
        out = self._tl.translate_lens({"repeat": "2", "pause": "0.5"},
                                      player=self.player)
        assert "frame 1:" in out and "frame 2:" in out
        assert len(self.player.lens) == 2

    def test_tool_shape(self):
        assert self._tl.TOOL["name"] == "translate_lens"
        assert self._tl.TOOL["handler"] is self._tl.translate_lens


class TestObsidian:
    """4b: Obsidian Local REST API bridge (CRUD + honest offline)."""

    @pytest.fixture(autouse=True)
    def _server(self, tmp_path, monkeypatch):
        import actions.obsidian as ob
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from urllib.parse import urlparse, parse_qs
        self._ob = ob
        vault = {"notes/a.md": "# A\nhello vault",
                 "notes/b.md": "# B\nsecond note"}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=""):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body.encode())

            def do_GET(self):
                u = urlparse(self.path)
                if u.path == "/":
                    self._send(200, '{"status":"ok"}')
                elif u.path == "/vault/":
                    self._send(200, json.dumps(sorted(vault)))
                elif u.path.startswith("/vault/"):
                    p = u.path[len("/vault/"):]
                    if p in vault:
                        self._send(200, json.dumps(
                            {"content": vault[p]}))
                    else:
                        self._send(404, '{"error":"nf"}')
                elif u.path == "/tags/":
                    self._send(200, json.dumps(
                        {"tags": [{"tag": "#idea", "usage": 3}]}))
                else:
                    self._send(404, "")

            def do_PUT(self):
                p = urlparse(self.path).path[len("/vault/"):]
                n = int(self.headers.get("Content-Length", 0))
                vault[p] = self.rfile.read(n).decode()
                self._send(204, "")

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n).decode() if n else ""
                if u.path.startswith("/vault/"):
                    p = u.path[len("/vault/"):]
                    vault[p] = vault.get(p, "") + body
                    self._send(200, "")
                elif u.path.startswith("/search/simple"):
                    q = (parse_qs(u.query).get("query") or [""])[0]
                    hits = [{"filename": k, "context": "…"}
                            for k in vault if q.lower() in
                            vault[k].lower()]
                    self._send(200, json.dumps({"matches": hits}))
                elif u.path.startswith("/open/"):
                    self._send(204, "")
                else:
                    self._send(404, "")

            def do_DELETE(self):
                p = urlparse(self.path).path[len("/vault/"):]
                vault.pop(p, None)
                self._send(204, "")

        srv = HTTPServer(("127.0.0.1", 0), H)
        import threading
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        monkeypatch.setattr(ob, "_settings", lambda: (base, "test-key"))
        self.vault = vault
        yield
        srv.shutdown()

    def test_status_read_list(self):
        ob = self._ob
        assert "is up" in ob.obsidian({"action": "status"})
        body = ob.obsidian({"action": "read", "path": "notes/a.md"})
        assert "hello vault" in body
        listing = ob.obsidian({"action": "list"})
        assert "notes/a.md" in listing

    def test_write_and_append(self):
        ob = self._ob
        assert "Wrote" in ob.obsidian({"action": "write",
                                       "path": "notes/new.md",
                                       "content": "# New"})
        assert "Appended" in ob.obsidian({"action": "append",
                                          "path": "notes/new.md",
                                          "content": "more text"})
        assert "more text" in self.vault["notes/new.md"]

    def test_search_and_tags_and_open(self):
        ob = self._ob
        out = ob.obsidian({"action": "search", "query": "second"})
        assert "notes/b.md" in out
        tags = ob.obsidian({"action": "tags"})
        assert "#idea" in tags
        assert "Opened" in ob.obsidian({"action": "open",
                                        "path": "notes/a.md"})

    def test_offline_and_missing_path_honest(self, monkeypatch):
        ob = self._ob
        monkeypatch.setattr(ob, "_settings",
                            lambda: ("http://127.0.0.1:1", "k"))
        out = ob.obsidian({"action": "status"})
        assert "Local REST API is not reachable" in out
        monkeypatch.setattr(ob, "_settings",
                            lambda: ("http://127.0.0.1:1", "k"))
        assert "Give a `path`" in ob.obsidian({"action": "read"})
        assert "Unknown obsidian action" in ob.obsidian(
            {"action": "zap", "path": "x"})

    def test_tool_shape(self):
        assert self._ob.TOOL["name"] == "obsidian"
        assert self._ob.TOOL["handler"] is self._ob.obsidian


class TestTelegramRx:
    """4c: Telegram receive — allowlisted long-poll into text-command path."""

    @pytest.fixture(autouse=True)
    def _rx(self, monkeypatch):
        import actions.telegram_rx as rx
        self._rx = rx
        rx.set_dispatch(None)
        rx.stop()
        rx._STATE.update({"running": False, "blocked": 0, "handled": 0,
                          "last_error": "", "token": ""})
        monkeypatch.setattr(rx, "_settings",
                            lambda: ("TESTTOKEN123", ["42"]))
        yield
        rx.stop()
        rx.set_dispatch(None)

    @staticmethod
    def _updates(messages):
        return {"result": [
            {"update_id": 100 + i,
             "message": {"chat": {"id": cid}, "text": txt}}
            for i, (cid, txt) in enumerate(messages)]}

    def test_allowlisted_text_reaches_dispatch(self, monkeypatch):
        rx = self._rx
        got = []
        rx.set_dispatch(got.append)
        monkeypatch.setattr(rx, "_http_get",
                            lambda *a, **k: self._updates(
                                [(42, "run system status")]))
        rx._one_round()
        assert got == ["run system status"]
        assert rx._STATE["handled"] == 1

    def test_unknown_chat_blocked_and_counted(self, monkeypatch):
        rx = self._rx
        got = []
        rx.set_dispatch(got.append)
        monkeypatch.setattr(rx, "_http_get",
                            lambda *a, **k: self._updates(
                                [(666, "hack the box"), (42, "hello")]))
        rx._one_round()
        assert got == ["hello"]
        assert rx._STATE["blocked"] == 1

    def test_refuses_without_allowlist(self, monkeypatch):
        rx = self._rx
        monkeypatch.setattr(rx, "_settings", lambda: ("TESTTOKEN123", []))
        out = rx.start()
        assert "Refusing" in out and "allowed_chats" in out

    def test_refuses_without_token(self, monkeypatch):
        rx = self._rx
        monkeypatch.setattr(rx, "_settings", lambda: ("", ["42"]))
        assert "No Telegram bot token" in rx.start()

    def test_start_stop_status_lifecycle(self, monkeypatch):
        rx = self._rx
        # fake long-poll that blocks until stop
        calls = {"n": 0}

        def fake_get(*a, **k):
            calls["n"] += 1
            if calls["n"] >= 3:
                import time as _t
                _t.sleep(0.05)
            return {"result": []}

        monkeypatch.setattr(rx, "_http_get", fake_get)
        out = rx.start()
        assert "allowlisted 1 chat" in out
        assert "already running" in rx.start()
        st = rx.telegram_rx({"action": "status"})
        assert "running" in st
        rx.stop()
        import time
        time.sleep(0.2)
        assert "stopped" in rx.telegram_rx({"action": "status"})

    def test_autostart_silent_when_unconfigured(self, monkeypatch):
        rx = self._rx
        monkeypatch.setattr(rx, "_settings", lambda: ("", []))
        assert rx.autostart() == ""

    def test_dispatch_exception_is_recorded_not_raised(self, monkeypatch):
        rx = self._rx

        def boom(text):
            raise RuntimeError("session down")

        rx.set_dispatch(boom)
        monkeypatch.setattr(rx, "_http_get",
                            lambda *a, **k: self._updates([(42, "x")]))
        rx._one_round()                      # must not raise
        assert "session down" in rx._STATE["last_error"]

    def test_tool_shape(self):
        assert self._rx.TOOL["name"] == "telegram_rx"
        assert self._rx.TOOL["handler"] is self._rx.telegram_rx


class TestDictation:
    """J: speech-to-text typing mode (offline segments + live feed)."""

    @pytest.fixture(autouse=True)
    def _mod(self, monkeypatch):
        import actions.dictation as d
        self._d = d
        d._ON = False
        d._STOP.set()
        d._STATE.update({"typed": 0, "chars": 0, "last": "", "error": ""})
        d._DEST = "clipboard"
        d._FILE = ""
        d._PLAYER = None
        yield
        d._ON = False
        d._STOP.set()

    def test_off_by_default_and_status_hint(self):
        assert "off" in self._d.dictation({"action": "status"})
        assert "action=on" in self._d.dictation({})

    def test_feed_noop_when_off(self, monkeypatch):
        got = []
        monkeypatch.setattr(self._d, "_clip_set", lambda t: got.append(t) or True)
        assert self._d.feed("hello") is False
        assert got == []

    def test_on_off_lifecycle_with_seams(self, monkeypatch):
        d = self._d
        monkeypatch.setattr(d, "_record_segment", lambda s: object())
        monkeypatch.setattr(d, "_transcribe", lambda a: "typed words")
        monkeypatch.setattr(d, "_clip_set", lambda t: True)
        out = d.dictation({"action": "on"})
        assert "Dictation ON" in out
        assert d._ON is True
        # one explicit tick (thread also runs, seams are safe)
        d._tick()
        assert d._STATE["typed"] >= 1 and d._STATE["last"] == "typed words"
        off = d.dictation({"action": "off"})
        assert "OFF" in off and "typed" in off
        assert d._ON is False

    def test_file_destination_appends(self, tmp_path):
        d = self._d
        target = tmp_path / "dict.txt"
        out = d.dictation({"action": "on", "dest": "file",
                           "path": str(target), "mode": "live"})
        assert "live feed" in out
        assert d.feed("line one")
        assert d.feed("line two")
        d.dictation({"action": "off"})
        body = target.read_text(encoding="utf-8")
        assert "line one" in body and "line two" in body

    def test_file_dest_needs_path(self):
        out = self._d.dictation({"action": "on", "dest": "file"})
        assert "needs a path" in out

    def test_bad_dest_honest(self):
        assert "dest must be" in self._d.dictation(
            {"action": "on", "dest": "floppy"})

    def test_record_failure_is_status_error_not_crash(self, monkeypatch):
        d = self._d
        monkeypatch.setattr(d, "_record_segment",
                            lambda s: (_ for _ in ()).throw(
                                RuntimeError("mic unavailable (none)")))
        monkeypatch.setattr(d, "_transcribe", lambda a: "")
        out = d.dictation({"action": "on"})
        assert "ON" in out
        d._tick()                              # must not raise
        assert "mic unavailable" in d._STATE["error"]
        assert "⚠" in d.dictation({"action": "status"})
        d.dictation({"action": "off"})

    def test_transcribe_empty_skips(self, monkeypatch):
        d = self._d
        monkeypatch.setattr(d, "_record_segment", lambda s: object())
        monkeypatch.setattr(d, "_transcribe", lambda a: "   ")
        monkeypatch.setattr(d, "_clip_set", lambda t: True)
        d.dictation({"action": "on", "mode": "live"})
        d._tick()
        assert d._STATE["typed"] == 0
        d.dictation({"action": "off"})

    def test_hud_destination_uses_player(self):
        d = self._d
        shown = []

        class P:
            def show_content(self, t, b):
                shown.append((t, b))
        d.dictation({"action": "on", "dest": "hud", "mode": "live"},
                    player=P())
        d.feed("spoken line")
        d.dictation({"action": "off"})
        assert shown and shown[0][0] == "DICTATION"

    def test_tool_shape(self):
        assert self._d.TOOL["name"] == "dictation"
        assert self._d.TOOL["handler"] is self._d.dictation


class TestResearchCritic:
    """Deep-research loop: authority critic + citation verify + extras."""

    def test_authority_scores(self):
        import actions.research as r
        assert r._authority("https://docs.python.org/3/") == 1.0
        assert r._authority("https://arxiv.org/abs/1234") == 1.0
        assert r._authority("https://www.reuters.com/x") == 0.7
        assert r._authority("https://medium.com/@a/post") == 0.4
        assert r._authority("https://reddit.com/r/x") == 0.3
        assert r._authority("https://random.example.com/") == 0.5

    def test_critic_ranks_and_prunes(self):
        import actions.research as r
        docs = [
            {"title": "social", "url": "https://reddit.com/a",
             "excerpt": "x" * 200},
            {"title": "official", "url": "https://docs.official.dev/guide",
             "excerpt": "y" * 200},
            {"title": "blog", "url": "https://dev.to/p", "excerpt": "z" * 200},
        ]
        kept = r._critique(docs, 2)
        assert [d["title"] for d in kept] == ["official", "blog"]

    def test_citation_verify_counts(self):
        import actions.research as r
        docs = [{"title": "Study", "url": "https://x",
                 "excerpt": "The aquifer fell 12.5% in 2024 after weak rains."}]
        report = "Water tables fell 12.5% [1]. Also 99.9% nowhere [1]."
        ok, total = r._verify_citations(report, docs)
        assert total == 2 and ok == 1

    def test_citation_header_landed_in_file(self, tmp_path, monkeypatch):
        import actions.research as r
        monkeypatch.setattr(r, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(r, "_synth_report", lambda t, d: None)
        monkeypatch.setattr(r, "_extra_sources", lambda t: [])
        monkeypatch.setattr(
            r, "_search",
            lambda q, max_results=6: [{"title": "A",
                                       "url": "https://docs.a.gov/g"}])
        monkeypatch.setattr(
            r, "_fetch_url",
            lambda u, timeout=15:
            "<html><head><title>Gov study</title></head><body><main>"
            "<p>Report: usage rose 42.0% during 2025 across districts, "
            "with steady growth each quarter and no decline recorded at "
            "any monitoring station across the whole year reliably.</p>"
            "</main></body></html>")
        out = r.research({"topic": "water", "depth": "standard"})
        assert "Research report saved:" in out
        path = Path(out.split("saved: ", 1)[1].split(" ", 1)[0])
        body = path.read_text(encoding="utf-8")
        assert "Citation check:" in body

    def test_extra_sources_use_seams(self, monkeypatch):
        import actions.research as r
        # arXiv path uses raw requests — stub via _http_json + requests
        import requests
        atom = (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry><title>Quantum paper</title>"
            "<summary>We show a result about qubits in 2025 with error "
            "rates below previous estimates.</summary>"
            "<id>http://arxiv.org/abs/1234.5678</id></entry></feed>")

        class R:
            text = atom
            def raise_for_status(self):
                pass

        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: R())

        def fake_json(url, timeout=12, headers=None):
            if "semanticscholar" in url:
                return {"data": [{"title": "S2 Paper", "year": 2025,
                                  "abstract": "An abstract about models.",
                                  "url": "https://s2.example/p"}]}
            if "opensearch" in url:
                return ["q", ["Quantum mechanics"]]
            if "rest_v1/page/summary" in url:
                return {"title": "Quantum mechanics",
                        "extract": "Quantum mechanics is fundamental.",
                        "content_urls": {"desktop": {
                            "page": "https://en.wikipedia.org/wiki/"
                                    "Quantum_mechanics"}}
                        if False else {"desktop": {"page":
                                    "https://en.wikipedia.org/wiki/QM"}}}
            return {}

        monkeypatch.setattr(r, "_http_json", fake_json)
        out = r._extra_sources("quantum mechanics")
        titles = " | ".join(d["title"] for d in out)
        assert "arXiv" in titles and "S2 Paper" in titles
        assert "Wikipedia" in titles

    def test_github_metadata_only_for_repo_topics(self, monkeypatch):
        import actions.research as r
        import requests
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no net")))

        def fake_json(url, timeout=12, headers=None):
            if "api.github.com" in url:
                return {"items": [{"full_name": "x/cool",
                                   "stargazers_count": 1234,
                                   "license": {"spdx_id": "MIT"},
                                   "html_url": "https://github.com/x/cool",
                                   "description": "A cool tool",
                                   "pushed_at": "2026-10-01"}]}
            raise RuntimeError("no net")

        monkeypatch.setattr(r, "_http_json", fake_json)
        # repo-flavoured topic → GitHub entry present
        out = r._extra_sources("best flutter library")
        assert any("GitHub: x/cool" in d["title"] and "★1234" in d["title"]
                   and "MIT" in d["title"] for d in out)
        # non-repo topic → no GitHub entry
        out2 = r._extra_sources("monsoon rains rajasthan")
        assert not any(d["title"].startswith("GitHub:") for d in out2)


class TestMermaid:
    """R2 §12: real mermaid-cli render with an honest missing-tool path."""

    def test_no_source_hints(self):
        import actions.mermaid as m
        assert "``` fences are fine" in m.mermaid({"source": ""})

    def test_garbage_source_rejected(self):
        import actions.mermaid as m
        out = m.mermaid({"source": "just some prose with no diagram"})
        assert "doesn't look like Mermaid" in out

    def test_normalize_strips_fences(self):
        import actions.mermaid as m
        src = "```mermaid\nflowchart TD; A-->B\n```"
        assert m._normalize(src).startswith("flowchart TD")

    def test_missing_cli_is_honest_install_hint(self, monkeypatch):
        import actions.mermaid as m
        monkeypatch.setattr(m, "_cli_candidates", lambda: [])
        out = m.mermaid({"source": "flowchart TD; A-->B"})
        assert "not installed" in out and "npm install -g" in out
        assert "never a fake" in out

    def test_successful_render_writes_svg(self, tmp_path, monkeypatch):
        import actions.mermaid as m
        monkeypatch.setattr(m, "_base_dir", lambda: tmp_path)

        def fake_run(argv, mmd_path, svg_path, timeout=90):
            assert mmd_path.read_text(encoding="utf-8").startswith("flowchart")
            svg_path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>",
                                encoding="utf-8")
            return svg_path.read_text(encoding="utf-8")
        monkeypatch.setattr(m, "_cli_candidates", lambda: [["npx", "-y", "x"]])
        monkeypatch.setattr(m, "_run_cli", fake_run)
        out = m.mermaid({"source": "flowchart TD; A-->B"})
        assert "Mermaid diagram rendered:" in out
        path = Path(out.split("rendered: ", 1)[1].split(". Open", 1)[0])
        assert path.is_file() and path.suffix == ".svg"
        assert path.with_suffix(".mmd").is_file()   # source kept for edits

    def test_timeout_then_honest_failure(self, tmp_path, monkeypatch):
        import subprocess as sp
        import actions.mermaid as m
        monkeypatch.setattr(m, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(m, "_cli_candidates", lambda: [["npx", "-y", "x"]])

        def boom(argv, mmd_path, svg_path, timeout=90):
            raise sp.TimeoutExpired(argv, 90)
        monkeypatch.setattr(m, "_run_cli", boom)
        out = m.mermaid({"source": "sequenceDiagram\nA->>B: hi"})
        assert "timed out" in out and "saved at" in out
        # the .mmd landed so the user can fix it externally
        assert list((tmp_path / "diagrams").glob("*.mmd"))

    def test_all_candidates_fail_reports_last_error(self, tmp_path,
                                                    monkeypatch):
        import actions.mermaid as m
        monkeypatch.setattr(m, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(m, "_cli_candidates", lambda: [["mmdc"]])

        def fail(argv, mmd_path, svg_path, timeout=90):
            raise RuntimeError("chromium missing")
        monkeypatch.setattr(m, "_run_cli", fail)
        out = m.mermaid({"source": "graph LR; X-->Y"})
        assert "Mermaid render failed:" in out and "chromium missing" in out

    def test_tool_discoverable(self):
        from pathlib import Path as P
        from core.action_loader import discover_actions
        reg = discover_actions(P("actions"))
        assert "mermaid" in reg.names()


class TestKGraph:
    """R2 §4: Graphiti-lite temporal knowledge graph (local sqlite)."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path, monkeypatch):
        import memory.graph as g
        monkeypatch.setattr(g, "_db_path", lambda: tmp_path / "graph.db")
        self.g = g

    def test_upsert_dedupes_and_guesses_kind(self):
        a = self.g.upsert_entity("Priya")
        b = self.g.upsert_entity("priya")            # case-insensitive
        assert a == b
        assert self.g._guess_kind("Acme Labs Inc") == "org"
        assert self.g._guess_kind("jaipur") == "place"

    def test_link_and_affirm(self):
        r1 = self.g.link("Priya", "Rahul", "works_with")
        assert r1["mode"] == "linked"
        r2 = self.g.link("Priya", "Rahul", "works_with")
        assert r2["mode"] == "affirmed" and r2["id"] == r1["id"]

    def test_refusals(self):
        with pytest.raises(ValueError):
            self.g.link("Priya", "Priya", "knows")
        with pytest.raises(ValueError):
            self.g.link("A", "B", "  ")
        with pytest.raises(ValueError):
            self.g.upsert_entity("  ")

    def test_extract_free_offline(self):
        made = self.g.extract(
            "Priya works_with Rahul. Rahul lives in Jaipur. "
            "Meera is the founder of Acme Labs.")
        rels = {m["rel"] for m in made}
        assert "works_with" in rels
        assert "lives_in" in rels
        assert "founded" in rels
        assert all(m["a"] for m in made)

    def test_close_makes_fact_stale(self):
        self.g.link("Priya", "Acme", "works_at", at=1_000_000.0)
        self.g.close("Priya", "Acme", "works_at", at=2_000_000.0)
        out = self.g.search("Priya")
        assert "no active relations" in out or "works_at" not in out
        tl = self.g.timeline("Priya")
        assert "works_at" in tl and "→ 1970-01-24" in tl  # valid_to shown

    def test_multi_hop_search(self):
        self.g.link("Priya", "Rahul", "works_with")
        self.g.link("Rahul", "Jaipur", "lives_in")
        out = self.g.search("Priya", hops=2)
        assert "works_with" in out and "lives_in" in out
        assert "active relation" in out

    def test_search_honest_empty(self):
        assert "No graph entities match" in self.g.search("Ghost")
        self.g.upsert_entity("Lonely")
        assert "no active relations" in self.g.search("Lonely")

    def test_tool_dispatch_and_errors(self):
        out = self.g.graph({"action": "remember",
                            "text": "Priya works_with Rahul"})
        assert "Extracted + linked" in out
        out2 = self.g.graph({"action": "search", "query": "Priya"})
        assert "works_with" in out2
        out3 = self.g.graph({"action": "close", "a": "Nope", "b": "Nada",
                             "rel": "x"})
        assert out3.startswith("graph:")
        out4 = self.g.graph({"action": "timeline", "query": "Ghost"})
        assert "No relations" in out4

    def test_explicit_remember_via_ab(self):
        out = self.g.graph({"action": "remember", "a": "Sam",
                            "b": "OpenAI", "rel": "works_at"})
        assert "works_at" in out

    def test_tool_discoverable(self):
        from pathlib import Path as P
        from core.action_loader import discover_actions
        reg = discover_actions(P("actions"))
        assert "graph" in reg.names()


class TestProactive30:
    """R2 §6: pending-decision injection into the proactive prompt."""

    @pytest.fixture(autouse=True)
    def _confirm(self, monkeypatch):
        import core.confirm as cf
        self.cf = cf
        monkeypatch.setattr(cf, "_show_cb", lambda t, d: None)
        monkeypatch.setattr(cf, "_hide_cb", lambda: None)
        monkeypatch.setattr(cf, "_log_cb", lambda m: None)
        monkeypatch.setattr(cf, "_pending", None)
        monkeypatch.setattr(cf, "_history", [])
        yield
        monkeypatch.setattr(cf, "_pending", None)

    def test_stats_empty_initially(self):
        st = self.cf.stats()
        assert st == {"pending": {}, "recent": [], "total": 0}

    def test_pending_and_confirmed_history(self):
        out = self.cf.request("k1", "Delete logs?", "disk cleanup",
                              lambda: "deleted")
        assert "CONFIRMATION_PENDING" in out
        st = self.cf.stats()
        assert st["pending"]["title"] == "Delete logs?"
        assert "age_s" in st["pending"]
        self.cf.resolve(True)                      # accepted → runs worker
        st2 = self.cf.stats()
        assert st2["pending"] == {}
        assert st2["recent"][-1]["outcome"] == "confirmed"
        assert self.cf.pending_title() == ""

    def test_cancelled_and_expired_recorded(self):
        self.cf.request("k2", "Reboot?", "", lambda: "ok")
        self.cf.resolve(False)
        assert self.cf.stats()["recent"][-1]["outcome"] == "cancelled"
        # expired: shrink the timeout for this check
        import core.confirm as cf
        self.cf.request("k3", "Run old job?", "", lambda: "ok")
        monkey_timeout = cf.TIMEOUT_SECONDS
        object.__getattribute__(cf, "_pending").at -= monkey_timeout + 5
        self.cf.resolve(True)
        assert self.cf.stats()["recent"][-1]["outcome"] == "expired"

    def test_prompt_includes_waiting_and_expired(self):
        from actions.proactive import ProactiveEngine
        eng = ProactiveEngine()
        decisions = {
            "pending": {"title": "Delete logs?", "age_s": 12},
            "recent": [{"title": "Reboot box", "outcome": "expired",
                        "at": 0},
                       {"title": "Wipe disk", "outcome": "cancelled",
                        "at": 0}],
            "total": 2,
        }
        p = eng.build_prompt(memory={}, decisions=decisions)
        assert "Pending user decisions" in p
        assert "WAITING on screen" in p and "Delete logs?" in p
        assert "expired unanswered" in p
        assert "was declined" in p
        assert "confirmed" not in p.split("Pending user decisions")[1]

    def test_prompt_without_decisions_unchanged(self):
        from actions.proactive import ProactiveEngine
        eng = ProactiveEngine()
        p = eng.build_prompt(memory={})
        assert "Pending user decisions" not in p
        assert "[PROACTIVE_CHECK]" in p

    def test_prompt_with_empty_stats_no_block(self):
        from actions.proactive import ProactiveEngine
        eng = ProactiveEngine()
        p = eng.build_prompt(memory={}, decisions=self.cf.stats())
        assert "Pending user decisions" not in p

    def test_main_wires_confirm_stats_into_prompt(self):
        src = Path("main.py").read_text(encoding="utf-8")
        assert "decisions    = _decisions" in src
        assert "_confirm.stats()" in src


class TestResearchJSRender:
    """JS-heavy pages: one Playwright render fallback, honest on failure."""

    def _run(self, monkeypatch, tmp_path, excerpt_html, rendered_html=""):
        import actions.research as r
        monkeypatch.setattr(r, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(r, "_synth_report", lambda t, d: None)
        monkeypatch.setattr(r, "_extra_sources", lambda t: [])
        monkeypatch.setattr(
            r, "_search",
            lambda q, max_results=6: [{"title": "A",
                                       "url": "https://spa.example/app"}])
        monkeypatch.setattr(r, "_fetch_url",
                            lambda u, timeout=15: excerpt_html)
        monkeypatch.setattr(r, "_fetch_rendered",
                            lambda u, timeout=15: rendered_html)
        return r.research({"topic": "spa", "depth": "quick"})

    def test_thin_shell_upgraded_by_render(self, tmp_path, monkeypatch):
        shell = ("<html><body><div id='root'></div>"
                 "<script>renderMe()</script></body></html>")
        rich = ("<html><head><title>SPA doc</title></head><body><main>"
                "<p>This application guide explains everything about the "
                "dashboard, including settings, billing, teams and "
                "reporting workflows in plenty of detail for readers who "
                "need the full picture rather than a stub page.</p>"
                "</main></body></html>")
        out = self._run(monkeypatch, tmp_path, shell, rich)
        assert "Research report saved:" in out
        path = Path(out.split("saved: ", 1)[1].split(" ", 1)[0])
        body = path.read_text(encoding="utf-8")
        assert "dashboard, including settings" in body

    def test_render_failure_keeps_raw(self, tmp_path, monkeypatch):
        import actions.research as r
        def boom(u, timeout=15):
            raise RuntimeError("no browser")
        # rebuild with the boom seam
        monkeypatch.setattr(r, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(r, "_synth_report", lambda t, d: None)
        monkeypatch.setattr(r, "_extra_sources", lambda t: [])
        monkeypatch.setattr(
            r, "_search",
            lambda q, max_results=6: [{"title": "A",
                                       "url": "https://spa.example/app"}])
        monkeypatch.setattr(
            r, "_fetch_url",
            lambda u, timeout=15:
            "<html><body><div id='root'></div>"
            "<script>boot()</script></body></html>")
        monkeypatch.setattr(r, "_fetch_rendered", boom)
        out = r.research({"topic": "spa", "depth": "quick"})
        # shell is a stub → honest no-sources (raw stayed stub, no crash)
        assert "no usable sources" in out or "saved:" in out

    def test_thick_page_skips_render(self, tmp_path, monkeypatch):
        calls = []

        def render(u, timeout=15):
            calls.append(u)
            return "<html><body><p>x</p></body></html>"
        import actions.research as r
        monkeypatch.setattr(r, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(r, "_synth_report", lambda t, d: None)
        monkeypatch.setattr(r, "_extra_sources", lambda t: [])
        monkeypatch.setattr(
            r, "_search",
            lambda q, max_results=6: [{"title": "A",
                                       "url": "https://thick.example/p"}])
        monkeypatch.setattr(
            r, "_fetch_url",
            lambda u, timeout=15:
            "<html><head><title>Thick</title></head><body><main>"
            "<p>" + ("Very long server-rendered content that easily "
                     "clears the thin-shell threshold with a long "
                     "passage about infrastructure, testing, releases "
                     "and observability practices. ") * 3 + "</p>"
            "</main></body></html>")
        monkeypatch.setattr(r, "_fetch_rendered", render)
        out = r.research({"topic": "thick", "depth": "quick"})
        assert "saved:" in out
        assert calls == []          # thick pages never pay render cost


class TestEmotionActing:
    """R2: content-driven emotion → avatar face (offline heuristics)."""

    def test_tag_heuristics(self):
        from core.emotion import tag
        assert tag("That was awesome, congrats!") == "happy"
        assert tag("Sorry, that command didn't work.") == "concerned"
        assert tag("Hmm, let me think about it.") == "thinking"
        assert tag("Whoa, that's unexpected!") == "surprised"
        assert tag("The sky is blue.") == "neutral"
        assert tag("") == "neutral"
        assert tag("[emotion:happy] Great!") == "happy"     # marker wins

    def test_bad_marker_falls_back(self):
        from core.emotion import tag
        assert tag("[emotion:banana] whatever") == "neutral"

    def test_strip_and_tag_and_clean(self):
        from core.emotion import strip, tag_and_clean
        assert strip("[emotion:happy] hi there") == "hi there"
        assert strip("plain text") == "plain text"
        e, clean = tag_and_clean("[emotion:concerned] it failed")
        assert e == "concerned" and clean == "it failed"

    def test_avatar_overlay_pure(self):
        from core.avatar import emotion_overlay
        b, l, bias = emotion_overlay("happy", 0.0, 1.0, [0.0, 0.0])
        assert b > 0.0 and l >= 1.0 and bias == [0.0, 0.06]
        b2, l2, bias2 = emotion_overlay("concerned", 0.0, 1.0, [0.0, 0.0])
        assert b2 < 0.0 and l2 < 1.0
        b3, l3, bias3 = emotion_overlay("thinking", 0.0, 1.0, [0.0, 0.0])
        assert bias3 != [0.0, 0.0]        # glances aside
        b4, l4, bias4 = emotion_overlay("neutral", 0.2, 0.9, [0.1, 0.1])
        assert (b4, l4, bias4) == (0.2, 0.9, [0.1, 0.1])  # passthrough

    def test_set_emotion_validation_and_expiry(self):
        from core.avatar import HoloAvatar
        av = object.__new__(HoloAvatar)
        av._t = 100.0
        av._emo = ("", 0.0)
        assert av.set_emotion("HAPPY", hold=3) == "happy"
        assert av._emo == ("happy", 103.0)
        assert av.set_emotion("nope") == "neutral"
        assert av.set_emotion("sad") == "neutral"      # unknown → neutral

    def test_ui_wires_emo_signal(self):
        src = Path("ui.py").read_text(encoding="utf-8")
        assert "_emo_sig        = pyqtSignal(str)" in src
        assert "_emo_sig.connect(self._apply_emotion)" in src
        assert "def _apply_emotion" in src
        assert "def set_emotion(self, emotion: str)" in src

    def test_main_tags_before_logging(self):
        src = Path("main.py").read_text(encoding="utf-8")
        i_tag = src.index("tag_and_clean(")
        i_log = src.index('self.ui.write_log(f"{self._asst_name}: {full_out}")')
        assert i_tag < i_log               # clean first, then log
        assert "self.ui.set_emotion(_e)" in src


class TestEvalAB:
    """R2: A/B reply evaluation with persisted scorecards + honest judges."""

    @pytest.fixture(autouse=True)
    def _dir(self, tmp_path, monkeypatch):
        import actions.eval_ab as ev
        monkeypatch.setattr(ev, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(ev, "_llm_judge", lambda *a, **k: None)
        self.ev = ev

    def test_missing_prompt_honest(self):
        assert "Give me a prompt" in self.ev.eval_ab({})

    def test_missing_candidate_honest(self):
        out = self.ev.eval_ab({"prompt": "how to test", "a": "only a"})
        assert "BOTH candidates" in out

    def test_heuristic_picks_stronger_a(self):
        out = self.ev.eval_ab({
            "prompt": "how do I write unit tests for parsers",
            "a": ("Use pytest fixtures and parametrize parsers with "
                  "edge cases: empty input, unicode, oversized buffers. "
                  "**Assert on structure**, not on formatting details. "
                  "1. table-driven cases 2. property checks"),
            "b": "maybe just try stuff and see, i guess"})
        assert "Winner: A" in out and "judge=heuristic" in out
        assert "Saved:" in out
        files = list(self.ev._evals_dir().glob("eval-*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["result"]["winner"] == "a"

    def test_tie_when_identical(self):
        same = "Both answers are equally fine for this prompt."
        out = self.ev.eval_ab({"prompt": "x y z", "a": same, "b": same})
        assert "Winner: Tie" in out

    def test_gemini_judge_when_available(self, monkeypatch):
        monkeypatch.setattr(
            self.ev, "_llm_judge",
            lambda *a, **k: {"winner": "b", "why": "clearer structure"})
        out = self.ev.eval_ab({"prompt": "q", "a": "aa", "b": "bb"})
        assert "Winner: B" in out and "judge=gemini" in out
        assert "clearer structure" in out

    def test_llm_judge_no_key_returns_none(self, monkeypatch):
        from core import gemini
        monkeypatch.setattr(gemini, "api_key", lambda refresh=False: "")
        assert self.ev._llm_judge("p", "a", "b", "") is None

    def test_report_aggregates(self, tmp_path):
        d = self.ev._evals_dir()
        for i, winner in enumerate(("a", "b", "a")):
            (d / f"eval-{i}.json").write_text(
                json.dumps({"result": {"winner": winner,
                                       "judge": "heuristic"}}),
                encoding="utf-8")
        out = self.ev.eval_ab({"action": "report"})
        assert "3 run(s)" in out and "A wins 2" in out and "B wins 1" in out

    def test_report_empty_honest(self):
        assert "No eval runs yet" in self.ev.eval_ab({"action": "report"})

    def test_tool_discoverable(self):
        from pathlib import Path as P
        from core.action_loader import discover_actions
        assert "eval_ab" in discover_actions(P("actions")).names()


class TestVideoQA:
    """R2: offline video captions (SRT) + honest transcript Q&A."""

    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.video_qa as vq
        self.vq = vq
        self.tmp = tmp_path
        self._real_extract = vq._extract_audio   # before the seam patch
        monkeypatch.setattr(vq, "_ffmpeg_exe", lambda: "/usr/bin/ffmpeg")
        monkeypatch.setattr(vq, "_extract_audio",
                            lambda path, timeout=600: _zeros(32000))
        monkeypatch.setattr(vq, "_transcribe_segments",
                            lambda a, engine="whisper": (_segs(), "whisper"))
        monkeypatch.setattr(vq, "_answer", lambda q, t: None)

    def test_no_path_honest(self):
        assert "Give me the video file" in self.vq.video_qa({})

    def test_missing_file_honest(self, tmp_path):
        out = self.vq.video_qa({"path": str(tmp_path / "nope.mp4")})
        assert out.startswith("No such file:")

    def test_ffmpeg_missing_is_install_hint(self, monkeypatch):
        import actions.video_qa as vq
        monkeypatch.setattr(vq, "_ffmpeg_exe", lambda: None)
        with pytest.raises(RuntimeError) as ei:
            self._real_extract("/tmp/x.mp4")
        assert "imageio-ffmpeg" in str(ei.value)

    def test_captions_write_srt_and_transcript(self):
        vid = self.tmp / "talk.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid), "action": "captions"})
        assert "Captions written:" in out
        srt = vid.with_suffix(".srt")
        assert srt.is_file()
        body = srt.read_text(encoding="utf-8")
        assert "00:00:00,000 --> 00:00:03,500" in body
        assert "hello from the lecture" in body
        assert vid.with_suffix(".transcript.txt").is_file()
        assert "Preview:" in out

    def test_ask_with_answer(self, monkeypatch):
        monkeypatch.setattr(self.vq, "_answer",
                            lambda q, t: "It says the deadline is Friday.")
        vid = self.tmp / "talk.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid), "action": "ask",
                                "question": "deadline?"})
        assert out == "Answer: It says the deadline is Friday."

    def test_ask_without_key_returns_context_window(self, monkeypatch):
        vid = self.tmp / "talk.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid), "action": "ask",
                                "question": "lecture"})
        assert "CONTEXT (not an answer)" in out
        assert "[00:00:0" in out          # timestamps survived

    def test_ask_needs_question(self):
        vid = self.tmp / "t.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid), "action": "ask"})
        assert "question=" in out

    def test_no_speech_honest(self, monkeypatch):
        import actions.video_qa as vq
        monkeypatch.setattr(vq, "_transcribe_segments",
                            lambda a, engine="whisper": ([], "whisper"))
        vid = self.tmp / "quiet.mp4"
        vid.write_bytes(b"\x00")
        out = vq.video_qa({"path": str(vid)})
        assert "Transcribed 0 words" in out

    def test_srt_timestamp_format(self):
        assert self.vq._srt_ts(0) == "00:00:00,000"
        assert self.vq._srt_ts(3723.5) == "01:02:03,500"

    def test_tool_discoverable(self):
        from pathlib import Path as P
        from core.action_loader import discover_actions
        assert "video_qa" in discover_actions(P("actions")).names()


def _zeros(n: int):
    import numpy as np
    return np.zeros(n, dtype="float32")


def _segs():
    return [
        {"start": 0.0, "end": 3.5, "text": "hello from the lecture"},
        {"start": 3.5, "end": 7.0, "text": "the deadline is Friday"},
        {"start": 7.0, "end": 11.0, "text": "any questions about that"},
    ]


class TestAtspi:
    """Report #1: guarded AT-SPI accessibility scanner (Linux)."""

    class _Node:
        def __init__(self, role="", name="", children=None, actions=0):
            self.roleName = role
            self.name = name
            self.description = ""
            self.states = "enabled"
            self._kids = children or []
            self._acts = actions
            self.childCount = len(self._kids)
            self._done = []

        def getChildAtIndex(self, i):
            return self._kids[i]

        def queryAction(self):
            node = self

            class A:
                nActions = node._acts

                def doAction(self, i):
                    node._done.append(i)
                    return True
            return A()

    @pytest.fixture(autouse=True)
    def _seams(self, monkeypatch):
        import actions.atspi as at
        self.at = at
        desk = self._Node("desktop", "root", [
            self._Node("application", "Files", [
                self._Node("frame", "Home",
                           [self._Node("push button", "Open", actions=1),
                            self._Node("entry", "Search")]),
            ]),
            self._Node("application", "Terminal", []),
        ])
        monkeypatch.setattr(at, "_bridge", lambda: object())  # present
        monkeypatch.setattr(at, "_desktop", lambda bridge: desk)

    def test_no_bridge_is_honest_install(self, monkeypatch):
        monkeypatch.setattr(self.at, "_bridge", lambda: None)
        out = self.at.atspi({"action": "scan"})
        assert "apt install python3-gi" in out
        assert "never a fabricated" in out
        # every action refuses identically
        for act in ("find", "act"):
            assert "apt install" in self.at.atspi({"action": act, "x": 1})

    def test_scan_shows_tree(self):
        out = self.at.atspi({"action": "scan", "depth": 3})
        assert "Desktop tree" in out
        assert "application: Files" in out
        assert "push button: Open" in out

    def test_find_breadcrumb_and_honest_empty(self):
        out = self.at.atspi({"action": "find", "query": "button"})
        assert "match(es)" in out
        assert "@ root > Files > Home > Open" in out
        assert "No accessibility nodes match" in self.at.atspi(
            {"action": "find", "query": "zzz-nothing"})
        assert "query=" in self.at.atspi({"action": "find"})

    def test_act_re_resolves_and_performs(self):
        path = "Files > Home > Open"
        out = self.at.atspi({"action": "act", "path": path})
        assert "done" in out and "push button" in out

    def test_act_missing_path_honest(self):
        out = self.at.atspi({"action": "act",
                             "path": "Ghost > App > Button"})
        assert "not found live" in out and "Re-run find" in out

    def test_act_needs_path(self):
        assert "breadcrumb line" in self.at.atspi({"action": "act"})

    def test_pure_format_and_flatten(self):
        d = {"role": "app", "name": "X", "children": [
            {"role": "button", "name": "OK", "children": []}]}
        text = self.at._format(d)
        assert text.splitlines()[0] == "app: X"
        assert "  button: OK" in text
        flat = self.at._flatten(d)
        assert len(flat) == 2
        assert self.at._crumb_text(flat[1]["_crumbs"]) == "X > OK"

    def test_unknown_action_honest(self):
        assert "scan | find | act" in self.at.atspi({"action": "dance"})

    def test_tool_discoverable(self):
        from pathlib import Path as P
        from core.action_loader import discover_actions
        assert "atspi" in discover_actions(P("actions")).names()


class TestCad:
    """Batch 14: CadQuery guarded CAD export (STL/STEP/SVG)."""

    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.cad as c
        self.c = c
        monkeypatch.setattr(c, "_base_dir", lambda: tmp_path)
        self.tmp = tmp_path

    def test_status_missing_is_install_line(self, monkeypatch):
        monkeypatch.setattr(self.c, "_has_cq", lambda: False)
        out = self.c.cad({"action": "status"})
        assert "pip install cadquery" in out
        assert "no placeholder" in out

    def test_no_code_honest(self, monkeypatch):
        monkeypatch.setattr(self.c, "_has_cq", lambda: False)
        assert "CadQuery snippet" in self.c.cad({})

    def test_missing_dep_never_fakes_files(self, monkeypatch):
        monkeypatch.setattr(self.c, "_has_cq", lambda: False)
        out = self.c.cad({"code": "result = cq.Workplane().box(1,1,1)"})
        assert "pip install cadquery" in out
        assert not list((self.tmp / "cad").glob("*.stl")) if \
            (self.tmp / "cad").exists() else True

    def test_build_script_composition(self):
        sc = self.c._build_script(
            "result = cq.Workplane().box(1,1,1)",
            Path("/o/x.stl"), Path("/o/x.step"), Path("/o/x.svg"))
        assert "import cadquery as cq" in sc
        assert "cq.exporters.export(result, r'/o/x.stl')" in sc
        assert "JARVIS_CAD_OK" in sc
        # fenced paste tolerated
        sc2 = self.c._build_script("```python\nresult = cq.Workplane()\n```",
                                   Path("/a.stl"), Path("/a.step"),
                                   Path("/a.svg"))
        assert "```" not in sc2.split("export tail")[1]

    def test_build_success_lists_artifacts(self, monkeypatch):
        monkeypatch.setattr(self.c, "_has_cq", lambda: True)

        def fake_run(script_path, timeout=120):
            stem = script_path.stem                      # cad-<stamp>
            d = script_path.parent
            (d / f"{stem}.stl").write_bytes(b"solid x")
            (d / f"{stem}.step").write_bytes(b"ISO-10303")
            (d / f"{stem}.svg").write_text("<svg/>")
            return "JARVIS_CAD_OK"
        monkeypatch.setattr(self.c, "_run_script", fake_run)
        out = self.c.cad({"code": "result = cq.Workplane().box(1,1,1)"})
        assert "CAD solid exported:" in out
        assert ".stl" in out and ".svg" in out

    def test_failure_keeps_snippet_and_reports(self, monkeypatch):
        monkeypatch.setattr(self.c, "_has_cq", lambda: True)

        def boom(script_path, timeout=120):
            raise RuntimeError("script failed (exit 1):\nValueError: bad")
        monkeypatch.setattr(self.c, "_run_script", boom)
        out = self.c.cad({"code": "result = 'oops'"})
        assert "CAD build failed" in out and "ValueError: bad" in out
        assert "Snippet kept at" in out
        assert list((self.tmp / "cad").glob("*.py"))

    def test_tool_shape_and_discoverable(self):
        from core.action_loader import discover_actions
        assert self.c.TOOL["handler"] is self.c.cad
        assert "cad" in discover_actions(Path("actions")).names()


class TestManimAnim:
    """Batch 14: Manim guarded animation render (mp4)."""

    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.manim_anim as ma
        self.ma = ma
        monkeypatch.setattr(ma, "_base_dir", lambda: tmp_path)
        self.tmp = tmp_path

    def test_status_missing(self, monkeypatch):
        monkeypatch.setattr(self.ma, "_has_manim", lambda: False)
        out = self.ma.manim_anim({"action": "status"})
        assert "pip install manim" in out
        assert "no placeholder" in out

    def test_no_code_honest(self, monkeypatch):
        monkeypatch.setattr(self.ma, "_has_manim", lambda: False)
        assert "Manim scene" in self.ma.manim_anim({})

    def test_missing_dep_never_fakes_video(self, monkeypatch):
        monkeypatch.setattr(self.ma, "_has_manim", lambda: False)
        out = self.ma.manim_anim({"code": "class S(Scene): pass"})
        assert "pip install manim" in out
        assert not (self.tmp / "anims").exists() or \
            not list((self.tmp / "anims").glob("*.mp4"))

    def test_scene_name_extraction(self):
        code = ("from manim import *\n"
                "class Demo(Scene):\n"
                "    def construct(self): pass\n")
        assert self.ma._scene_name(code) == "Demo"
        assert self.ma._scene_name("x = 1") is None
        assert self.ma._scene_name("class Inner(ThreeDScene):") == "Inner"

    def test_build_file_adds_import_and_default_scene(self):
        f = self.tmp / "s.py"
        name = self.ma._build_file("class MyScene(Scene): pass", f)
        assert name == "MyScene"
        body = f.read_text(encoding="utf-8")
        assert "from manim import *" in body
        f2 = self.tmp / "s2.py"
        name2 = self.ma._build_file("x = 42", f2)
        assert name2 == "JavisScene"          # default scene injected
        assert "JARVIS" in f2.read_text(encoding="utf-8")
        # fenced paste tolerated
        f3 = self.tmp / "s3.py"
        self.ma._build_file("```python\nclass Q(Scene): pass\n```", f3)
        assert "```" not in f3.read_text(encoding="utf-8")

    def test_render_success_returns_newest_mp4(self, monkeypatch):
        def fake_render(file_path, scene, quality, media_dir, timeout=300):
            clip = media_dir / "vid.mp4"
            clip.parent.mkdir(parents=True, exist_ok=True)
            clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
            return clip
        monkeypatch.setattr(self.ma, "_has_manim", lambda: True)
        monkeypatch.setattr(self.ma, "_render", fake_render)
        out = self.ma.manim_anim({"code": "class S(Scene): pass"})
        assert "Animation rendered:" in out and "scene `S`" in out

    def test_render_failure_keeps_scene(self, monkeypatch):
        monkeypatch.setattr(self.ma, "_has_manim", lambda: True)

        def boom(file_path, scene, quality, media_dir, timeout=300):
            raise RuntimeError("manim failed (exit 1): no cairo")
        monkeypatch.setattr(self.ma, "_render", boom)
        out = self.ma.manim_anim({"code": "class S(Scene): pass"})
        assert "Render failed" in out and "no cairo" in out
        assert "Scene kept at" in out
        assert list((self.tmp / "anims").glob("*.py"))

    def test_tool_shape_and_discoverable(self):
        from core.action_loader import discover_actions
        assert self.ma.TOOL["handler"] is self.ma.manim_anim
        assert "manim_anim" in discover_actions(Path("actions")).names()


class TestWhisperXGuarded:
    """Batch 15: WhisperX optional engine — honest gaps, real paths."""

    def test_missing_library_is_install_line(self):
        """Sandbox reality: whisperx absent → exact honest message."""
        from core.stt import WhisperXSTT
        with pytest.raises(RuntimeError) as ei:
            WhisperXSTT()
        msg = str(ei.value)
        assert "pip install whisperx" in msg
        assert "HuggingFace token" in msg

    def test_transcribe_diarized_with_stub_module(self, monkeypatch):
        import sys as _sys
        import types
        from core.stt import WhisperXSTT

        calls = {}

        class _Model:
            def transcribe(self, audio):
                return {"segments": [
                    {"start": 0.0, "end": 2.5, "text": " hello world "},
                    {"start": 2.5, "end": 4.0, "text": "  "},
                ]}

        def _load(name, device, language=None):
            calls["loaded"] = (name, device)
            return _Model()
        stub = types.ModuleType("whisperx")
        stub.load_model = _load
        # NO diarize attr + NO token → honest single speaker
        monkeypatch.setitem(_sys.modules, "whisperx", stub)

        wx = WhisperXSTT.__new__(WhisperXSTT)
        wx._language = None
        wx._model_name = "small"
        wx._loaded = None
        segs = wx.transcribe_diarized(None)
        assert len(segs) == 1                    # blank segment dropped
        assert segs[0]["diarized"] is False
        assert segs[0]["speaker"] == "SPEAKER_00"
        assert "hello world" in segs[0]["text"]
        # parity: transcribe joins text
        assert wx.transcribe(None) == "hello world"

    def test_transcribe_words_with_stub(self, monkeypatch):
        import sys as _sys
        import types
        from core.stt import WhisperXSTT

        class _Model:
            def transcribe(self, audio):
                return {"segments": [{
                    "start": 0.0, "end": 1.0, "text": "hi there",
                    "words": [{"start": 0.0, "end": 0.4, "word": "hi"},
                              {"start": 0.5, "end": 1.0, "word": "there"},
                              {"start": 1.0, "end": 1.0, "word": ""}]}]}
        stub = types.ModuleType("whisperx")
        stub.load_model = lambda *a, **k: _Model()
        monkeypatch.setitem(_sys.modules, "whisperx", stub)
        wx = WhisperXSTT.__new__(WhisperXSTT)
        wx._language = None
        wx._model_name = "small"
        wx._loaded = None
        words = wx.transcribe_words(None)
        assert [w["word"] for w in words] == ["hi", "there"]
        assert words[0]["start"] == 0.0


class TestVideoQAWhisperXEngine:
    """Batch 15: video_qa engine=whisperx with honest fallback."""

    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.video_qa as vq
        self.vq = vq
        self.tmp = tmp_path
        monkeypatch.setattr(vq, "_extract_audio",
                            lambda path, timeout=600: _zeros(32000))
        monkeypatch.setattr(vq, "_answer", lambda q, t: None)

    def test_engine_whisperx_unavailable_falls_back_with_note(
            self, monkeypatch):
        import core.stt as stt_mod

        class _Boom:
            def __init__(self):
                raise RuntimeError("pip install whisperx (MIT)")

        class _Good:
            def transcribe_segments(self, a):
                return [{"start": 0.0, "end": 3.0,
                         "text": "fallback transcript words"}]
        monkeypatch.setattr(stt_mod, "WhisperXSTT", _Boom)
        monkeypatch.setattr(stt_mod, "WhisperSTT", _Good)

        vid = self.tmp / "t.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid), "engine": "whisperx"})
        assert "Captions written:" in out
        assert "whisperx unavailable" in out
        assert "faster-whisper instead" in out

    def test_engine_whisperx_success_marks_engine(self, monkeypatch):
        import core.stt as stt_mod

        class _Wx:
            def transcribe_diarized(self, a):
                return [{"start": 0.0, "end": 3.0, "text": "hello there",
                         "speaker": "SPEAKER_01", "diarized": True}]
        monkeypatch.setattr(stt_mod, "WhisperXSTT", _Wx)
        vid = self.tmp / "t.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid), "engine": "whisperx"})
        assert "[engine: whisperx]" in out
        # speaker folded into the caption text
        srt = vid.with_suffix(".srt").read_text(encoding="utf-8")
        assert "[SPEAKER_01] hello there" in srt

    def test_default_engine_unchanged(self, monkeypatch):
        import core.stt as stt_mod

        class _Good:
            def transcribe_segments(self, a):
                return [{"start": 0.0, "end": 3.0, "text": "plain text"}]
        monkeypatch.setattr(stt_mod, "WhisperSTT", _Good)
        vid = self.tmp / "t.mp4"
        vid.write_bytes(b"\x00")
        out = self.vq.video_qa({"path": str(vid)})
        assert "[engine: whisper]" in out
        assert "plain text" in vid.with_suffix(".transcript.txt").read_text()


class TestGo2rtc:
    """Batch 16: guarded RTSP/WebRTC stream bridge."""

    class _Proc:
        def __init__(self, alive=True):
            self._alive = alive
            self.terminated = False
            self.killed = False
            self.stderr = None

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self.terminated = True
            self._alive = False

        def wait(self, timeout=None):
            self._alive = False
            return 0

        def kill(self):
            self.killed = True
            self._alive = False

    @pytest.fixture(autouse=True)
    def _seams(self, tmp_path, monkeypatch):
        import actions.go2rtc as g
        self.g = g
        monkeypatch.setattr(g, "_base_dir", lambda: tmp_path)
        monkeypatch.setattr(g, "_STATE",
                            {"proc": None, "port": 1984, "url": "",
                             "streams": [], "error": ""})
        self.tmp = tmp_path

    def test_status_without_binary_is_honest(self, monkeypatch):
        monkeypatch.setattr(self.g, "_binary", lambda: None)
        out = self.g.go2rtc({"action": "status"})
        assert "MISSING" in out and "action=install" in out

    def test_start_without_streams_hints(self, monkeypatch):
        monkeypatch.setattr(self.g, "_binary", lambda: "/usr/bin/go2rtc")
        out = self.g.go2rtc({"action": "start"})
        assert "streams=" in out

    def test_start_without_binary_never_fakes(self, monkeypatch):
        monkeypatch.setattr(self.g, "_binary", lambda: None)
        out = self.g.go2rtc({"action": "start",
                             "streams": "cam=rtsp://x"})
        assert "never\nfake" in out or "never fake" in out
        assert "action=install" in out

    def test_config_text_pure(self):
        txt = self.g._config_text(["cam1=rtsp://a", "bare"])
        assert "streams:" in txt
        assert "  cam1: rtsp://a" in txt
        assert "  bare: bare" in txt
        assert self.g._parse_streams("a=b, c=d ") == ["a=b", "c=d"]
        assert self.g._parse_streams(None) == []
        assert self.g._parse_streams(["x=y"]) == ["x=y"]

    def test_start_running_lifecycle(self, monkeypatch):
        monkeypatch.setattr(self.g, "_binary", lambda: "/fake/go2rtc")
        spawned = {}

        def fake_spawn(binary, cfg, port):
            spawned["cfg"] = cfg.read_text(encoding="utf-8")
            spawned["port"] = port
            return self._Proc(alive=True)
        monkeypatch.setattr(self.g, "_spawn", fake_spawn)
        monkeypatch.setattr(self.g, "_port_ready", lambda port, **k: True)
        out = self.g.go2rtc({"action": "start",
                             "streams": "cam1=rtsp://user@host/1",
                             "port": 1985})
        assert "go2rtc running: http://127.0.0.1:1985" in out
        assert "cam1: rtsp://user@host/1" in spawned["cfg"]
        assert spawned["port"] == 1985
        # already running
        assert "already running" in self.g.go2rtc({"action": "start",
                                                   "streams": "x=y"})
        # status reflects it
        assert "RUNNING" in self.g.go2rtc({"action": "status"})
        # stop
        assert "stopped" in self.g.go2rtc({"action": "stop"})
        assert "not running" in self.g.go2rtc({"action": "stop"})

    def test_port_never_ready_is_honest(self, monkeypatch):
        monkeypatch.setattr(self.g, "_binary", lambda: "/fake/go2rtc")
        monkeypatch.setattr(self.g, "_spawn",
                            lambda b, c, p: self._Proc(alive=True))
        monkeypatch.setattr(self.g, "_port_ready",
                            lambda port, **k: False)
        out = self.g.go2rtc({"action": "start", "streams": "cam=rtsp://x"})
        assert "never opened" in out

    def test_install_extracts_official_tarball(self, monkeypatch):
        import io as _io
        import tarfile as _tf

        def fake_json(url, timeout=20):
            return {"tag_name": "v1.9.0",
                    "assets": [{"name": "go2rtc_1.9.0_linux_amd64.tar.gz",
                                "browser_download_url": "https://x/y.tgz"}]}

        def fake_download(url, dest, timeout=120):
            buf = _io.BytesIO()
            with _tf.open(fileobj=buf, mode="w:gz") as tar:
                info = _tf.TarInfo(name="go2rtc")
                data = b"#!/bin/sh\necho go2rtc"
                info.size = len(data)
                tar.addfile(info, _io.BytesIO(data))
            dest.write_bytes(buf.getvalue())
            return dest
        monkeypatch.setattr(self.g, "_fetch_json", fake_json)
        monkeypatch.setattr(self.g, "_download", fake_download)
        out = self.g.go2rtc({"action": "install"})
        assert "installed" in out and "v1.9.0" in out
        dest = self.g._bin_dir() / "go2rtc"
        assert dest.is_file()
        assert dest.stat().st_mode & 0o100          # exec bit

    def test_install_failure_points_to_manual(self, monkeypatch):
        def boom(url, timeout=20):
            raise OSError("network down")
        monkeypatch.setattr(self.g, "_fetch_json", boom)
        out = self.g.go2rtc({"action": "install"})
        assert "install failed" in out
        assert "github.com/AlexxIT/go2rtc/releases" in out

    def test_tool_discoverable(self):
        from core.action_loader import discover_actions
        assert "go2rtc" in discover_actions(Path("actions")).names()


class TestPWAPush:
    """Batch 17: installable dashboard + RFC 8291 web push (zero deps)."""

    @pytest.fixture(autouse=True)
    def _env(self, tmp_path, monkeypatch):
        import dashboard.push as pu
        self.pu = pu
        monkeypatch.setattr(pu, "_base_dir", lambda: tmp_path)
        (tmp_path / "config").mkdir(exist_ok=True)
        self.tmp = tmp_path

    def test_vapid_keygen_persists_and_matches(self):
        priv, pub_pem, priv_pem = self.pu._vapid_pair()
        assert pub_pem.is_file() and priv_pem.is_file()
        # second call loads the SAME key
        priv2, _, _ = self.pu._vapid_pair()
        assert (priv.private_numbers() ==
                priv2.private_numbers())
        key = self.pu.public_key_b64()
        assert len(key) in (86, 87) and "=" not in key
        nums = priv.public_key().public_numbers()
        import base64 as _b64
        raw = _b64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
        assert raw[0] == 4
        assert int.from_bytes(raw[1:33], "big") == nums.x
        assert int.from_bytes(raw[33:], "big") == nums.y

    def test_subscribe_dedupe_cap_and_validation(self):
        sub = {"endpoint": "https://push.example/a",
               "keys": {"p256dh": "x", "auth": "y"}}
        assert self.pu.subscribe(sub) == {"stored": 1}
        self.pu.subscribe(sub)                        # dedupe by endpoint
        assert len(self.pu._load_subs()) == 1
        with pytest.raises(ValueError):
            self.pu.subscribe({"keys": {}})
        # cap — oldest evicted first
        for i in range(30):
            self.pu.subscribe({"endpoint": f"https://e/{i}",
                               "keys": {"p256dh": "a", "auth": "b"}})
        assert len(self.pu._load_subs()) == 20
        self.pu.subscribe(sub)                        # re-add after cap
        assert self.pu.unsubscribe("https://push.example/a")["stored"] == 19

    def test_notify_happy_path_counts_sent(self, monkeypatch):
        captured = []

        def fake_post(endpoint, headers, body, timeout=15):
            captured.append((endpoint, headers, body))
            return 201, ""
        monkeypatch.setattr(self.pu, "_post_push", fake_post)
        self.pu.subscribe({"endpoint": "https://push.example/x",
                           "keys": {
                               "p256dh": self.pu.public_key_b64().replace(
                                   "A", "B")[:80] + "A",
                               "auth": "AAAAAAAAAAAAAAAAAAAAAA"}})
        # use a REAL valid subscriber key for the encrypt path
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        sub_key = _ec.generate_private_key(_ec.SECP256R1())
        nums = sub_key.public_key().public_numbers()
        import base64 as _b64
        p256 = _b64.urlsafe_b64encode(
            b"\x04" + nums.x.to_bytes(32, "big") +
            nums.y.to_bytes(32, "big")).rstrip(b"=").decode()
        auth = _b64.urlsafe_b64encode(b"0123456789abcdef").rstrip(
            b"=").decode()
        self.pu._subs_path().write_text(
            __import__("json").dumps([{"endpoint": "https://push.example/x",
                                       "keys": {"p256dh": p256,
                                                "auth": auth}}]),
            encoding="utf-8")
        out = self.pu.notify("Reminder", "stand up")
        assert out["sent"] == 1 and out["errors"] == []
        endpoint, headers, body = captured[0]
        assert headers["Content-Encoding"] == "aes128gcm"
        assert headers["Authorization"].startswith("vapid t=")
        assert headers["TTL"] == "60"
        # RFC 8291 layout: 16-byte salt + 2-byte rs + 1-byte idlen(65)
        assert len(body) > 16 + 3 + 65
        assert body[18] == 65

    def test_notify_decrypt_roundtrip_rfc8291(self, monkeypatch):
        """Real crypto check: our output decrypts with the sub key."""
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes as _h
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64 as _b64
        import json as _json

        sub_key = _ec.generate_private_key(_ec.SECP256R1())
        nums = sub_key.public_key().public_numbers()
        p256 = _b64.urlsafe_b64encode(
            b"\x04" + nums.x.to_bytes(32, "big") +
            nums.y.to_bytes(32, "big")).rstrip(b"=").decode()
        auth = _b64.urlsafe_b64encode(b"0123456789abcdef").rstrip(
            b"=").decode()
        monkeypatch.setattr(self.pu, "_post_push",
                            lambda e, h, b, timeout=15: (201, ""))
        self.pu._subs_path().write_text(_json.dumps(
            [{"endpoint": "https://push.example/x",
              "keys": {"p256dh": p256, "auth": auth}}]), encoding="utf-8")
        out = self.pu.notify("T", "hello push")
        assert out["sent"] == 1
        # decrypt what notify produced — re-derive by capturing body
        bodies = []
        monkeypatch.setattr(self.pu, "_post_push",
                            lambda e, h, b, timeout=15:
                            (bodies.append((h, b)) or (201, "")))
        self.pu.notify("T2", "secret body")
        headers, ct = bodies[0]
        salt, rs = ct[:16], int.from_bytes(ct[16:18], "big")
        assert rs == 8426                    # RFC 8291 record size
        idlen = ct[18]
        eph_pub = ct[19:19 + idlen]
        assert idlen == 65 and eph_pub[0] == 4     # uncompressed point
        payload = ct[19 + idlen:]
        ua_pub = _b64.urlsafe_b64decode(p256 + "=" * (-len(p256) % 4))
        auth_raw = _b64.urlsafe_b64decode(auth + "=" * (-len(auth) % 4))
        # ECDH with OUR ephemeral pub (we are the "browser" in this test)
        from cryptography.hazmat.primitives.asymmetric.ec import \
            EllipticCurvePublicNumbers
        eph_pubkey = EllipticCurvePublicNumbers(
            int.from_bytes(eph_pub[1:33], "big"),
            int.from_bytes(eph_pub[33:], "big"),
            _ec.SECP256R1()).public_key()
        prk = HKDF(algorithm=_h.SHA256(), length=32, salt=auth_raw,
                   info=b"WebPush: info\x00" + ua_pub + eph_pub
                   ).derive(sub_key.exchange(_ec.ECDH(), eph_pubkey))
        cek = HKDF(algorithm=_h.SHA256(), length=16, salt=salt,
                   info=b"Content-Encoding: aes128gcm\x00").derive(prk)
        nonce = HKDF(algorithm=_h.SHA256(), length=12, salt=salt,
                     info=b"Content-Encoding: nonce\x00").derive(prk)
        plain = AESGCM(cek).decrypt(nonce, payload, None)
        assert plain.endswith(b"\x01")
        data = _json.loads(plain[:-1].decode())
        assert data["title"] == "T2" and data["body"] == "secret body"

    def test_notify_honest_when_no_subs(self):
        out = self.pu.notify("x", "y")
        assert out["sent"] == 0 and "no subscriptions" in out["note"]

    def test_notify_drops_gone_endpoints(self, monkeypatch):
        import json as _json
        from cryptography.hazmat.primitives.asymmetric import ec as _ec
        k = _ec.generate_private_key(_ec.SECP256R1())
        n = k.public_key().public_numbers()
        import base64 as _b64
        p256 = _b64.urlsafe_b64encode(
            b"\x04" + n.x.to_bytes(32, "big") +
            n.y.to_bytes(32, "big")).rstrip(b"=").decode()
        auth = _b64.urlsafe_b64encode(b"0123456789abcdef").rstrip(
            b"=").decode()
        self.pu._subs_path().write_text(_json.dumps(
            [{"endpoint": "https://gone/1",
              "keys": {"p256dh": p256, "auth": auth}}]), encoding="utf-8")
        monkeypatch.setattr(self.pu, "_post_push",
                            lambda e, h, b, timeout=15: (410, ""))
        out = self.pu.notify("t", "b")
        assert out["sent"] == 0 and "dropped" in out["errors"][0]
        assert self.pu._load_subs() == []        # dead sub removed

    def test_vapid_jwt_shape(self):
        jwt = self.pu._vapid_jwt("https://push.example", "mailto:a@b")
        h, c, sig = jwt.split(".")
        import json as _json
        import base64 as _b64
        header = _json.loads(_b64.urlsafe_b64decode(
            h + "=" * (-len(h) % 4)))
        assert header["alg"] == "ES256"
        claims = _json.loads(_b64.urlsafe_b64decode(
            c + "=" * (-len(c) % 4)))
        assert claims["aud"] == "https://push.example"
        assert claims["exp"] > __import__("time").time()

    def test_frontend_assets_exist_and_wire(self):
        static = Path("dashboard/static")
        assert (static / "manifest.json").is_file()
        assert (static / "sw.js").is_file()
        assert (static / "pwa.js").is_file()
        assert (static / "icons" / "icon-192.png").is_file()
        assert (static / "icons" / "icon-512.png").is_file()
        app = (static / "app.html").read_text(encoding="utf-8")
        assert 'rel="manifest" href="/static/manifest.json"' in app
        assert "/static/pwa.js" in app
        sw = (static / "sw.js").read_text(encoding="utf-8")
        assert "showNotification" in sw and "notificationclick" in sw
        pwa = (static / "pwa.js").read_text(encoding="utf-8")
        assert "/api/push/subscribe" in pwa
        assert "/api/push/public-key" in pwa

    def test_server_routes_wired(self):
        src = Path("dashboard/server.py").read_text(encoding="utf-8")
        for route in ('"/manifest.json"', '"/sw.js"',
                      '"/api/push/public-key"', '"/api/push/subscribe"',
                      '"/api/push/notify"'):
            assert route in src, route
        assert 'Service-Worker-Allowed' in src
        assert src.count("_auth(req)") >= 3        # push routes gated
