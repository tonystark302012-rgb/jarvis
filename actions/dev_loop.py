"""
dev_loop — background test loop for a project (Report #3 E).

Edit → save → the loop notices the file changed → runs the test
command → PASS/FAIL lands in a log you can read from `status` (and the
model can tail). This is the missing inner development loop: dev_agent
scaffolds and fixes ONCE; dev_loop keeps verifying while you type.

  start(path, cmd?)  spawn the watcher (cmd auto-detected: pytest for
                     python projects, `npm test` for package.json)
  run                one-shot run without watching
  stop               kill the watcher
  status             state + last log lines (honest about everything)

Safety: the command runs via subprocess in the project dir (same trust
model as dev_agent/code_helper), skips venv/node_modules/.git noise,
and a crashed watcher reports its last error instead of vanishing.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

_STATE: dict[str, Any] = {
    "running": False, "path": "", "cmd": "", "runs": 0, "fails": 0,
    "last_run": 0.0, "last_status": "", "error": "", "thread": None,
    "stop": None,
}
_POLL = 0.8
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__",
              ".mypy_cache", ".pytest_cache", "dist", "build", ".tox",
              ".ruff_cache", "site-packages"}
_WATCH_EXT = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".c",
              ".cpp", ".h", ".java"}


# ── seams (tests replace these) ────────────────────────────────────────────

def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _run_cmd(cmd: str, cwd: str, timeout: int = 600) -> tuple[int, str]:
    """Run the test command → (rc, tail). Seam for tests."""
    proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)
    tail = ((proc.stdout or "") + (proc.stderr or ""))[-2000:]
    return int(proc.returncode), tail


def _log_path(root: Path) -> Path:
    return Path(root) / ".jarvis_testloop.log"


# ── pure helpers ────────────────────────────────────────────────────────────

def _default_cmd(root: Path) -> str:
    """Auto-detect the test command (pure — tested). Empty = none."""
    root = Path(root)
    if (root / "tests").is_dir() or (root / "pytest.ini").is_file() or \
            (root / "conftest.py").is_file():
        return f"{sys.executable} -m pytest -q"
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            import json
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if "test" in (data.get("scripts") or {}):
                return "npm test"
        except Exception:
            pass
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            txt = pyproject.read_text(encoding="utf-8", errors="replace")
            if "[tool.pytest" in txt:
                return f"{sys.executable} -m pytest -q"
        except Exception:
            pass
    return ""


def _snapshot(root: Path) -> dict:
    """{relpath: (size, digest)} for watched files (noise skipped).

    Content digest, NOT mtime: some filesystems quantise mtime to ~8ms,
    so two rapid saves in one tick would otherwise look identical."""
    import hashlib
    out: dict = {}
    root = Path(root)
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _WATCH_EXT:
            continue
        try:
            st = p.stat()
            if st.st_size <= 2_000_000:
                digest = hashlib.md5(p.read_bytes()).hexdigest()[:12]
            else:
                digest = f"mt:{st.st_mtime_ns}"      # huge files: mtime only
            out[str(p.relative_to(root))] = (st.st_size, digest)
        except Exception:
            continue
    return out


# ── loop core (testable without threads) ───────────────────────────────────

_seen: dict = {}


def _log(root: Path, line: str) -> None:
    try:
        with open(_log_path(root), "a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except Exception:
        pass


def _tick(path: str, cmd: str, *, initial: bool = False) -> str:
    """One poll: run cmd when files changed (or initial baseline).
    Returns 'ran' | 'clean'. Never raises."""
    root = Path(path)
    try:
        snap = _snapshot(root)
    except Exception as e:
        _STATE["error"] = f"scan failed: {e}"[:160]
        return "clean"
    changed = [f for f, m in snap.items() if _seen.get(f) != m]
    _seen.clear()
    _seen.update(snap)
    if not changed and not initial:
        return "clean"
    try:
        rc, tail = _run_cmd(cmd, str(root))
    except Exception as e:
        _STATE["error"] = f"run failed: {e}"[:160]
        _log(root, f"[{_now()}] ERROR {e}")
        return "clean"
    _STATE["runs"] += 1
    _STATE["last_run"] = time.time()
    ok = rc == 0
    _STATE["last_status"] = "PASS" if ok else f"FAIL rc={rc}"
    if not ok:
        _STATE["fails"] += 1
    when = _now()
    what = ", ".join(changed[:4]) + ("…" if len(changed) > 4 else "")
    _log(root, f"[{when}] {_STATE['last_status']} ({what})")
    if not ok and tail:
        _log(root, tail[-800:])
    _STATE["error"] = ""
    return "ran"


def _now() -> str:
    return time.strftime("%H:%M:%S")


def _loop(path: str, cmd: str) -> None:
    stop: threading.Event = _STATE.get("stop") or threading.Event()
    _tick(path, cmd, initial=True)
    while not stop.is_set():
        time.sleep(_POLL)
        if stop.is_set():
            break
        try:
            _tick(path, cmd)
        except Exception as e:                       # belt + braces
            _STATE["error"] = str(e)[:160]
    _STATE["running"] = False


# ── tool ────────────────────────────────────────────────────────────────────

def dev_loop(parameters: dict | None = None, player=None,
             session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()
    path = str(params.get("path") or _STATE.get("path") or "").strip()
    cmd = str(params.get("cmd") or "").strip()

    if action == "start":
        if not path:
            return ("Give me the project — dev_loop action=start "
                    "path=/path/to/project [cmd='pytest -q'].")
        root = Path(path).expanduser()
        if not root.is_dir():
            return f"No such directory: {root}"
        if _STATE.get("running"):
            return (f"Test loop already running for {_STATE['path']} "
                    f"(cmd: {_STATE['cmd']}) — dev_loop action=stop first.")
        if not cmd:
            cmd = _default_cmd(root)
        if not cmd:
            return ("No test command detected (pytest tests/, pytest.ini, "
                    "pyproject [tool.pytest] or package.json test script) "
                    "— pass cmd=… explicitly.")
        _STATE.update({"running": True, "path": str(root), "cmd": cmd,
                       "runs": 0, "fails": 0, "last_status": "",
                       "error": ""})
        _seen.clear()
        stop = threading.Event()
        _STATE["stop"] = stop
        th = threading.Thread(target=_loop, args=(str(root), cmd),
                              daemon=True, name="jarvis-dev-loop")
        _STATE["thread"] = th
        th.start()
        return (f"Test loop STARTED on {root} — cmd: {cmd}. It runs once "
                "now, then re-runs on every save. Check "
                f"dev_loop action=status (log: {_log_path(root)}).")

    if action == "run":
        if not path:
            return "run needs path=…"
        root = Path(path).expanduser()
        if not root.is_dir():
            return f"No such directory: {root}"
        if not cmd:
            cmd = _default_cmd(root)
        if not cmd:
            return "No test command detected — pass cmd=…"
        _STATE.update({"path": str(root), "cmd": cmd})
        _tick(str(root), cmd, initial=True)
        return f"One-shot: {(_STATE['last_status'] or 'no output')} — " \
               f"dev_loop action=status for the log tail."

    if action == "stop":
        if not _STATE.get("running"):
            return "Test loop was not running."
        stopper = _STATE.get("stop")
        if stopper is not None:
            stopper.set()
        _STATE["running"] = False
        return (f"Test loop STOPPED — {_STATE['runs']} run(s), "
                f"{_STATE['fails']} fail(s).")

    # status
    if not _STATE.get("path"):
        return "No dev loop yet — dev_loop action=start path=…"
    lines = [f"Dev loop: {'RUNNING' if _STATE.get('running') else 'stopped'}"
             f" on {_STATE['path']} (cmd: {_STATE.get('cmd', '')}) — "
             f"{_STATE['runs']} run(s), {_STATE['fails']} fail(s), "
             f"last: {_STATE.get('last_status') or '—'}."]
    if _STATE.get("error"):
        lines.append(f"⚠ {_STATE['error']}")
    log = _log_path(Path(_STATE["path"]))
    try:
        tail = log.read_text(encoding="utf-8").splitlines()[-12:]
        if tail:
            lines.append("Log tail:")
            lines += ["  " + t[:160] for t in tail]
    except Exception:
        pass
    return "\n".join(lines)


TOOL = {
    "name": "dev_loop",
    "description": (
        "Background test loop for a project: watches source files and "
        "re-runs the test command on every save (auto-detects pytest / "
        "npm test; cmd= overrides). actions: start (path, optional "
        "cmd), run (one-shot), stop, status (state + log tail from "
        ".jarvis_testloop.log). Use for 'watch my tests', 'keep running "
        "the test loop', 'dev mode'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "start | run | stop | status."},
            "path": {"type": "STRING",
                     "description": "Project directory."},
            "cmd": {"type": "STRING",
                    "description": "Test command override."},
        },
        "required": [],
    },
    "handler": dev_loop,
}
