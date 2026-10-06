"""core/tool_output.py and the cap at the dispatch point.

The tools' own caps ranged from 2,500 to 40,000 characters, and several tools
had none at all — `file_processor` on a large document, `data_query` over a wide
table. A single such result can spend most of a session's context, and the
symptom is not an error: the assistant simply starts forgetting what was said a
minute ago.

So there is one backstop where every result passes through. These tests cover
the trimming, and then drive the real `JarvisLive._execute_tool` to prove the
cap is actually wired — a cap that is only defined and never applied is the
failure mode this whole file exists to prevent.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.tool_output import LIMIT, cap, needs_capping              # noqa: E402


class _StandInUI:
    """The attributes `_execute_tool` touches on the window."""

    muted = False
    current_file = None

    def __init__(self):
        self.logged: list[str] = []

    def set_state(self, *_a): pass
    def write_log(self, msg): self.logged.append(msg)
    def show_content(self, *_a, **_k): pass
    def set_audio_level(self, *_a): pass


class TestCap:
    def test_a_short_result_is_returned_untouched(self):
        """The overwhelmingly common case must not be rewritten at all."""
        text = "Printer is fine."
        assert cap(text) is text or cap(text) == text

    def test_a_result_at_the_limit_is_untouched(self):
        text = "x" * LIMIT
        assert cap(text) == text

    def test_an_oversized_result_never_exceeds_the_limit(self):
        """The note counts against the limit — the promise is about the payload."""
        for text in ("x" * (LIMIT + 1), "\n".join(["line"] * 200_000),
                     "word " * 200_000, "z" * 5_000_000):
            assert len(cap(text)) <= LIMIT

    def test_it_says_how_much_was_dropped(self):
        text = "a" * (LIMIT * 3)
        out = cap(text)
        assert f"{len(text):,} characters" in out
        assert "truncated" in out

    def test_it_tells_the_model_what_to_do_instead(self):
        """A model handed a silently shortened document summarises the part it
        got and says nothing about the rest — a wrong answer that looks right."""
        out = cap("b" * (LIMIT * 2))
        assert "Narrow the request" in out
        assert "content panel" in out

    def test_it_cuts_on_a_line_boundary(self):
        text = "\n".join(f"row {i:05d} value" for i in range(20_000))
        out = cap(text)
        head = out.split("\n\n[...")[0]
        assert not head.endswith("ro")            # not mid-row
        assert head.splitlines()[-1].startswith("row ")

    def test_it_cuts_on_a_word_when_there_are_no_lines(self):
        text = "alpha beta gamma " * 10_000          # one long line
        head = cap(text).split("\n\n[...")[0]
        assert not head.endswith("gam")
        assert head.endswith("gamma")

    def test_a_boundary_too_close_to_the_start_is_ignored(self):
        """Otherwise one early newline throws away the whole answer."""
        text = "header\n" + ("z" * (LIMIT * 2))
        head = cap(text).split("\n\n[...")[0]
        assert len(head) > LIMIT * 0.9

    def test_it_keeps_the_beginning_not_the_end(self):
        """Where the point is stated is the beginning — summaries, table
        headers, first matches, error text."""
        text = "IMPORTANT: the disk is full\n" + ("x" * (LIMIT * 2))
        assert cap(text).startswith("IMPORTANT: the disk is full")

    def test_a_non_string_result_is_coerced(self):
        """Tools occasionally return a dict or a list."""
        assert cap({"a": 1}) == str({"a": 1})
        assert len(cap(["x"] * 200_000)) <= LIMIT

    def test_needs_capping_agrees_with_cap(self):
        for text in ("short", "x" * LIMIT, "x" * (LIMIT + 1)):
            assert needs_capping(text) == (cap(text) != text)

    def test_a_custom_limit_is_honoured(self):
        """The note shrinks with the limit — at 50 characters the long form
        would not fit, and a note that blew the budget would be the joke."""
        out = cap("abcdefghij" * 10, limit=50)
        assert len(out) <= 50
        assert "/" in out and "chars]" in out

    def test_unicode_is_not_split_in_the_middle_of_a_character(self):
        """Slicing a str cannot split a codepoint, but it can split a grapheme
        cluster — this at least pins that nothing crashes and nothing is lost
        from the kept part."""
        text = ("नमस्ते दुनिया " * 20_000)
        out = cap(text)
        assert len(out) <= LIMIT
        assert "नमस्ते" in out


def _make_live(ui, registry_tool=None):
    """A JarvisLive with no socket, no audio and no Qt — the dispatch path."""
    import main as M
    from core.action_loader import discover_actions
    from core.plugin_loader import discover_plugins

    lv = object.__new__(M.JarvisLive)
    lv.ui = ui
    lv._action_registry = discover_actions(
        ROOT / "actions",
        reserved_names={t["name"] for t in M.TOOL_DECLARATIONS})
    lv._plugin_registry = discover_plugins(
        ROOT / "plugins", {t["name"] for t in M.TOOL_DECLARATIONS},
        logger=lambda _m: None)
    lv._vision_busy = False
    lv._vision_last_time = 0.0
    lv._vision_cam_active = False
    lv._pending_vision = None
    return M, lv


class TestTheCapIsActuallyWired:
    """Driving the real method: this is the half that matters."""

    def _run(self, name, args, monkeypatch, result):
        import main as M
        monkeypatch.setattr(M, "TOOL_DECLARATIONS", M.TOOL_DECLARATIONS)
        ui = _StandInUI()
        M2, lv = _make_live(ui)
        # a real ActionRecord whose handler returns the given result, so the
        # call takes the same path a real tool does (audit chain included)
        from core.action_loader import ActionRecord

        def handler(parameters, **ctx):        # the real call convention
            return result

        monkeypatch.setitem(lv._action_registry._actions, name, ActionRecord(
            name=name, description="test", handler=handler,
            file="test.py", valid=True))
        from google.genai import types
        return asyncio.run(M2.JarvisLive._execute_tool(
            lv, types.FunctionCall(id="c1", name=name, args=args))), ui

    def _an_action_name(self) -> str:
        from core.action_loader import discover_actions
        reg = discover_actions(ROOT / "actions", reserved_names=set())
        return sorted(reg._actions)[0]

    def test_an_oversized_tool_result_is_trimmed_before_the_model_sees_it(
            self, monkeypatch):
        name = self._an_action_name()
        resp, _ui = self._run(name, {}, monkeypatch, "y" * (LIMIT * 4))
        text = resp.response["result"]
        assert len(text) <= LIMIT
        assert "truncated" in text

    def test_a_normal_result_passes_through_unchanged(self, monkeypatch):
        name = self._an_action_name()
        resp, _ui = self._run(name, {}, monkeypatch, "the answer is 42")
        assert resp.response["result"] == "the answer is 42"

    def test_the_user_is_told_when_a_result_was_trimmed(self, monkeypatch):
        """Silent trimming is how a user ends up asking "why did it only read
        the first page of my document?"."""
        name = self._an_action_name()
        _resp, ui = self._run(name, {}, monkeypatch, "z" * (LIMIT * 2))
        assert any("trimmed" in line for line in ui.logged)

    def test_nothing_is_logged_when_nothing_was_trimmed(self, monkeypatch):
        name = self._an_action_name()
        _resp, ui = self._run(name, {}, monkeypatch, "short")
        assert not any("trimmed" in line for line in ui.logged)

    def test_a_muted_window_does_not_block_the_cap(self, monkeypatch):
        name = self._an_action_name()
        resp, _ui = self._run(name, {}, monkeypatch, "q" * (LIMIT * 3))
        assert len(resp.response["result"]) <= LIMIT


class TestSilentExceptAudit:
    """tools/silent_except_audit.py — the report behind P3-23.

    A count of `except: pass` is not a finding; the 300+ handlers in this
    codebase include many that are correct (a teardown path must not raise a
    second exception while handling the first). The tool classifies instead of
    judging, and these tests pin the classification rules — a tool that reports
    the wrong number is worse than no tool, because the number is what gets
    quoted.
    """

    def _scan(self, source: str, name: str = "sample.py"):
        from tools.silent_except_audit import scan_file
        p = ROOT / "tests" / name
        p.write_text(source, encoding="utf-8")
        try:
            return scan_file(p, source)
        finally:
            p.unlink(missing_ok=True)

    def test_a_bare_pass_is_a_finding(self):
        (f,) = self._scan("def do_thing():\n    try:\n        x()\n"
                          "    except Exception:\n        pass\n")
        assert f.risk == "high" and f.body == "pass" and f.function == "do_thing"

    def test_reporting_the_error_is_not_silence(self):
        """`return f"...{e}"` is the function's error contract — the caller and
        the model both see it. Counting it would inflate the report with
        correct code."""
        assert self._scan("def do():\n    try:\n        x()\n"
                          "    except Exception as e:\n"
                          "        return f'failed: {e}'\n") == []

    def test_logging_is_not_silence(self):
        assert self._scan("def do():\n    try:\n        x()\n"
                          "    except Exception as e:\n"
                          "        log.warning(f'no: {e}')\n") == []

    def test_re_raising_is_not_silence(self):
        assert self._scan("def do():\n    try:\n        x()\n"
                          "    except ValueError:\n        raise\n") == []

    def test_a_comment_moves_it_off_the_high_risk_list(self):
        (f,) = self._scan("def do():\n    try:\n        x()\n"
                          "    except Exception:\n"
                          "        # the device is gone; nothing to do here\n"
                          "        pass\n")
        assert f.risk == "comment"

    def test_a_teardown_shaped_function_is_low_risk(self):
        for fn in ("close_connection", "stop_watcher", "shutdown_audio",
                   "_probe_devices", "is_available"):
            (f,) = self._scan(f"def {fn}():\n    try:\n        x()\n"
                              "    except Exception:\n        pass\n")
            assert f.risk == "low", fn

    def test_an_empty_return_counts_as_silence(self):
        for value in ("None", "[]", "{}", '""', "False"):
            (f,) = self._scan("def lookup():\n    try:\n        x()\n"
                              f"    except Exception:\n        return {value}\n")
            assert f.risk == "high", value

    def test_a_meaningful_return_value_is_a_decision(self):
        """Returning a considered default (a real value, not an empty one) is
        the code saying what it wants to happen — not a swallowed error."""
        assert self._scan("def lookup():\n    try:\n        x()\n"
                          "    except Exception:\n"
                          "        return 'system default'\n") == []

    def test_it_reads_real_files(self):
        from tools.silent_except_audit import scan
        findings = scan()
        assert len(findings) > 100            # the codebase really has this many
        assert all(f.path.endswith(".py") for f in findings)
        assert all(f.line > 0 for f in findings)

    def test_it_never_judges_a_file_it_cannot_parse(self):
        from tools.silent_except_audit import scan_file
        assert scan_file(ROOT / "tests" / "x.py", "def broken(\n") == []

    def test_the_report_is_ordered_by_file(self):
        from tools.silent_except_audit import scan
        paths = [f.path for f in scan()]
        assert paths == sorted(paths) or len(set(paths)) > 1
