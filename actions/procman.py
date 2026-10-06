"""
procman — process manager: list, details, kill.

Reads are free; killing requires confirm="yes" and refuses JARVIS itself,
the session's own PID, and PID ≤ system reserved. psutil does the lifting —
already a core dependency of scanner/system_monitor.
"""
from __future__ import annotations

import os
import sys

_MAX_ROWS = 25


def _psutil():
    import psutil
    return psutil


def _self_pids() -> set[int]:
    """Our own process tree — never kill the assistant mid-reply."""
    pids = {os.getpid()}
    try:
        p = _psutil().Process(os.getpid())
        pids.update(c.pid for c in p.children(recursive=True))
    except Exception:
        pass
    if sys.platform != "win32":
        pids.add(1)                     # init/systemd
    return pids


def _fmt_row(p) -> str:
    try:
        mem = p.memory_info().rss / (1024 * 1024)
        cpu = p.cpu_percent(interval=0.0)
        return f"{p.pid:>7}  {cpu:5.1f}%  {mem:6.1f}MB  {p.name()[:40]}"
    except Exception:
        return f"{p.pid:>7}  (gone)"


def procman(parameters: dict | None = None, player=None, session_memory=None) -> str:
    psutil = _psutil()
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    query = str(params.get("query", "")).strip().lower()

    if action == "kill":
        raw = params.get("pid", "")
        name = str(params.get("name", "")).strip().lower()
        if str(params.get("confirm", "")).lower().strip() not in ("yes", "true", "1"):
            return ("Killing a process ends it immediately (unsaved work is "
                    "lost). Confirm with yes, and give pid or name.")
        targets: list[int] = []
        if str(raw).strip().isdigit():
            targets = [int(raw)]
        elif name:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if name in p.info["name"].lower():
                        targets.append(p.info["pid"])
                except Exception:
                    pass
            targets = targets[:8]
        else:
            return "Give a pid or a name to kill."
        banned = _self_pids()
        killed: list[int] = []
        refused: list[str] = []          # always formatted, never a bare pid
        missing: list[int] = []
        for pid in targets:
            if pid in banned or pid <= 0:
                refused.append(f"{pid} (protected)")
                continue
            try:
                p = psutil.Process(pid)
                p.terminate()
                killed.append(pid)
            except psutil.NoSuchProcess:
                missing.append(pid)
            except Exception as e:
                refused.append(f"{pid} ({e})")
        parts = []
        if killed:
            parts.append(f"terminated: {killed}")
        if refused:
            parts.append(f"refused: {refused}")
        if missing:
            parts.append(f"not found: {missing}")
        return "Kill result — " + "; ".join(parts)

    # list / find
    rows, total = [], 0
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_info"]):
        total += 1
        try:
            nm = p.info["name"] or ""
        except Exception:
            continue
        if query and query not in nm.lower():
            continue
        rows.append(p)
    # top by RSS
    def rss(p):
        try:
            return p.memory_info().rss
        except Exception:
            return 0
    rows.sort(key=rss, reverse=True)
    shown = rows[:_MAX_ROWS]
    if not shown:
        return f"No processes match {query!r} (scanned {total})."
    header = f"{'PID':>7}  {'CPU':>5}  {'MEM':>8}  NAME"
    lines = [header] + [_fmt_row(p) for p in shown]
    if len(rows) > len(shown):
        lines.append(f"… {len(rows) - shown.__len__()} more (narrow with query)")
    lines.append(f"({len(rows)} of {total} processes"
                 + (f" matching {query!r}" if query else "") + ")")
    result = "\n".join(lines)
    if player is not None:
        try:
            player.show_content("PROCESSES", result[:4000])
        except Exception:
            pass
    return result


TOOL = {
    "name": "procman",
    "description": (
        "Lists running processes (top by memory, optional query filter) and "
        "kills them with explicit confirmation. Actions: list (default), "
        "kill (pid or name, requires confirm=yes — NEVER pass confirm "
        "without the user saying yes). Use for 'what's running', 'which app "
        "eats RAM', 'close chrome'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | kill"},
            "query": {"type": "STRING", "description": "Filter by process name."},
            "pid": {"type": "STRING", "description": "PID for kill."},
            "name": {"type": "STRING", "description": "Process name for kill."},
            "confirm": {"type": "STRING",
                        "description": "'yes' only after the user agrees."},
        },
        "required": [],
    },
    "handler": procman,
}
