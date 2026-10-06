"""
Tool tiering — the `toolbox` router and the core/deferred split.

Why this file exists: JARVIS declares its tools once, when the Live socket
opens, and the Live API has no way to add more later (AsyncSession exposes
`send_client_content`, `send_realtime_input` and `send_tool_response`, and
nothing that changes the declared set). So most tools are deliberately not
declared, and the model reaches them through one router. If the router breaks,
those ~62 tools stop existing as far as the model is concerned — silently.

The two things worth guarding are therefore not "does search return a list":

  1. **Deferred is not disabled.** A routed call must re-enter the real
     executor, so the autonomy gate, the confirm gate, the activity timeline
     and the dispatch audit all still see the real tool name. Tiering changes
     what the model is told it can do; it must not change what it is allowed
     to do. `TestRoutedCallsAreNotUnsandboxed` drives the REAL
     `JarvisLive._execute_tool` to prove that.
  2. **The router is always reachable.** If `toolbox` is ever dropped from the
     declaration list, or a deferred name collides with a core one, the model
     loses the ability to find things with no error anywhere.

These tests drive the REAL `JarvisLive._execute_tool` rather than grepping the
source for it, and that used to require a stand-in `ui` module in sys.modules
because `main.py` imported PyQt6 (and PortAudio) at module load. Neither is
imported at module load any more, so `import main` works on a bare runner — see
`_StandInUI` below for what replaced it, and why the fake went.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _force_repo_root_first() -> None:
    """Put the app root at sys.path[0], not merely somewhere on sys.path.

    Importing OpenCV appends `.../site-packages/cv2` to sys.path, and that
    directory contains `config.py` — so the repo's own `config/` package is one
    sys.path entry away from being shadowed by a module that does not define
    `get_base_dir`. `from config import get_base_dir` then raises
    `NameError: LOADER_DIR` from inside the library. `main.py` pins this for the
    real launch (see the guard there); a test that imports `main` has to do the
    same, or it fails for a reason unrelated to what it is testing.
    """
    root = str(ROOT)
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    # Drop a `config` that already resolved to the wrong module.
    cached = sys.modules.get("config")
    if cached is not None:
        f = getattr(cached, "__file__", "") or ""
        if not f.startswith(str(ROOT / "config")):
            del sys.modules["config"]


_force_repo_root_first()


# ────────────────────────────────────────────────────────────────────────────
# harness
# ────────────────────────────────────────────────────────────────────────────

class _StandInUI:
    """The few attributes `JarvisLive._execute_tool` touches on the window.

    This used to be a fake `ui` module installed into sys.modules, because
    `import main` needed one: main.py imported Qt at module level. Both Qt and
    sounddevice are imported where they are used now, so `import main` works on
    a bare CI runner and the fakes are gone. A fake `ui` left in sys.modules is
    not harmless — it shadows the real ui package for every later test in the
    session, and a test that imports the real one passes for the wrong reason.
    """

    muted = False
    current_file = None

    def __init__(self, *a, **k):
        pass

    def set_state(self, *_a): pass
    def write_log(self, *_a): pass
    def show_content(self, *_a, **_k): pass
    def set_audio_level(self, *_a): pass


@pytest.fixture(scope="module")
def jarvis():
    """A JarvisLive with real registries and no live socket.

    `__init__` opens audio, Qt and Gemini; the tests want the dispatch path, so
    the object is built without it and the few attributes it touches are set.
    """
    import main as M

    from core.action_loader import discover_actions
    from core.plugin_loader import discover_plugins
    from core import tool_tiers as tt

    async def _call(name, args):
        from google.genai import types
        return await M.JarvisLive._execute_tool(
            lv, types.FunctionCall(id="call-1", name=name, args=args))

    lv = object.__new__(M.JarvisLive)                 # no socket, no audio
    lv.ui = _StandInUI()
    lv._action_registry = discover_actions(
        ROOT / "actions",
        reserved_names={t["name"] for t in M.TOOL_DECLARATIONS},
        logger=lambda _m: None,
    )
    lv._plugin_registry = discover_plugins(
        ROOT / "plugins", core_tool_names=set(), logger=lambda _m: None)
    _core, lv._deferred_decls = tt.split_declarations(
        lv._action_registry.get_tool_declarations())
    lv.call = _call
    return lv


def _run(lv, name, args):
    return asyncio.run(lv.call(name, args))


def _text(resp) -> str:
    return str((resp.response or {}).get("result", ""))


# ────────────────────────────────────────────────────────────────────────────
# the split itself
# ────────────────────────────────────────────────────────────────────────────

class TestTierSplit:
    def _decls(self):
        from core.action_loader import discover_actions
        from core import tool_tiers as tt
        reg = discover_actions(ROOT / "actions")
        return tt.split_declarations(reg.get_tool_declarations())

    def test_every_tool_lands_in_exactly_one_tier(self):
        core, deferred = self._decls()
        core_names = [d["name"] for d in core]
        deferred_names = [d["name"] for d in deferred]
        assert not set(core_names) & set(deferred_names), "a tool is in both tiers"
        assert len(core_names) + len(deferred_names) == len(core_names + deferred_names)

    def test_core_set_is_much_smaller_than_the_whole(self):
        """The point of the change: if this ever inverts, tiering is buying
        nothing and should be revisited rather than left in."""
        core, deferred = self._decls()
        assert len(deferred) > len(core), (
            "more tools are declared up front than deferred — tiering "
            "is not reducing the payload")
        assert len(core) <= 25, f"core tier has grown to {len(core)} tools"

    def test_core_contains_the_conversational_basics(self):
        """The tools a user reaches for constantly must never need a router
        hop first — the extra round trip is the cost this change pays, and
        these are the calls where a user would feel it."""
        from core.tool_tiers import CORE_TOOL_NAMES
        for name in ("open_app", "web_search", "weather_report", "reminder",
                     "file_processor", "send_message", "computer_settings",
                     "save_memory", "recall_memory", "undo", "screen_process"):
            assert name in CORE_TOOL_NAMES, f"{name} should not be deferred"

    def test_workflow_entry_points_are_never_deferred(self):
        """The tier line: entry points and safety levers are core, leaf
        utilities are deferred. Deferring a LEAF costs one round trip on a
        job that was going to be one call anyway. Deferring an ENTRY POINT
        costs a round trip in the middle of a spoken sentence, on the way
        into a ten-step job — "roz subah 8 baje research karo" needs `rules`
        to schedule it and `dots` to pick the researcher before any work
        starts. If someone re-tunes the tiers, this is the line they have to
        argue with."""
        from core.tool_tiers import CORE_TOOL_NAMES
        for name in ("rules", "dots", "pages", "obsidian", "task_agent"):
            assert name in CORE_TOOL_NAMES, (
                f"{name} starts a workflow — a router hop in front of it "
                f"lands mid-sentence")

    def test_the_users_own_levers_are_never_deferred(self):
        """`privacy` and `autonomy` are not tasks, they are how someone takes
        control back from the assistant. A router hop in front of a safety
        switch means the one time it matters is the one time the model has to
        go looking for it — so they stay core however the tiers are tuned."""
        from core.tool_tiers import CORE_TOOL_NAMES
        for name in ("privacy", "autonomy"):
            assert name in CORE_TOOL_NAMES, (
                f"{name} is a user-facing control and must not be deferred")

    def test_declaration_payload_shrinks(self):
        import json
        from core import tool_tiers as tt
        core, deferred = self._decls()
        before = len(json.dumps(core + deferred))
        after = len(json.dumps(core + [tt.router_declaration()]))
        # A floor, not a target. The exact saving moves whenever the core tier
        # is deliberately re-tuned (it went 67% -> 61% when the workflow entry
        # points were promoted), so pinning a number here would fight the
        # design. What must never happen is tiering quietly becoming a no-op.
        assert after < before * 0.5, (
            f"tiering only saved {100 - after * 100 // before}% "
            f"({before:,} -> {after:,} chars)")

    def test_router_declaration_is_well_formed(self):
        from core import tool_tiers as tt
        d = tt.router_declaration()
        assert d["name"] == tt.ROUTER_NAME
        assert d["parameters"]["type"] == "OBJECT"
        assert set(d["parameters"]["required"]) == {"action"}
        assert "action" in d["parameters"]["properties"]
        assert tt.ROUTER_NAME not in tt.CORE_TOOL_NAMES


# ────────────────────────────────────────────────────────────────────────────
# search
# ────────────────────────────────────────────────────────────────────────────

class TestSearch:
    def _deferred(self, jarvis):
        return jarvis._deferred_decls

    def test_independent_of_a_live_session(self):
        """Search is pure — it must not need a session to be testable."""
        from core import tool_tiers as tt
        from core.action_loader import discover_actions
        _c, deferred = tt.split_declarations(
            discover_actions(ROOT / "actions").get_tool_declarations())
        assert tt.search(deferred, "3d model")[0]["name"] == "make_3d"

    def test_name_query_reaches_that_tool(self, jarvis):
        """Every tool the router hides, the router can find again.

        Derived from the live deferred list rather than a hand-written table —
        a hardcoded name silently stops testing anything the moment that tool
        is promoted to core (which is exactly what happened to `obsidian`).
        """
        from core import tool_tiers as tt
        deferred = self._deferred(jarvis)
        biggest = sorted(deferred, key=lambda d: len(json.dumps(d)),
                         reverse=True)[:8]
        assert biggest, "nothing is deferred — tiering is off"
        for decl in biggest:
            name = decl["name"]
            hits = [d["name"] for d in tt.search(deferred, name)]
            assert name in hits[:2], f"{name!r} did not surface itself"
        # a couple of known-specific queries still resolve by name
        for query, expected in (("make_3d", "make_3d"),
                                ("manim", "manim_anim"),
                                ("smart_home", "smart_home")):
            hits = [d["name"] for d in tt.search(deferred, query)]
            assert expected in hits[:2], f"{query!r} did not surface {expected}"

    def test_returns_the_full_schema_not_a_summary(self, jarvis):
        """A summary produces malformed calls — the model needs the real
        parameter names and types."""
        from core import tool_tiers as tt
        text = tt.format_schemas(tt.search(self._deferred(jarvis), "make_3d"))
        assert "### make_3d" in text
        assert "parameters:" in text
        assert '"spec"' in text or '"properties"' in text

    def test_empty_query_still_returns_something(self, jarvis):
        from core import tool_tiers as tt
        assert tt.search(self._deferred(jarvis), "")

    def test_nonsense_query_returns_something_to_correct_from(self, jarvis):
        from core import tool_tiers as tt
        assert tt.search(self._deferred(jarvis), "zzzz qqqq")

    def test_prompt_hint_lists_every_deferred_name(self, jarvis):
        """Names are cheap and the model cannot search for a capability it
        does not know exists."""
        from core import tool_tiers as tt
        hint = tt.hint_for_prompt(self._deferred(jarvis))
        for name in ("make_3d", "manim_anim", "smart_home", "telegram_send"):
            assert name in hint
        assert len(hint) < 2000, "the hint is supposed to be the cheap half"

    def test_hint_is_empty_when_nothing_is_deferred(self):
        from core import tool_tiers as tt
        assert tt.hint_for_prompt([]) == ""


# ────────────────────────────────────────────────────────────────────────────
# plan_run — the validation that decides whether anything executes
# ────────────────────────────────────────────────────────────────────────────

class TestPlanRun:
    def test_accepts_a_json_object_string(self):
        from core import tool_tiers as tt
        target, params, err = tt.plan_run(
            {"tool": "make_3d", "parameters_json": '{"spec": "box 1 2 3"}'}, [])
        assert (target, params, err) == ("make_3d", {"spec": "box 1 2 3"}, "")

    def test_accepts_an_already_parsed_object(self):
        from core import tool_tiers as tt
        _t, params, err = tt.plan_run(
            {"tool": "make_3d", "parameters_json": {"spec": "box"}}, [])
        assert err == "" and params == {"spec": "box"}

    def test_missing_params_is_an_empty_object(self):
        from core import tool_tiers as tt
        _t, params, err = tt.plan_run({"tool": "make_3d"}, [])
        assert err == "" and params == {}

    def test_missing_tool_name_is_refused_with_instructions(self):
        from core import tool_tiers as tt
        _t, _p, err = tt.plan_run({"action": "run"}, [])
        assert err and "search" in err.lower()

    def test_self_recursion_is_refused(self):
        from core import tool_tiers as tt
        _t, _p, err = tt.plan_run({"tool": tt.ROUTER_NAME}, [])
        assert err and "itself" in err

    def test_bad_json_hands_back_the_schema(self, jarvis):
        """The usual cause is a guessed parameter name, and 'invalid JSON'
        alone does not let the model fix that."""
        from core import tool_tiers as tt
        _t, _p, err = tt.plan_run(
            {"tool": "make_3d", "parameters_json": "not json"},
            jarvis._deferred_decls)
        assert err and "### make_3d" in err

    def test_a_json_list_is_refused(self):
        from core import tool_tiers as tt
        _t, _p, err = tt.plan_run({"tool": "make_3d", "parameters_json": "[1]"},
                                  [])
        assert err and "OBJECT" in err


# ────────────────────────────────────────────────────────────────────────────
# the router, through the REAL executor
# ────────────────────────────────────────────────────────────────────────────

class TestRouterThroughRealExecutor:
    def test_search_returns_schemas_without_running_anything(self, jarvis):
        resp = _run(jarvis, "toolbox", {"action": "search", "query": "diagram"})
        text = _text(resp)
        assert "###" in text and "parameters:" in text
        assert resp.name == "toolbox"

    def test_search_opens_no_activity_event(self, jarvis):
        """A search is not work done on the user's behalf; it should not
        appear on the Mission Control timeline."""
        from core import activity
        seen: list[str] = []
        original = activity.begin
        activity.begin = lambda kind, name, *a, **k: (
            seen.append(name), original(kind, name, *a, **k))[1]
        try:
            _run(jarvis, "toolbox", {"action": "search", "query": "diagram"})
        finally:
            activity.begin = original
        assert seen == []

    def test_run_executes_the_real_deferred_tool(self, jarvis):
        resp = _run(jarvis, "toolbox", {
            "action": "run", "tool": "make_3d",
            "parameters_json": '{"spec": "box 2 3 4"}',
        })
        text = _text(resp)
        # The handler actually ran — a routed call is not a no-op ack.
        assert "Model saved" in text or "spec" in text.lower()
        assert resp.name == "toolbox", "the envelope must stay the router"

    def test_unknown_action_is_refused(self, jarvis):
        resp = _run(jarvis, "toolbox", {"action": "explode"})
        assert "Unknown action" in _text(resp)

    def test_missing_tool_name_is_refused(self, jarvis):
        resp = _run(jarvis, "toolbox", {"action": "run"})
        assert "tool" in _text(resp).lower()

    def test_self_recursion_is_refused_end_to_end(self, jarvis):
        from core import tool_tiers as tt
        resp = _run(jarvis, "toolbox",
                    {"action": "run", "tool": tt.ROUTER_NAME})
        assert "itself" in _text(resp)

    def test_unknown_tool_name_suggests_a_real_one(self, jarvis):
        resp = _run(jarvis, "toolbox", {
            "action": "run", "tool": "telegrm", "parameters_json": "{}"})
        text = _text(resp)
        assert "Unknown tool" in text
        assert "telegram" in text, f"no useful suggestion offered: {text}"

    def test_bad_json_is_reported_with_the_schema(self, jarvis):
        resp = _run(jarvis, "toolbox", {
            "action": "run", "tool": "make_3d", "parameters_json": "{"})
        assert "not valid JSON" in _text(resp)


# ────────────────────────────────────────────────────────────────────────────
# the safety property: routing is not a way around the gates
# ────────────────────────────────────────────────────────────────────────────

class TestRoutedCallsAreNotUnsandboxed:
    """The whole change rests on this: reaching a tool through the router must
    be indistinguishable from calling it directly, as far as the gates are
    concerned. Otherwise tiering would be a sandbox bypass wearing a
    performance hat."""

    def _record(self, monkeypatch):
        from core import autonomy, activity
        gate_calls: list[str] = []
        begin_calls: list[str] = []
        real_gate, real_begin = autonomy.gate, activity.begin

        def spy_gate(tool, args=None, *a, **k):
            gate_calls.append(tool)
            return real_gate(tool, args, *a, **k)

        def spy_begin(kind, name, *a, **k):
            begin_calls.append(name)
            return real_begin(kind, name, *a, **k)

        monkeypatch.setattr(autonomy, "gate", spy_gate)
        monkeypatch.setattr(activity, "begin", spy_begin)
        return gate_calls, begin_calls

    def test_autonomy_gate_sees_the_routed_tool(self, jarvis, monkeypatch):
        gate_calls, begin_calls = self._record(monkeypatch)
        _run(jarvis, "toolbox", {
            "action": "run", "tool": "make_3d",
            "parameters_json": '{"spec": "box 1 2 3"}'})
        assert gate_calls == ["make_3d"], (
            f"the autonomy gate saw {gate_calls}, not the real tool")
        assert begin_calls == ["make_3d"], (
            f"the timeline saw {begin_calls}, not the real tool")

    def test_a_direct_call_hits_the_same_hooks(self, jarvis, monkeypatch):
        """Control for the test above — the two paths must agree."""
        gate_calls, begin_calls = self._record(monkeypatch)
        _run(jarvis, "make_3d", {"spec": "box 1 2 3"})
        assert gate_calls == ["make_3d"]
        assert begin_calls == ["make_3d"]

    def test_observe_mode_blocks_a_routed_mutating_call(self, jarvis, monkeypatch):
        """End-to-end: with autonomy in read-only mode a routed mutating tool
        must be refused, not quietly executed."""
        from core import autonomy
        monkeypatch.setattr(autonomy, "gate", lambda tool, args=None, *a, **k: (
            "Autonomy mode is OBSERVE (read-only) — refused."
            if autonomy.mutating(tool, args) else None))

        resp = _run(jarvis, "toolbox", {
            "action": "run", "tool": "file_controller",
            "parameters_json": '{"action": "delete", "path": "/tmp/x.txt"}'})
        text = _text(resp)
        if autonomy.mutating("file_controller", {"action": "delete"}):
            assert "OBSERVE" in text, f"routed call bypassed the gate: {text}"


# ────────────────────────────────────────────────────────────────────────────
# the declaration the session actually builds
# ────────────────────────────────────────────────────────────────────────────

class TestBuildConfigWiring:
    def test_tiering_can_be_switched_off(self, monkeypatch):
        import memory.config_manager as cm
        monkeypatch.setattr(cm, "load_api_keys", lambda: {"tool_tiering": False})
        assert cm.get_tool_tiering_enabled() is False

    def test_tiering_defaults_on(self, monkeypatch):
        import memory.config_manager as cm
        monkeypatch.setattr(cm, "load_api_keys", lambda: {})
        assert cm.get_tool_tiering_enabled() is True

    def test_main_declares_the_router_when_deferring(self):
        """If the router is not in the declaration list the model cannot call
        it, and every deferred tool becomes invisible with no error raised."""
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        assert "_tool_tiers.router_declaration()" in src
        assert "_tool_tiers.ROUTER_NAME" in src

    def test_toolbox_is_not_batched_as_read_only(self):
        """It can run anything, including something that mutates — so it must
        never be overlapped with other calls."""
        import main as M
        assert "toolbox" not in M.JarvisLive._READ_ONLY_TOOLS
