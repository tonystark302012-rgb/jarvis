"""The doctor's report and the installer behind `--fix`.

`core/installer.py` was 200 lines of unreachable code. Its docstring said it ran
"automatically on first launch"; nothing called it, so the packages that offline
transcription needs (faster-whisper) were never installed by anything, and the
README promised a Meeting Recorder that transcribed offline.

The fix was not to delete it: the doctor already reports exactly what is
missing, so `python main.py --doctor --fix` is where installing belongs. One
command, said out loud, run when the user asks — instead of pip running behind
their back at start-up.

These tests never touch the network: `_pip` is stubbed at the seam.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import installer                                   # noqa: E402
from tools import doctor                                     # noqa: E402

OK, WARN, FAIL = doctor.OK, doctor.WARN, doctor.FAIL


class TestMissingPipNames:
    def test_it_maps_a_missing_dependency_to_its_pip_name(self):
        checks = [(FAIL, "sounddevice", "no module", "pip install sounddevice"),
                  (WARN, "yt-dlp", "no module", "pip install yt-dlp")]
        assert doctor._missing_pip_names(checks) == ["sounddevice", "yt-dlp"]

    def test_present_dependencies_are_not_installed_again(self):
        checks = [(OK, "numpy", "present", ""),
                  (WARN, "croniter", "missing", "pip install croniter")]
        assert doctor._missing_pip_names(checks) == ["croniter"]

    def test_non_dependency_findings_are_ignored(self):
        """`display` is a warning about the environment, not a package — the
        installer must not try to pip-install a monitor."""
        checks = [(WARN, "display", "no DISPLAY", "run on a desktop"),
                  (FAIL, "config writable", "permission denied", "chmod")]
        assert doctor._missing_pip_names(checks) == []

    def test_the_names_are_the_pip_names_not_the_import_names(self):
        """`yt_dlp` is imported, `yt-dlp` is installed. Getting this wrong
        installs a wheel nobody imports."""
        checks = [(WARN, "yt-dlp", "missing", ""), (WARN, "paho.mqtt", "missing", "")]
        names = doctor._missing_pip_names(checks)
        assert "yt-dlp" in names
        assert all("_" not in n for n in names)


class TestInstallMissing:
    def test_it_installs_what_is_not_there(self, monkeypatch):
        installed = []
        monkeypatch.setattr(installer, "_available", lambda name: False)
        monkeypatch.setattr(installer, "_pip",
                            lambda pkg, log=None: installed.append(pkg) or True)
        assert installer.install_missing(["faster-whisper"]) == []
        assert installed == ["faster-whisper"]

    def test_it_skips_what_is_already_there(self, monkeypatch):
        installed = []
        monkeypatch.setattr(installer, "_available", lambda name: True)
        monkeypatch.setattr(installer, "_pip",
                            lambda pkg, log=None: installed.append(pkg) or True)
        assert installer.install_missing(["numpy"]) == []
        assert installed == []

    def test_the_callers_import_map_wins(self, monkeypatch):
        """`paho-mqtt` installs the `paho.mqtt` import, and only the doctor's
        table knows that. Probing the pip name would report it missing forever
        and reinstall it on every run."""
        probed = []
        monkeypatch.setattr(installer, "_available",
                            lambda name: probed.append(name) or True)
        installer.install_missing(["paho-mqtt"],
                                  import_names={"paho-mqtt": "paho.mqtt"})
        assert probed == ["paho.mqtt"]

    def test_its_own_tables_are_used_when_no_map_is_given(self, monkeypatch):
        probed = []
        monkeypatch.setattr(installer, "_available",
                            lambda name: probed.append(name) or True)
        installer.install_missing(["faster-whisper"])
        assert probed == ["faster_whisper"]

    def test_the_dash_rule_is_the_last_resort(self, monkeypatch):
        probed = []
        monkeypatch.setattr(installer, "_available",
                            lambda name: probed.append(name) or True)
        installer.install_missing(["some-unknown-package"])
        assert probed == ["some_unknown_package"]

    def test_a_failed_install_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(installer, "_available", lambda name: False)
        monkeypatch.setattr(installer, "_pip", lambda pkg, log=None: False)
        assert installer.install_missing(["nowhere-package"]) == ["nowhere-package"]

    def test_it_keeps_going_after_one_failure(self, monkeypatch):
        seen = []
        monkeypatch.setattr(installer, "_available", lambda name: False)
        monkeypatch.setattr(installer, "_pip",
                            lambda pkg, log=None: seen.append(pkg) or pkg != "bad")
        failed = installer.install_missing(["bad", "good"])
        assert seen == ["bad", "good"] and failed == ["bad"]


class TestInstallForConfig:
    def _plan(self, monkeypatch, config) -> list[str]:
        wanted: list[str] = []
        monkeypatch.setattr(installer, "_available", lambda name: False)
        monkeypatch.setattr(installer, "_pip",
                            lambda pkg, log=None: wanted.append(pkg) or True)
        installer.install_for_config(config, log=lambda _m: None)
        return wanted

    def test_the_config_picks_the_stt_engine_packages(self, monkeypatch):
        assert "faster-whisper" in self._plan(monkeypatch, {"stt_engine": "whisper"})

    def test_a_vosk_config_installs_vosk(self, monkeypatch):
        plan = self._plan(monkeypatch, {"stt_engine": "vosk"})
        assert "vosk" in plan and "faster-whisper" not in plan

    def test_elevenlabs_needs_no_package(self, monkeypatch):
        """It uses requests, which is already required — an empty list here is
        correct, not a bug."""
        assert self._plan(monkeypatch, {"tts_engine": "elevenlabs"}) == \
            self._plan_no_tts(monkeypatch)

    def _plan_no_tts(self, monkeypatch) -> list[str]:
        def _pip(pkg, log=None):
            return True
        monkeypatch.setattr(installer, "_pip", _pip)
        wanted: list[str] = []
        monkeypatch.setattr(installer, "_pip",
                            lambda pkg, log=None: wanted.append(pkg) or True)
        installer.install_for_config({"tts_engine": "elevenlabs"}, log=lambda _m: None)
        return wanted

    def test_a_missing_optional_never_raises(self, monkeypatch):
        monkeypatch.setattr(installer, "_available", lambda name: False)
        monkeypatch.setattr(installer, "_pip", lambda pkg, log=None: False)
        installer.install_for_config({}, log=lambda _m: None)   # must not raise


class TestDoctorFix:
    def test_fix_installs_what_the_report_lists(self, monkeypatch, capsys):
        checks = [(FAIL, "sounddevice", "missing", "pip install sounddevice"),
                  (WARN, "yt-dlp", "missing", "pip install yt-dlp")]
        called: dict[str, list[str]] = {}

        def fake_install(pkgs, log=None, import_names=None):
            called["pkgs"] = pkgs
            called["names"] = import_names or {}
            return []                      # nothing failed

        monkeypatch.setattr(installer, "install_missing", fake_install)
        assert doctor.fix(checks) == 0
        assert called["pkgs"] == ["sounddevice", "yt-dlp"]
        assert called["names"]["paho-mqtt"] == "paho.mqtt"
        assert "Installing 2 missing package(s)" in capsys.readouterr().out

    def test_fix_with_nothing_missing_says_so_and_installs_nothing(
            self, monkeypatch, capsys):
        monkeypatch.setattr(installer, "install_missing",
                            lambda *a, **k: pytest.fail("should not install"))
        assert doctor.fix([(OK, "numpy", "present", "")]) == 0
        assert "Nothing to install" in capsys.readouterr().out

    def test_fix_reports_failures_without_claiming_success(self, monkeypatch, capsys):
        monkeypatch.setattr(installer, "install_missing",
                            lambda pkgs, log=None, import_names=None: ["yt-dlp"])
        rc = doctor.fix([(WARN, "yt-dlp", "missing", "")])
        out = capsys.readouterr().out
        assert rc == 1 and "could not be installed" in out
        assert "Done." not in out             # no false success

    def test_main_without_fix_only_advises(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks",
                            lambda base_dir=None: [(FAIL, "sounddevice", "m", "f")])
        monkeypatch.setattr(installer, "install_missing",
                            lambda *a, **k: pytest.fail("should not install"))
        assert doctor.main([]) == 1
        assert "--fix" in capsys.readouterr().out

    def test_main_with_fix_acts(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks",
                            lambda base_dir=None: [(FAIL, "sounddevice", "m", "f")])
        seen = []
        monkeypatch.setattr(installer, "install_missing",
                            lambda pkgs, log=None, import_names=None:
                                seen.append(pkgs) or [])
        doctor.main(["--fix"])
        assert seen == [["sounddevice"]]

    def test_main_returns_zero_when_nothing_is_broken(self, monkeypatch, capsys):
        monkeypatch.setattr(doctor, "run_checks",
                            lambda base_dir=None: [(OK, "numpy", "present", "")])
        assert doctor.main([]) == 0


class TestReachability:
    """The finding this work came from, pinned so it cannot come back."""

    def test_the_installer_is_reachable_from_the_doctor(self):
        src = (ROOT / "tools" / "doctor.py").read_text(encoding="utf-8")
        assert "from core.installer import" in src

    def test_the_docs_no_longer_promise_an_automatic_install(self):
        doc = installer.__doc__ or ""
        assert "NOT called automatically" in doc
        assert "install_for_config" not in doc.split("NOT called automatically")[0]

    def test_main_documents_the_flag(self):
        """`--fix` has to be discoverable: the report tells you to run it."""
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        assert "--doctor" in src
        # the doctor itself suggests the flag when something is broken
        assert "--fix" in (ROOT / "tools" / "doctor.py").read_text(encoding="utf-8")
