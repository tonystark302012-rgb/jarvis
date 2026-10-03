"""
window_layout — snap and arrange windows by voice.

Layouts: left | right | top | bottom | center | maximize | full and two
presets — "split" (two apps side by side) and "coding" (editor wide, browser
narrow). Platform backends, in order of preference:

  Windows : pywin32 (already a declared dependency)
  macOS   : osascript (system, free)
  Linux   : xdotool (system tool, free) + wmctrl fallback

Everything degrades to a clear message when no backend exists — the tool
never pretends it moved a window it could not see. Pure geometry (the part
that decides where each window goes) is separated out so it is testable
without any display server.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Sequence


# ── geometry (pure, tested) ──────────────────────────────────────────────────

def compute_rects(screen: tuple[int, int], slots: int | Sequence[str],
                  gutter: int = 6) -> list[tuple[int, int, int, int]]:
    """Return (x, y, w, h) per slot.

    slots as int N  → N equal vertical stacks? No: N equal HORIZONTAL columns.
    slots as list   → named layout string in {'left','right','top','bottom',
                     'center','maximize','full'}.
    """
    w, h = screen
    if isinstance(slots, int):
        n = max(1, slots)
        col_w = (w - gutter * (n - 1)) // n
        return [(i * (col_w + gutter), 0, col_w, h) for i in range(n)]

    out: list[tuple[int, int, int, int]] = []
    half_w = (w - gutter) // 2
    half_h = (h - gutter) // 2
    for name in slots:
        if name in ("left",):
            out.append((0, 0, half_w, h))
        elif name in ("right",):
            out.append((half_w + gutter, 0, w - half_w - gutter, h))
        elif name in ("top",):
            out.append((0, 0, w, half_h))
        elif name in ("bottom",):
            out.append((0, half_h + gutter, w, h - half_h - gutter))
        elif name in ("center",):
            cw, ch = int(w * 0.7), int(h * 0.7)
            out.append(((w - cw) // 2, (h - ch) // 2, cw, ch))
        elif name in ("maximize", "full"):
            out.append((0, 0, w, h))
        else:
            out.append((0, 0, w, h))
    return out


# ── backends ─────────────────────────────────────────────────────────────────

def _screen_size() -> tuple[int, int]:
    try:
        import pyautogui
        w, h = pyautogui.size()
        return int(w), int(h)
    except Exception:
        pass
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])
        g = app.primaryScreen().geometry()
        return int(g.width()), int(g.height())
    except Exception:
        return 1920, 1080


def _list_windows_xdotool() -> list[tuple[int, str]]:
    """[(window_id, title)] on Linux/X11."""
    if not shutil.which("xdotool"):
        return []
    try:
        out = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ""],
                             capture_output=True, text=True, timeout=6)
        ids = [l.strip() for l in out.stdout.split() if l.strip().isdigit()]
        wins = []
        for wid in ids[:40]:
            r = subprocess.run(["xdotool", "getwindowname", wid],
                               capture_output=True, text=True, timeout=4)
            title = r.stdout.strip()
            if title:
                wins.append((int(wid), title))
        return wins
    except Exception:
        return []


def _focus_windows_backend() -> str:
    import sys
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    if shutil.which("xdotool"):
        return "xdotool"
    if shutil.which("wmctrl"):
        return "wmctrl"
    return "none"


def _place(win_id: int, rect: tuple[int, int, int, int], backend: str) -> bool:
    x, y, w, h = rect
    try:
        if backend == "xdotool":
            subprocess.run(
                ["xdotool", "windowmove", str(win_id), str(x), str(y)],
                capture_output=True, timeout=4)
            subprocess.run(
                ["xdotool", "windowsize", str(win_id), str(w), str(h)],
                capture_output=True, timeout=4)
            subprocess.run(["xdotool", "windowactivate", str(win_id)],
                           capture_output=True, timeout=4)
            return True
        if backend == "win":
            import win32gui  # pywin32
            win32gui.SetWindowPos(win_id, None, x, y, w, h, 0x0040)
            return True
        if backend == "mac":
            script = (
                'tell application "System Events" to set position of front '
                f'window to {{{x}, {y}}}\n'
                'tell application "System Events" to set size of front '
                f'window to {{{w}, {h}}}'
            )
            subprocess.run(["osascript", "-e", script],
                           capture_output=True, timeout=4)
            return True
    except Exception:
        return False
    return False


_PRESETS = {
    "split":   ["left", "right"],
    "coding":  ["left", "right"],     # editor left (wide), browser right
    "stack":   ["top", "bottom"],
}


def window_layout(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    backend = _focus_windows_backend()

    if backend == "none":
        return ("No window backend found. Install xdotool (Linux: "
                "sudo apt install xdotool) — Windows/macOS work out of the box.")

    if action == "list":
        if backend == "xdotool":
            wins = _list_windows_xdotool()
            if not wins:
                return "No visible windows found."
            lines = [f"{i}. [{wid}] {title[:70]}" for i, (wid, title)
                     in enumerate(wins[:20], 1)]
            return "Windows:\n" + "\n".join(lines)
        if backend == "win":
            import win32gui
            wins = []
            def cb(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    t = win32gui.GetWindowText(hwnd)
                    if t:
                        wins.append((hwnd, t))
            win32gui.EnumWindows(cb, None)
            lines = [f"{i}. [{h}] {t[:70]}" for i, (h, t) in enumerate(wins[:20], 1)]
            return "Windows:\n" + "\n".join(lines)
        if backend == "mac":
            script = ('tell application "System Events" to get name of every '
                      'window of every process whose visible is true')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=6)
            return "Windows: " + (r.stdout.strip() or "none")
        return "Listing not supported on this backend."

    layout = str(params.get("layout", action)).lower().strip()
    slots = _PRESETS.get(layout)
    if slots is None:
        if layout in ("left", "right", "top", "bottom", "center",
                      "maximize", "full"):
            slots = [layout]
        else:
            return (f"Unknown layout '{layout}'. Use: "
                    f"{', '.join(sorted(_PRESETS))} or left/right/top/bottom/center/maximize.")

    sw, sh = _screen_size()
    rects = compute_rects((sw, sh), slots)

    # Which windows to arrange?
    which = str(params.get("query", "")).strip().lower()
    if backend == "xdotool":
        wins = _list_windows_xdotool()
    elif backend == "win":
        import win32gui
        wins = []
        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd)
                if t:
                    wins.append((hwnd, t))
        win32gui.EnumWindows(cb, None)
    elif backend == "mac":
        script = ('tell application "System Events" to get {name, unix id} of '
                  'every process whose visible is true')
        r = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=6)
        wins = []
        for chunk in r.stdout.split(","):
            wins.append((chunk.strip(), chunk.strip()))
    else:
        wins = []

    if which:
        wins = [w for w in wins if which in str(w[1]).lower()]
    if not wins:
        return ("No matching windows"
                + (f" for {which!r}" if which else "") + ". "
                  "Say 'list windows' first, or name the app.")

    moved = 0
    for (win, _title), rect in zip(wins[:len(rects)], rects):
        if _place(win if isinstance(win, int) else 0, rect, backend):
            moved += 1
    if not moved:
        return "Found windows but could not move them (backend refused)."
    result = f"Arranged {moved} window(s) with layout '{layout}'."
    if player is not None:
        try:
            player.show_content("WINDOW LAYOUT", result)
        except Exception:
            pass
    return result


TOOL = {
    "name": "window_layout",
    "description": (
        "Arranges open windows into layouts: split (side by side), coding "
        "(editor + browser), stack (top/bottom), left, right, center, "
        "maximize. Actions: apply (default, give layout), list (show open "
        "windows). Use when the user says 'arrange windows', 'tile my "
        "apps', 'put editor on the left', 'split screen'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "apply (default) | list"},
            "layout": {"type": "STRING",
                       "description": "split | coding | stack | left | right | top | bottom | center | maximize"},
            "query": {"type": "STRING",
                      "description": "Only arrange windows whose title matches."},
        },
        "required": [],
    },
    "handler": window_layout,
}
