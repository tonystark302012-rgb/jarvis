"""`ui` is a package — and that is a fix, not a tidy-up.

Until now `ui` was a single 9,000-line module `ui.py`, so `ui` could not also be
a package. `from ui.display_panel import DisplayPanel` therefore raised
`'ui' is not a package`, the exception was swallowed by the panel's defensive
wrapper, and the display screen simply never opened — with no error anywhere.
The workaround that shipped was a loader reading `ui/display_panel.py` off disk
by path.

These tests pin the consequence (the submodule import works) rather than the
mechanism, and guard the two hazards the move introduced: `__file__` no longer
means "the repo root", and anything that read `ui.py` by name has to read the
package instead.
"""
from __future__ import annotations

import importlib.machinery
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui"


def _qt_is_available() -> bool:
    try:
        import PyQt6.QtWidgets  # noqa: F401
    except Exception:                                     # noqa: BLE001
        return False
    return True


HAVE_QT = _qt_is_available()
needs_qt = pytest.mark.skipif(not HAVE_QT, reason="no Qt/GL stack in this sandbox")


class TestLayout:
    def test_the_window_lives_in_the_package(self):
        assert (UI / "app.py").is_file()
        assert (UI / "display_panel.py").is_file()
        assert (UI / "__init__.py").is_file()

    def test_there_is_no_stray_ui_module_next_to_the_package(self):
        """A `ui.py` file and a `ui/` package cannot both win the name `ui` —
        the file wins, the package becomes unreachable, and this is exactly the
        state the repo was in."""
        assert not (ROOT / "ui.py").exists()

    def test_the_package_is_importable_and_not_empty(self):
        assert UI.joinpath("__init__.py").read_text(encoding="utf-8").strip()


class TestPublicSurface:
    @needs_qt
    def test_from_ui_import_jarvisui_still_works(self):
        """`main.py` says exactly this line, and so does the smoke tool."""
        from ui import JarvisUI
        assert isinstance(JarvisUI, type)
        assert JarvisUI.__name__ == "JarvisUI"

    @needs_qt
    def test_the_icon_helpers_are_reexported(self):
        from ui import _ICONS, _ToastLayer, icon_pm, set_icon
        assert isinstance(_ICONS, dict) and len(_ICONS) > 30
        assert callable(icon_pm) and callable(set_icon)
        assert isinstance(_ToastLayer, type)

    @needs_qt
    def test_the_window_class_comes_from_app(self):
        import ui
        import ui.app
        from ui import JarvisUI
        assert JarvisUI is ui.app.JarvisUI
        assert ui.__all__


class TestSubmoduleImport:
    """The actual bug: this import could never work while `ui` was a module."""

    def test_the_finder_resolves_ui_to_the_package(self):
        """`ui.py` next to `ui/` wins the name and the package becomes
        unreachable — PathFinder is where that decision is made."""
        spec = importlib.machinery.PathFinder.find_spec("ui", [str(ROOT)])
        assert spec is not None
        assert spec.submodule_search_locations is not None, (
            "`ui` resolved to a plain module — a ui.py file is shadowing the "
            "package again")
        assert Path(spec.origin).name == "__init__.py"

    def test_the_submodule_is_resolvable(self):
        """Resolution must work without importing the package (Qt) and without
        trusting sys.modules — other tests leave a fake `ui` in there."""
        spec = importlib.machinery.PathFinder.find_spec(
            "ui.display_panel", [str(UI)])
        assert spec is not None and spec.origin is not None
        assert Path(spec.origin).name == "display_panel.py"

    def test_importing_the_package_does_not_pull_qt(self):
        """`import ui` must stay cheap: main.py's --version path and headless
        tools import it, and a GL stack is not always there."""
        src = (UI / "__init__.py").read_text(encoding="utf-8")
        assert "__getattr__" in src
        assert "import PyQt6" not in src

    def test_it_imports_without_a_gl_stack_when_qt_is_stubbed(self, monkeypatch):
        """display_panel guards its own Qt import, so this works on a box with
        no libGL — which is how CI verifies the fix on a headless runner."""
        if HAVE_QT:
            pytest.skip("real Qt present — the stub would shadow it")
        import types
        stub = types.ModuleType("PyQt6")
        for sub in ("QtCore", "QtGui", "QtWidgets"):
            mod = types.ModuleType(f"PyQt6.{sub}")
            for name in ("QWidget", "QVBoxLayout", "QHBoxLayout", "QTextBrowser",
                         "QPushButton", "QLabel", "QTimer"):
                setattr(mod, name, type(name, (), {}))
            setattr(stub, sub, mod)
            monkeypatch.setitem(sys.modules, f"PyQt6.{sub}", mod)
        monkeypatch.setitem(sys.modules, "PyQt6", stub)
        for name in [n for n in sys.modules if n == "ui" or n.startswith("ui.")]:
            monkeypatch.delitem(sys.modules, name, raising=False)
        mod = __import__("ui.display_panel", fromlist=["DisplayPanel"])
        assert hasattr(mod, "DisplayPanel")

    def test_the_by_path_loader_is_gone(self):
        """Keeping it would leave two ways to load one file, and the reason it
        existed is the thing that got fixed."""
        src = (UI / "app.py").read_text(encoding="utf-8")
        assert "spec_from_file_location" not in src
        assert 'BASE_DIR / "ui" / "display_panel.py"' not in src
        assert "from ui.display_panel import DisplayPanel" in src

    @needs_qt
    def test_the_panel_loader_returns_a_widget(self):
        from ui.app import _load_display_panel
        panel = _load_display_panel()
        assert panel is not None


class TestTheMoveDidNotBreakPaths:
    """`__file__` used to be the repo root because the module sat there."""

    def test_no_path_is_derived_from_dunder_file(self):
        src = (UI / "app.py").read_text(encoding="utf-8")
        bad = re.findall(r'Path\(__file__\)[^\n]*', src)
        assert not bad, f"these would now point into ui/: {bad}"

    def test_autostart_points_at_the_real_entry_point(self):
        src = (UI / "app.py").read_text(encoding="utf-8")
        assert src.count('base_dir() / "main.py"') >= 2
        assert (ROOT / "main.py").exists()

    def test_the_app_root_helper_uses_the_shared_one(self):
        src = (UI / "app.py").read_text(encoding="utf-8")
        assert "from core.paths import base_dir" in src


class TestReadersOfTheSource:
    """Several tests and one dev tool read the HUD as text. They must read the
    package, not a file that no longer exists."""

    def test_the_source_helper_reads_every_module(self):
        from tests._ui_source import ui_source
        text = ui_source()
        assert "class JarvisUI" in text
        assert len(text) > 200_000

    def test_no_test_still_reads_the_old_module(self):
        """AST, not a text search: the guards that describe the old name build
        it as `"ui" + ".py"` and regexes escape it, so a text search finds
        itself. A constant string equal to the old filename is the thing that
        actually opens it."""
        import ast
        needle = "ui" + ".py"                     # not written literally here
        offenders = []
        for p in (ROOT / "tests").glob("*.py"):
            if p.name == Path(__file__).name:
                continue
            src = p.read_text(encoding="utf-8")
            lines = src.splitlines()
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Constant) or node.value != needle:
                    continue
                # `not (ROOT / "ui.py").exists()` asserts it is GONE — the
                # opposite of a stale reference.
                line = lines[node.lineno - 1]
                if ".exists()" in line:
                    continue
                offenders.append(f"{p.name}:{node.lineno}")
        assert not offenders, f"still naming the old module: {offenders}"

    def test_the_smoke_tool_still_parses(self):
        out = subprocess.run([sys.executable, "-m", "compileall", "-q",
                              str(ROOT / "tools" / "ui_smoke.py")],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-400:]
