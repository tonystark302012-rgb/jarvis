"""core/plugin_loader.py — the drop-in plugin engine.

This is the headline feature: one .py file in plugins/ and the assistant has a
new ability. It is also the place where a stranger's file is imported into a
running process, so the contract worth testing is not "does a good plugin work"
(that is the easy half) but what happens to a bad one:

  * a plugin that raises at import time must not stop the other plugins loading,
  * a plugin that crashes mid-call must not take the session down,
  * a plugin whose helper file is missing must say which file to download,
    in words a non-programmer can act on,
  * a plugin that would shadow a core tool must be refused, not merged.

The suite covers each of those against a real directory of real .py files on
disk, because that is the path the app takes.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core import plugin_loader
from core.plugin_loader import PluginRecord, PluginRegistry, discover_plugins, _call_run

CORE_TOOLS = {"open_app", "web_search", "system_monitor"}


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    """The loader asks memory.config_manager whether a plugin is enabled and for
    its stored settings — that reads api_keys.json. Tests never touch a real
    config file: the two lookups are stubbed at the module seam."""
    enabled: dict[str, bool] = {}
    values: dict[str, dict] = {}
    monkeypatch.setattr(plugin_loader, "get_plugin_enabled",
                        lambda name: enabled.get(name, True))
    monkeypatch.setattr(plugin_loader, "get_plugin_config",
                        lambda ns: dict(values.get(ns, {})))
    return {"enabled": enabled, "values": values}


def write(plugins_dir: Path, name: str, *parts: str) -> Path:
    """Write a plugin file. Each part is dedented on its own so a test can
    append a second block (a settings schema, an extra import) to a template
    without inheriting its indentation."""
    p = plugins_dir / name
    body = "\n".join(textwrap.dedent(part).strip("\n") for part in parts)
    p.write_text(body + "\n", encoding="utf-8")
    return p


VALID = '''
    PLUGIN = {
        "name": "printer_status",
        "description": "Reports whether the printer is reachable.",
        "parameters": {"type": "OBJECT", "properties": {"host": {"type": "STRING"}}},
    }

    def run(parameters, **kwargs):
        return f"printer at {parameters.get('host')} is fine"
'''


def discover(tmp_path, core=CORE_TOOLS, logger=None, notify=None):
    return discover_plugins(tmp_path / "plugins", set(core),
                            logger=logger or (lambda _m: None),
                            notify=notify)


class TestDiscovery:
    def test_a_good_plugin_loads(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "printer.py", VALID)
        reg = discover(tmp_path)
        assert reg.has("printer_status")
        assert reg.get_tool_declarations()[0]["description"].startswith("Reports")
        assert reg.run("printer_status", {"host": "10.0.0.5"}) == \
            "printer at 10.0.0.5 is fine"

    def test_it_creates_the_directory_when_missing(self, tmp_path):
        reg = discover(tmp_path)                       # no plugins/ at all
        assert (tmp_path / "plugins").is_dir()
        assert reg.get_tool_declarations() == []

    def test_underscore_files_are_never_plugins(self, tmp_path):
        """`_template.py` ships as a starting point and a shared helper is
        prefixed with `_` — importing either as a plugin would either register
        a template as a tool or run a helper's import side effects twice."""
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "_template.py", VALID)
        write(d, "_helper.py", "VALUE = 1")
        reg = discover(tmp_path)
        assert not reg.has("printer_status")
        assert reg._all_records == []

    def test_non_python_files_are_ignored(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "printer.py", VALID)
        (d / "notes.txt").write_text("not a plugin", encoding="utf-8")
        (d / "data.json").write_text("{}", encoding="utf-8")
        assert discover(tmp_path).has("printer_status")

    def test_several_plugins_all_load(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        for n, tool in (("a.py", "alpha"), ("b.py", "beta"), ("c.py", "gamma")):
            write(d, n, VALID.replace("printer_status", tool)
                           .replace("printer", tool))
        reg = discover(tmp_path)
        assert sorted(r["name"] for r in reg.get_tool_declarations()) == \
            ["alpha", "beta", "gamma"]

    def test_order_is_deterministic(self, tmp_path):
        """Two runs of the same folder must declare tools in the same order, or
        the tool list the model sees churns between sessions for no reason."""
        d = tmp_path / "plugins"
        d.mkdir()
        for n in ("z.py", "a.py", "m.py"):
            write(d, n, VALID.replace("printer_status", n[0] * 3))
        first = [r["name"] for r in discover(tmp_path).get_tool_declarations()]
        second = [r["name"] for r in discover(tmp_path).get_tool_declarations()]
        assert first == second == ["aaa", "mmm", "zzz"]


class TestBadPluginsAreRejectedNotFatal:
    """Every one of these is a file a user could plausibly drop in."""

    @pytest.mark.parametrize("body,expected", [
        ("X = 1", "Missing PLUGIN dict"),
        ('''PLUGIN = {"description": "d", "parameters": {"type": "OBJECT"}}\n'''
         "def run(p): return 'x'", "name"),
        ('''PLUGIN = {"name": "has space", "description": "d",\n'''
         "          \"parameters\": {\"type\": \"OBJECT\"}}\ndef run(p): return 'x'",
         "valid identifier"),
        ('''PLUGIN = {"name": "9lives", "description": "d",\n'''
         "          \"parameters\": {\"type\": \"OBJECT\"}}\ndef run(p): return 'x'",
         "valid identifier"),
        ('''PLUGIN = {"name": "x", "description": "",\n'''
         "          \"parameters\": {\"type\": \"OBJECT\"}}\ndef run(p): return 'x'",
         "description"),
        ('''PLUGIN = {"name": "x", "description": "d", "parameters": {}}\n'''
         "def run(p): return 'x'", "OBJECT"),
        ('''PLUGIN = {"name": "x", "description": "d",\n'''
         "          \"parameters\": {\"type\": \"OBJECT\"}}\nrun = 5", "run(parameters"),
    ])
    def test_it_is_rejected_with_a_reason(self, tmp_path, body, expected):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "bad.py", body)
        reg = discover(tmp_path)
        assert reg.get_tool_declarations() == []
        (rec,) = reg.list_for_ui()
        assert rec["valid"] is False
        assert expected.lower() in rec["error"].lower()

    def test_a_plugin_that_crashes_on_import_does_not_stop_the_scan(self, tmp_path):
        """Crash isolation is the whole reason this loader exists."""
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "broken.py", "raise RuntimeError('boom at import')")
        write(d, "good.py", VALID)
        logs = []
        reg = discover(tmp_path, logger=logs.append)
        assert reg.has("printer_status")            # the good one still loaded
        bad = [r for r in reg.list_for_ui() if r["file"] == "broken.py"][0]
        assert bad["valid"] is False and "boom at import" in bad["error"]
        assert any("Plugin rejected: broken.py" in m for m in logs)

    def test_a_crash_does_not_leave_a_half_imported_module_behind(self, tmp_path):
        """The broken module is removed from sys.modules, so a second scan of
        the same directory retries it instead of reusing the corpse."""
        import sys
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "broken.py", "raise RuntimeError('boom')")
        discover(tmp_path)
        assert "plugins.broken" not in sys.modules

    def test_a_name_colliding_with_a_core_tool_is_refused(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "shadow.py", VALID.replace("printer_status", "open_app"))
        reg = discover(tmp_path)
        assert not reg.has("open_app")
        (rec,) = reg.list_for_ui()
        assert "collides with a core tool" in rec["error"]

    def test_two_plugins_with_the_same_name_first_wins(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "a_first.py", VALID)
        write(d, "b_second.py", VALID)
        reg = discover(tmp_path)
        assert len(reg.get_tool_declarations()) == 1
        second = [r for r in reg.list_for_ui() if r["file"] == "b_second.py"][0]
        assert "already used by plugin 'a_first.py'" in second["error"]

    def test_a_rejection_is_reported_once_to_the_user(self, tmp_path):
        """The activity log is the conversation, not a boot transcript: one
        line, not one per file."""
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "bad1.py", "X = 1")
        write(d, "bad2.py", "Y = 2")
        notices = []
        discover(tmp_path, notify=notices.append)
        assert len(notices) == 1 and "2 plugin(s)" in notices[0]

    def test_a_clean_load_says_nothing_to_the_user(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "good.py", VALID)
        notices = []
        discover(tmp_path, notify=notices.append)
        assert notices == []


class TestActionableErrorMessages:
    """A download that is missing a file is the most common support question."""

    def test_a_missing_shared_helper_names_the_file(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "printer.py", '''
            import plugins._printer_core as core
            PLUGIN = {"name": "printer", "description": "d",
                      "parameters": {"type": "OBJECT"}}
            def run(p): return "x"
        ''')
        (rec,) = discover(tmp_path).list_for_ui()
        assert "_printer_core.py" in rec["error"]
        assert "download" in rec["error"]

    def test_a_missing_third_party_package_says_pip_install(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "needs.py", '''
            import totally_missing_package_xyz   # noqa: F401
            PLUGIN = {"name": "needs", "description": "d",
                      "parameters": {"type": "OBJECT"}}
            def run(p): return "x"
        ''')
        (rec,) = discover(tmp_path).list_for_ui()
        assert "pip install totally_missing_package_xyz" in rec["error"]

    def test_any_other_failure_is_still_readable(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "syntax.py", "def run(\n")          # SyntaxError
        (rec,) = discover(tmp_path).list_for_ui()
        assert rec["error"].startswith("Failed to load:")


class TestRunIsolated:
    def test_a_crashing_plugin_returns_a_sentence_not_an_exception(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "boom.py", '''
            PLUGIN = {"name": "boom", "description": "d",
                      "parameters": {"type": "OBJECT"}}
            def run(p): raise ValueError("the printer is on fire")
        ''')
        logs, notices = [], []
        reg = discover(tmp_path, logger=logs.append, notify=notices.append)
        out = reg.run("boom", {})
        assert isinstance(out, str)
        assert "the printer is on fire" in out and out.startswith("Sir,")
        assert any("crashed during run()" in m for m in logs)
        assert notices and "failed" in notices[0]

    def test_an_unknown_plugin_says_so(self, tmp_path):
        reg = discover(tmp_path)
        assert reg.run("ghost", {}) == "Plugin 'ghost' is not available."

    def test_a_disabled_plugin_will_not_run(self, tmp_path, _isolated_config):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "printer.py", VALID)
        reg = discover(tmp_path)
        _isolated_config["enabled"]["printer_status"] = False
        assert reg.run("printer_status", {}) == \
            "The 'printer_status' plugin is currently disabled."
        assert reg.get_tool_declarations() == []      # and it is not declared

    def test_a_plugin_returning_nothing_is_not_a_crash(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "quiet.py", '''
            PLUGIN = {"name": "quiet", "description": "d",
                      "parameters": {"type": "OBJECT"}}
            def run(p): pass
        ''')
        assert discover(tmp_path).run("quiet", {}) == "Done."


class TestCallRunAdaptsToTheSignature:
    """A plugin author writes `def run(parameters)` and nothing else. That must
    work — requiring the full signature would be a tax on every plugin."""

    def test_parameters_only(self):
        seen = {}
        assert _call_run(lambda parameters: seen.update(p=parameters) or "ok",
                         {"a": 1}, player="P", session_memory="S") == "ok"
        assert seen["p"] == {"a": 1}

    def test_optional_player_is_passed_when_declared(self):
        def run(parameters, player=None):
            return f"player={player}"
        assert _call_run(run, {}, player="WINDOW", session_memory=None) == \
            "player=WINDOW"

    def test_var_keyword_gets_everything(self):
        def run(parameters, **kw):
            return sorted(kw)
        assert _call_run(run, {}, player=1, session_memory=2) == \
            ["player", "session_memory"]

    def test_it_does_not_send_kwargs_the_plugin_cannot_take(self):
        """Sending player= to a two-argument plugin is a TypeError at call time
        — the plugin would look broken to its author."""
        def run(parameters):
            return "fine"
        assert _call_run(run, {}, player=object(), session_memory=object()) == "fine"


class TestOptionalMetadata:
    def test_behavior_and_scheduling_are_normalised(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "slow.py", VALID.replace(
            '"parameters"', '"behavior": "non_blocking", '
                            '"scheduling": "interrupt", "parameters"'))
        reg = discover(tmp_path)
        decl = reg.get_tool_declarations()[0]
        assert decl["behavior"] == "NON_BLOCKING"
        assert reg.scheduling("printer_status") == "INTERRUPT"

    def test_an_unknown_behavior_is_ignored_not_guessed(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "odd.py", VALID.replace(
            '"parameters"', '"behavior": "sideways", "parameters"'))
        decl = discover(tmp_path).get_tool_declarations()[0]
        assert "behavior" not in decl          # the API's default applies
        assert discover(tmp_path).scheduling("printer_status") is None

    def test_a_malformed_settings_schema_does_not_reject_the_plugin(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "cfg.py", VALID,
              "PLUGIN_SETTINGS = {'fields': 'not a list'}")
        reg = discover(tmp_path)
        assert reg.has("printer_status")
        assert reg.settings_schemas() == []


class TestSettingsSchemas:
    def test_one_form_per_namespace_with_values_merged(self, tmp_path, _isolated_config):
        """A suite of plugins sharing a namespace shows ONE form, pre-filled."""
        d = tmp_path / "plugins"
        d.mkdir()
        schema = '''
            PLUGIN_SETTINGS = {"namespace": "printer", "title": "Printer",
                               "fields": [{"key": "host", "label": "Host"}]}
        '''
        write(d, "p1.py", VALID.replace("printer_status", "print_a"), schema)
        write(d, "p2.py", VALID.replace("printer_status", "print_b"), schema)
        _isolated_config["values"]["printer"] = {"host": "10.0.0.9"}
        schemas = discover(tmp_path).settings_schemas()
        assert len(schemas) == 1
        assert schemas[0]["namespace"] == "printer"
        assert schemas[0]["title"] == "Printer"
        assert schemas[0]["values"] == {"host": "10.0.0.9"}

    def test_a_plugin_without_a_schema_shows_no_form(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "plain.py", VALID)
        assert discover(tmp_path).settings_schemas() == []

    def test_a_disabled_plugin_shows_no_form(self, tmp_path, _isolated_config):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "cfg.py", VALID,
              'PLUGIN_SETTINGS = {"fields": [{"key": "host"}]}')
        _isolated_config["enabled"]["printer_status"] = False
        assert discover(tmp_path).settings_schemas() == []


class TestUiListing:
    def test_it_lists_valid_and_invalid_alike(self, tmp_path):
        """The Plugin Manager has to show the broken file — that is where the
        user finds out why their plugin does nothing."""
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "good.py", VALID)
        write(d, "bad.py", "X = 1")
        rows = {r["file"]: r for r in discover(tmp_path).list_for_ui()}
        assert rows["good.py"]["valid"] is True and rows["good.py"]["enabled"] is True
        assert rows["bad.py"]["valid"] is False
        assert rows["bad.py"]["enabled"] is False     # never claims otherwise
        assert rows["bad.py"]["error"]

    def test_every_row_carries_what_the_manager_renders(self, tmp_path):
        d = tmp_path / "plugins"
        d.mkdir()
        write(d, "good.py", VALID)
        (row,) = discover(tmp_path).list_for_ui()
        assert set(row) == {"name", "description", "file", "valid", "error", "enabled"}


class TestRegistryShape:
    def test_only_valid_records_are_registered(self):
        good = PluginRecord(name="a", description="d", run=lambda p: "x", valid=True)
        bad = PluginRecord(name="b", file="b.py", valid=False, error="nope")
        reg = PluginRegistry({"a": good}, lambda _m: None)
        reg._all_records = [good, bad]
        assert reg.has("a") and not reg.has("b")
        assert len(reg.list_for_ui()) == 2
        assert len(reg.get_tool_declarations()) == 1

    def test_the_default_notify_is_a_no_op(self):
        """Every existing single-sink caller keeps working unchanged."""
        reg = PluginRegistry({}, lambda _m: None)
        reg.run("nope", {})                       # must not raise
        assert reg.scheduling("nope") is None
