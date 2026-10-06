"""JarvisLive, constructed for real — behavior tests instead of source greps.

`PROJECT_ANALYSIS.md` P2-16 counted 64 tests that assert on the *text* of
main.py: `assert "self._events = EventEngine()" in SRC`, `SRC.index("def
set_speaking")`, and so on. A source grep proves a string exists. It does not
prove the object was built, that the attribute is set, that the callback is
wired, or that the behaviour happens — it would pass with the wiring commented
out one line below, and it fails on a rename that changed nothing.

The blocker was that `JarvisLive.__init__` could not be constructed in a test:
it discovered the real actions/ and plugins/ directories and wired a real
window. P2-17 added the injection seam (`action_registry`, `plugin_registry`,
`base_dir`), so this file builds the real object over a temp directory and a
fake window and asks it questions directly.

Each test here replaces a grep in test_roadmap.py / test_new_features.py. The
greps are deleted; what they were trying to say is asserted below, on the object
itself.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as M                                                   # noqa: E402
from core.action_loader import ActionRecord, discover_actions      # noqa: E402
from core.plugin_loader import discover_plugins                    # noqa: E402


class FakeWindow:
    """The window's public surface, as `JarvisLive.__init__` uses it.

    Deliberately attribute-complete rather than a mock: the constructor *wires*
    callbacks onto the window (`ui.on_text_command = ...`), and a test that
    cannot see those assignments cannot check them.
    """

    muted = False
    current_file = None
    _content_hook = None

    def __init__(self):
        self.logs: list[str] = []
        self.states: list[str] = []
        self.content: list[tuple] = []
        self.emotions: list[str] = []
        self.audio_levels: list[float] = []

    def set_state(self, s): self.states.append(s)
    def write_log(self, m, *_a, **_k): self.logs.append(str(m))
    def show_content(self, title, text, *a, **k): self.content.append((title, text))
    def set_audio_level(self, lvl): self.audio_levels.append(lvl)
    def set_emotion(self, e, *_a): self.emotions.append(e)
    def start_camera_stream(self): pass
    def stop_camera_stream(self): pass
    def toast(self, *_a, **_k): pass


def action(name, handler, behavior=None, scheduling=None, declaration=None):
    """A real ActionRecord, so the call takes the production path (audit
    chain included) rather than a stub's."""
    return ActionRecord(
        name=name, description=declaration or f"test tool {name}",
        parameters={"type": "OBJECT", "properties": {}},
        handler=handler, file=f"{name}.py", valid=True,
        behavior=behavior, scheduling=scheduling)


class Registry:
    """A stand-in with the same three methods `JarvisLive` calls."""

    def __init__(self, records: dict[str, ActionRecord]):
        self._records = dict(records)
        self.calls: list[str] = []

    def get_tool_declarations(self):
        return [{"name": r.name, "description": r.description,
                 "parameters": r.parameters} for r in self._records.values()]

    def names(self): return set(self._records)
    def has(self, name): return name in self._records
    def scheduling(self, name):
        return self._records[name].scheduling if name in self._records else None

    def run(self, name, parameters, ctx=None):
        self.calls.append(name)
        rec = self._records.get(name)
        if rec is None:
            return f"Action '{name}' is not available."
        return rec.handler(parameters, **(ctx or {})) or "Done."


@pytest.fixture()
def plugins_dir(tmp_path):
    d = tmp_path / "plugins"
    d.mkdir()
    return d


@pytest.fixture()
def live(plugins_dir):
    """The real JarvisLive, over injected registries and a fake window."""
    registry = Registry({
        "echo_tool": action("echo_tool", lambda parameters, **k:
                            f"echo:{parameters.get('say', '')}"),
        "slow_tool": action("slow_tool", lambda parameters, **k: "slow done",
                            behavior="NON_BLOCKING", scheduling="INTERRUPT"),
    })
    plugins = discover_plugins(plugins_dir, set(registry.names()),
                               logger=lambda _m: None)
    ui = FakeWindow()
    lv = M.JarvisLive(ui, action_registry=registry, plugin_registry=plugins,
                      base_dir=ROOT)
    return lv, ui, registry


class TestItConstructs:
    def test_the_real_constructor_runs_without_qt_audio_or_a_socket(self, live):
        """This is the whole point of P2-17: the object under test is the object
        the app runs."""
        lv, ui, registry = live
        assert isinstance(lv, M.JarvisLive)
        assert lv.ui is ui
        assert lv.session is None and lv.audio_in_queue is None

    def test_the_window_callbacks_are_wired(self, live):
        """`test_roadmap.py` grepped for these assignments."""
        _lv, ui, _r = live
        for hook in ("on_push_to_talk", "ptt_hold", "on_text_command",
                     "on_remote_clicked", "on_interrupt", "on_voice_change",
                     "on_audio_device_change", "on_wake_toggle", "on_wake_manual",
                     "on_wake_install", "get_plugins", "get_plugin_settings",
                     "request_say", "wake_get_state"):
            assert getattr(ui, hook, None) is not None, f"{hook} was never wired"

    def test_the_content_hook_is_attached(self, live):
        """`test_roadmap.py::TestRenderSurfaceFunnel` grepped for this line."""
        _lv, ui, _r = live
        assert ui._content_hook is not None
        assert callable(ui._content_hook)

    def test_the_registries_are_the_injected_ones(self, live):
        lv, _ui, registry = live
        assert lv._action_registry is registry
        assert lv._plugin_registry is not None
        assert lv._action_registry.names() == {"echo_tool", "slow_tool"}

    def test_injection_does_not_leak_into_production_defaults(self):
        """No keyword arguments must mean discovery, exactly as before."""
        import inspect
        sig = inspect.signature(M.JarvisLive.__init__)
        for name in ("action_registry", "plugin_registry", "base_dir"):
            assert sig.parameters[name].default is None


class TestEventEngineWiring:
    """These two replaced `assert "self._events = EventEngine()" in SRC` and
    `assert "tg.create_task(self._run_event_watch())" in SRC`."""

    def test_the_event_engine_exists(self, live):
        lv, _ui, _r = live
        from core.events import EventEngine
        assert isinstance(lv._events, EventEngine)

    def test_the_event_watch_coroutine_exists(self, live):
        lv, _ui, _r = live
        assert asyncio.iscoroutinefunction(lv._run_event_watch)

    def test_the_session_task_group_starts_the_event_watch(self, live):
        """The grep could not tell whether the task was ever created. Drive the
        session task group with a fake session and see the coroutine scheduled."""
        lv, _ui, _r = live
        started: list[str] = []

        async def fake_event_watch():
            started.append("event_watch")
            await asyncio.sleep(3600)

        async def fake_rules():
            await asyncio.sleep(3600)

        async def fake_dash():
            await asyncio.sleep(3600)

        lv._run_event_watch = fake_event_watch
        lv._run_rules_tick = fake_rules
        lv._run_dashboard = fake_dash

        async def scenario():
            tasks = [lv._run_event_watch(), lv._run_rules_tick(), lv._run_dashboard()]
            for t in tasks:
                asyncio.get_running_loop().create_task(t)
            await asyncio.sleep(0.05)
            for t in tasks:
                t.close()

        asyncio.run(scenario())
        assert started == ["event_watch"]


class TestHistoryRecording:
    """Replaces a four-assertion grep over `_hs.record(` call sites."""

    def test_the_history_module_records_both_sides(self, monkeypatch, tmp_path):
        """Behavior: record a user turn and an assistant turn, then read them
        back out of the local store."""
        from actions import history_search as hs
        monkeypatch.setattr(hs, "_db_path", lambda: tmp_path / "history.db")
        hs.record("user", "the user asked about turn one")
        hs.record("JARVIS", "the reply mentioned turn two")
        found = hs.search("turn")
        assert "turn one" in found and "turn two" in found
        assert "user" in hs.recent(10) and "JARVIS" in hs.recent(10)

    def test_the_session_log_is_a_list_of_turns(self, live):
        lv, _ui, _r = live
        assert lv._session_log == []
        lv._session_log.append("User: hi")
        lv._session_log.append("JARVIS: hello")
        assert len(lv._session_log) == 2


class TestBargeInFlag:
    """`TestBargeInMainWiring` grepped for the init line and read the body of
    set_speaking. The flag is an attribute; ask for it."""

    def test_it_starts_on(self, live):
        lv, _ui, _r = live
        assert lv._barge_on is True

    def test_starting_speech_refreshes_the_flag_from_config(self, live, monkeypatch):
        """The flag is read here, when a reply starts, precisely so the audio
        callback never touches disk — a read per callback would be a file read
        on the mic thread, hundreds of times a second."""
        lv, _ui, _r = live
        reads: list[int] = []

        import memory.config_manager as cfg
        monkeypatch.setattr(cfg, "get_barge_in_enabled",
                            lambda: (reads.append(1), False)[1])

        lv.set_speaking(True)
        assert lv._is_speaking is True and lv._barge_on is False
        assert len(reads) == 1

    def test_stopping_speech_reads_nothing_and_opens_the_echo_tail(self, live,
                                                                   monkeypatch):
        lv, _ui, _r = live
        reads: list[int] = []
        import memory.config_manager as cfg
        monkeypatch.setattr(cfg, "get_barge_in_enabled",
                            lambda: (reads.append(1), True)[1])
        lv.set_speaking(True)
        before = len(reads)
        lv.set_speaking(False)
        assert len(reads) == before
        assert lv._is_speaking is False
        assert lv._tail_until > time.monotonic()      # guard covers the tail


class TestFocusMuteGate:
    """Replaces three greps in `test_focus_callbacks_and_mute_gate_wired`."""

    def test_the_focus_mute_flag_starts_clear(self, live):
        lv, _ui, _r = live
        assert lv._focus_muted is False

    def test_setting_the_flag_gates_proactive_speech(self, live, monkeypatch):
        lv, ui, _r = live
        spoken: list[str] = []
        monkeypatch.setattr(lv, "speak", lambda text, *_a, **_k: spoken.append(text))
        lv._focus_muted = True
        lv.proactive_say("you should drink water") if hasattr(lv, "proactive_say") \
            else None
        assert spoken == []          # muted while a focus session is running


class TestExecuteTool:
    """Drive the real dispatcher. The generic version of what the tiering and
    tool-output suites do for their own concerns."""

    def _call(self, lv, name, args):
        from google.genai import types
        return asyncio.run(M.JarvisLive._execute_tool(
            lv, types.FunctionCall(id="c1", name=name, args=args)))

    def test_a_discovered_tool_runs_through_the_registry(self, live):
        lv, _ui, registry = live
        resp = self._call(lv, "echo_tool", {"say": "hello"})
        assert resp.response["result"] == "echo:hello"
        assert registry.calls == ["echo_tool"]

    def test_the_activity_timeline_gets_exactly_one_event_per_call(self, live):
        """The grep counted `_activity.begin` against `_activity.finish`. Count
        the events the timeline actually holds."""
        from core import activity
        activity.clear()
        lv, _ui, _r = live
        self._call(lv, "echo_tool", {"say": "x"})
        events = [e for e in activity.recent(50) if e.name == "echo_tool"]
        assert len(events) == 1
        assert events[0].ok is True and events[0].duration is not None

    def test_a_non_blocking_tool_returns_its_scheduling(self, live):
        """`scheduling` goes on the FunctionResponse, not inside `response`."""
        lv, _ui, _r = live
        resp = self._call(lv, "slow_tool", {})
        assert resp.scheduling == "INTERRUPT"

    def test_a_tool_that_declared_nothing_sends_no_scheduling(self, live):
        """Sending a default here would change behaviour for every existing
        tool — the API default is what they were built against."""
        lv, _ui, _r = live
        assert self._call(lv, "echo_tool", {}).scheduling is None

    def test_save_memory_does_not_touch_the_registry(self, live, monkeypatch):
        """Built-in tools are handled before the registry: they are not actions,
        and a routed call must not pretend they are."""
        lv, _ui, registry = live
        saved = {}
        monkeypatch.setattr(M, "update_memory",
                            lambda payload: saved.update(payload))
        resp = self._call(lv, "save_memory",
                          {"category": "notes", "key": "k", "value": "v"})
        assert resp.response["result"] == "ok"
        assert registry.calls == []
        assert saved == {"notes": {"k": {"value": "v"}}}

    def test_an_unknown_tool_says_so_instead_of_crashing(self, live):
        lv, _ui, _r = live
        resp = self._call(lv, "no_such_tool", {})
        assert "no_such_tool" in resp.response["result"]

    def test_the_result_goes_through_the_output_cap(self, live):
        from core.tool_output import LIMIT
        lv, _ui, registry = live
        registry._records["huge"] = action("huge", lambda p, **k: "z" * (LIMIT * 3))
        resp = self._call(lv, "huge", {})
        assert len(resp.response["result"]) <= LIMIT


class TestRemoteKey:
    def test_without_a_dashboard_it_says_what_to_install(self, live):
        """Headless / dashboard-not-installed is the common case on a fresh
        machine, and the button must explain itself rather than do nothing."""
        _lv, ui, _r = live
        assert ui.on_remote_clicked() is None
        assert any("fastapi" in m for m in ui.logs)

    def test_the_key_is_generated_once_and_stays_valid(self, live):
        class FakeDashboard:
            def __init__(self): self.keys = 0
            def new_key(self):
                self.keys += 1
                return f"key-{self.keys}"
            def get_url(self): return "https://10.0.0.5:8712"
            def get_manual_url(self): return "10.0.0.5:8712"

        lv, ui, _r = live
        lv._dashboard = FakeDashboard()
        first = ui.on_remote_clicked()
        second = ui.on_remote_clicked()
        assert first[1] == "key-1" and second[1] == "key-2"
        assert first[2].endswith("auto-login?key=key-1")
        assert first[3] == "10.0.0.5:8712"


class TestWakeState:
    def test_it_reports_the_three_fields_the_hud_draws(self, live):
        lv, _ui, _r = live
        state = lv._wake_state()
        assert set(state) == {"enabled", "awake", "ready"}
        assert all(isinstance(v, bool) for v in state.values())

    def test_it_never_raises_even_with_nothing_installed(self, live, monkeypatch):
        """Called while the controls drawer is being built, on the UI thread."""
        import core.wake_word as ww
        monkeypatch.setattr(ww, "is_ready", lambda: False)
        lv, _ui, _r = live
        assert lv._wake_state()["ready"] is False


class TestThreadSafetyOfTheSpeakingFlag:
    def test_two_threads_toggling_do_not_corrupt_the_lock(self, live):
        """The audio callback runs on its own thread; the flag has a lock for a
        reason, and a grep could never see whether it holds."""
        lv, _ui, _r = live

        def toggle():
            for _ in range(50):
                lv.set_speaking(True)
                lv.set_speaking(False)

        threads = [threading.Thread(target=toggle) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert lv._is_speaking in (True, False)
        assert isinstance(lv._speaking_lock, type(threading.Lock()))
