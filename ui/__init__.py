"""
ui — the PyQt6 HUD, as a package.

It used to be a single 9,000-line module `ui.py`, and that had one consequence
beyond tidiness: `ui` could not also be a package, so the normal, obvious
`from ui.display_panel import DisplayPanel` raised `'ui' is not a package`, the
exception was swallowed by the panel's defensive wrapper, and the display screen
silently never opened. The workaround that shipped read the file off disk by
path.

The exports below are lazy (PEP 562). Qt pulls in a GL stack, and `import ui`
happens on paths that must not pay for one — `python main.py --version`, a
headless tool that only wants the icon table, the test suite on a runner.
`from ui import JarvisUI` works exactly as before and loads Qt at that moment.
"""

from __future__ import annotations

_LAZY = {
    "JarvisUI": "ui.app",
    "icon_pm": "ui.app",
    "set_icon": "ui.app",
    "set_icons": "ui.app",
    "_icon_label": "ui.app",
    "_ICONS": "ui.app",
    "_ToastLayer": "ui.app",
}

__all__ = ["JarvisUI", "icon_pm", "set_icon", "_ICONS", "_ToastLayer"]


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value           # cache: one import, then a plain lookup
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
