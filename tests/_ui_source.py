"""Read the HUD source for the tests that assert on it.

`ui` is a package now (it used to be a single ui.py). Several tests read the
source text to check wiring that cannot be exercised without a display. Those
tests should not break every time a panel moves to its own module, so they all
read the package through this one helper.
"""
from __future__ import annotations

from pathlib import Path

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def ui_source() -> str:
    """Every .py in the ui/ package, concatenated.

    Order is by name so `_ICONS` is present before the code that uses it, but
    the tests only ever search the text.
    """
    parts = [p.read_text(encoding="utf-8") for p in sorted(UI_DIR.glob("*.py"))]
    return "\n".join(parts)


def ui_file(name: str = "app.py") -> Path:
    return UI_DIR / name
