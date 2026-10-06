"""Logging, diagnostics, version and the doctor.

The project had no stdlib logging at all — zero `import logging`, ~430 prints —
so nothing recorded what happened, and a crash on a GUI launch went to a stderr
nobody reads. These tests pin the pieces that make a bug report possible: a log
file that exists, a `print()` transcript that reaches it, a crash that gets
recorded, a redaction that actually redacts, and CLI flags that work without a
display.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from tests._ui_source import ui_source

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def logdir(tmp_path, monkeypatch):
    """Fresh logging config per test — setup() is global state, so it is
    reverted: leaving a handler installed would make unrelated tests write to
    a deleted tmp_path."""
    from core import logging_setup
    before_stdout = sys.stdout
    before_handlers = list(logging.getLogger().handlers)
    monkeypatch.setenv("JARVIS_LOG_MIRROR", "0")    # opt out by default
    yield tmp_path
    logging_setup.mirror_prints(False)
    sys.stdout = before_stdout
    root = logging.getLogger()
    for h in list(root.handlers):
        if h not in before_handlers:
            root.removeHandler(h)


class TestSetup:
    def test_it_writes_a_log_file(self, logdir):
        from core import logging_setup
        path = logging_setup.setup(directory=logdir, console=False, mirror=False)
        assert path == logdir / "jarvis.log"
        logging_setup.get_logger("t").warning("something happened")
        assert "something happened" in path.read_text(encoding="utf-8")

    def test_lines_carry_a_timestamp_and_level(self, logdir):
        from core import logging_setup
        path = logging_setup.setup(directory=logdir, console=False, mirror=False)
        logging_setup.get_logger("t").error("boom")
        line = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                if "boom" in ln][-1]
        assert "ERROR" in line and line[:4].isdigit() and "jarvis.t" in line

    def test_repeated_setup_does_not_stack_handlers(self, logdir):
        """Called twice (a reconnect, a test, a host app) the file must not
        receive every line twice."""
        from core import logging_setup
        for _ in range(3):
            path = logging_setup.setup(directory=logdir, console=False,
                                       mirror=False)
        logging_setup.get_logger("t").warning("once")
        assert path.read_text(encoding="utf-8").count("once") == 1

    def test_it_is_not_configured_by_import_alone(self, logdir):
        """Importing a module must not reconfigure logging for the process —
        the app calls setup() from its entry point."""
        import importlib
        from core import logging_setup
        importlib.reload(logging_setup)
        assert logging_setup.is_configured() is False

    def test_the_log_directory_is_overridable(self, monkeypatch, tmp_path):
        from core import logging_setup
        monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path / "custom"))
        assert logging_setup.log_dir() == tmp_path / "custom"

    def test_the_default_is_outside_the_checkout(self, monkeypatch):
        """A checkout should not collect runtime noise that then has to be
        git-ignored, and a frozen build may be read-only."""
        from core import logging_setup
        monkeypatch.delenv("JARVIS_LOG_DIR", raising=False)
        assert ROOT not in logging_setup.log_dir().parents


class TestPrintMirroring:
    """The mirror is the argument worth having: ~430 prints are the app's
    console UX, and rewriting them all would change what the user sees. So
    they are captured as they are, and the migration can happen later."""

    def test_prints_reach_the_log(self, logdir, monkeypatch):
        from core import logging_setup
        monkeypatch.setenv("JARVIS_LOG_MIRROR", "1")
        path = logging_setup.setup(directory=logdir, console=False, mirror=True)
        print("[JARVIS] 🔧 web_search  {'query': 'x'}")
        logging_setup.mirror_prints(False)
        text = path.read_text(encoding="utf-8")
        assert "🔧 web_search" in text

    def test_stdout_still_gets_the_print(self, logdir, capsys, monkeypatch):
        from core import logging_setup
        monkeypatch.setenv("JARVIS_LOG_MIRROR", "1")
        logging_setup.setup(directory=logdir, console=False, mirror=True)
        print("both places")
        logging_setup.mirror_prints(False)
        assert "both places" in capsys.readouterr().out

    def test_turning_it_off_restores_stdout(self, logdir, monkeypatch):
        from core import logging_setup
        monkeypatch.setenv("JARVIS_LOG_MIRROR", "1")
        logging_setup.setup(directory=logdir, console=False, mirror=True)
        assert sys.stdout is not sys.__stdout__
        logging_setup.mirror_prints(False)
        assert not isinstance(sys.stdout, logging_setup._Tee)

    def test_it_can_be_disabled_by_environment(self, logdir, monkeypatch):
        from core import logging_setup
        monkeypatch.setenv("JARVIS_LOG_MIRROR", "0")
        assert logging_setup.mirror_prints(True) is False

    def test_the_mirror_honours_a_custom_directory(self, logdir, monkeypatch):
        """It used to write to the default location regardless — the tee was
        built from `log_path()` instead of the file just configured."""
        from core import logging_setup
        monkeypatch.setenv("JARVIS_LOG_MIRROR", "1")
        path = logging_setup.setup(directory=logdir, console=False, mirror=True)
        print("custom dir please")
        logging_setup.mirror_prints(False)
        assert "custom dir please" in path.read_text(encoding="utf-8")


class TestCrashRecording:
    def test_excepthook_records_with_a_traceback(self, logdir):
        from core import logging_setup
        path = logging_setup.setup(directory=logdir, console=False, mirror=False)

        def boom(kind, value, tb):        # stand-in for the real hook it wraps
            pass

        real = sys.excepthook
        try:
            sys.excepthook = boom
            logging_setup.install_excepthook()
            try:
                raise ValueError("the widget exploded")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook = real
        text = path.read_text(encoding="utf-8")
        assert "the widget exploded" in text
        assert "Traceback" in text and "ValueError" in text

    def test_it_still_calls_the_previous_hook(self, logdir):
        """The default hook prints to stderr; swallowing it would make the
        terminal *quieter* than before, which is not the point."""
        from core import logging_setup
        logging_setup.setup(directory=logdir, console=False, mirror=False)
        seen = []
        real = sys.excepthook
        try:
            sys.excepthook = lambda *a: seen.append(a)
            logging_setup.install_excepthook()
            try:
                raise ValueError("x")
            except ValueError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook = real
        assert len(seen) == 1


class TestRedaction:
    @pytest.mark.parametrize("key", [
        "gemini_api_key", "GEMINI_API_KEY", "apiKey", "slack_token",
        "user_password", "device_pin", "client_secret", "private_key",
    ])
    def test_secret_shaped_keys_are_replaced(self, key):
        from core.logging_setup import redact
        out = redact({key: "super-secret-value"})
        assert out[key] == "<redacted>"

    def test_ordinary_settings_survive(self):
        from core.logging_setup import redact
        cfg = {"assistant_name": "JARVIS", "user_name": "Tony",
               "privacy_mode": True, "voice": "Puck"}
        assert redact(cfg) == cfg

    def test_an_empty_secret_stays_empty(self):
        """A config with no key should look like a config with no key, not
        like a config whose key was redacted — the difference matters when
        you are reading the bundle to find out why it will not start."""
        from core.logging_setup import redact
        assert redact({"gemini_api_key": ""}) == {"gemini_api_key": ""}

    def test_nested_structures_are_walked(self):
        from core.logging_setup import redact
        out = redact({"plugins": {"gmail": {"password": "hunter2"}},
                      "hosts": [{"token": "abc"}]})
        assert out["plugins"]["gmail"]["password"] == "<redacted>"
        assert out["hosts"][0]["token"] == "<redacted>"


class TestDiagnosticsBundle:
    def test_the_bundle_has_the_parts_a_report_needs(self, logdir, tmp_path,
                                                     monkeypatch):
        from core import logging_setup, paths
        logging_setup.setup(directory=logdir, console=False, mirror=False)
        logging_setup.get_logger("t").info("a line for the bundle")
        cfg = tmp_path / "api_keys.json"
        cfg.write_text('{"gemini_api_key": "REAL-KEY-VALUE", '
                       '"assistant_name": "JARVIS"}', encoding="utf-8")
        monkeypatch.setattr(paths, "api_keys_path", lambda: cfg)

        dest = logging_setup.export_diagnostics(tmp_path / "d.zip")
        with zipfile.ZipFile(dest) as z:
            names = set(z.namelist())
            assert {"summary.txt", "jarvis.log",
                    "config.redacted.json", "audit.txt"} <= names
            blob = z.read("config.redacted.json").decode()
            assert "REAL-KEY-VALUE" not in blob
            assert "JARVIS" in blob                  # the harmless one stays
            assert "a line for the bundle" in z.read("jarvis.log").decode()

    def test_it_works_with_nothing_to_report(self, logdir, tmp_path):
        """A bundle that fails because there was no log yet is worse than
        useless — that is the state you are in when you ask for it."""
        from core import logging_setup
        dest = logging_setup.export_diagnostics(tmp_path / "empty.zip")
        assert dest.exists()

    def test_the_summary_names_the_state(self, logdir):
        from core import logging_setup
        logging_setup.setup(directory=logdir, console=False, mirror=False)
        summary = logging_setup.diagnostics_summary()
        assert "JARVIS" in summary and "python" in summary
        assert "app root" in summary and "audit" in summary


class TestVersion:
    def test_one_version_answers_everywhere(self):
        from core.version import __version__, full_version, version_string
        assert version_string() == f"JARVIS {__version__}"
        assert full_version().startswith(version_string())

    def test_it_is_not_a_placeholder(self):
        from core.version import __version__
        assert __version__.count(".") == 2
        assert all(part.isdigit() for part in __version__.split("."))


class TestCLIFlags:
    """These must work on a machine where the app's own dependencies are what
    is broken — that is the state you are in when you ask for the version."""

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(ROOT / "main.py"), *args],
            capture_output=True, text=True, timeout=120,
            cwd=str(ROOT), env={**_clean_env(), "JARVIS_LOG_DIR":
                                str(ROOT / ".pytest-logs")})

    def test_version_prints_and_exits_zero(self):
        out = self._run("--version")
        assert out.returncode == 0, out.stderr[-400:]
        assert out.stdout.startswith("JARVIS ") and "Python" in out.stdout

    def test_version_needs_no_display(self):
        """Regression guard: the Qt import used to happen at module load, so
        this flag crashed with libGL before it could print anything."""
        assert "libGL" not in self._run("--version").stderr

    def test_doctor_reports_and_does_not_crash(self):
        out = self._run("--doctor")
        assert "python" in out.stdout.lower()
        assert out.returncode in (0, 1)          # 1 = something to fix
        assert "Traceback" not in out.stderr

    def test_diagnostics_writes_a_bundle(self, tmp_path):
        out = self._run("--diagnostics")
        assert out.returncode == 0, out.stderr[-400:]
        assert "wrote" in out.stdout


def _clean_env() -> dict:
    import os
    return {k: v for k, v in os.environ.items()
            if k not in ("JARVIS_LOG_DIR", "JARVIS_LOG_MIRROR")}


class TestUIWiring:
    """The controls drawer is built at runtime, so a typo in an icon name or a
    handler is invisible until someone opens it. These read the source."""

    def _ui(self) -> str:
        return ui_source()

    def test_every_icon_name_used_exists(self):
        """`set_icon(btn, "download", …)` silently does nothing when the name
        is not in the table — the button just comes up bare."""
        import re
        src = self._ui()
        table = re.search(r"_ICONS\s*[:=].*?\{(.*?)\n\}", src, re.S)
        assert table, "icon table not found"
        known = set(re.findall(r'"([a-z_0-9]+)":', table.group(1)))
        assert len(known) > 30, "icon table looks wrong"
        used = set(re.findall(r'set_icon\(\s*[\w.]+\s*,\s*"([a-z_0-9]+)"', src))
        used |= set(re.findall(r'_icon_label\(\s*"([a-z_0-9]+)"', src))
        unknown = sorted(used - known)
        assert not unknown, f"these icon names do not exist: {unknown}"

    def test_the_diagnostics_button_is_wired(self):
        src = self._ui()
        assert "EXPORT DIAGNOSTICS" in src
        assert "def _export_diagnostics" in src
        assert "self._diag_btn.clicked.connect(self._export_diagnostics)" in src

    def test_the_diagnostics_handler_never_raises_into_the_ui(self):
        """A missing log directory must not take the window down with it."""
        src = self._ui()
        body = src.split("def _export_diagnostics", 1)[1].split("\n    def ", 1)[0]
        assert "try:" in body and "except Exception" in body
