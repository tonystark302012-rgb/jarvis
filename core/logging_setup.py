# core/logging_setup.py
"""Logging — a real log file, and a way to hand it to someone.

WHAT WAS WRONG
    The project had no stdlib logging at all: zero `import logging`, zero
    `getLogger`, and roughly 430 `print()` calls. Emoji progress lines read
    nicely in a terminal, but they are not a diagnostic: no levels, no
    timestamps, no file, nothing to attach to a bug report, and no way to see
    what happened five minutes ago on a machine you are not sitting at.

WHAT THIS DOES
    1. `setup()` configures one rotating log file and, optionally, the console.
    2. It also MIRRORS `print()` into that file. That is the part worth
       arguing about, and the argument is this: there are ~430 prints and they
       are the app's console UX, not debug leftovers. Rewriting all of them
       into logger calls would change what the user sees, to no benefit — but
       leaving them unrecognised means the log file starts life empty and stays
       useless. Mirroring gets a complete transcript today, and the migration
       to `log.info()` can then happen print-by-print, at leisure, without
       anybody losing information in the meantime.
    3. `export_diagnostics()` zips the log with a redacted config, for the
       "it broke, here is what happened" case.

    Nothing calls `setup()` at import time. A library that reconfigures logging
    when you import it is a nuisance, and the test suite imports most of this
    package.
"""
from __future__ import annotations

import io
import logging
import os
import platform
import re
import sys
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: One file, 5 MB × 3 — enough history for "it broke yesterday" without a
#: runaway process filling a disk.
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 3
_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False
_log_dir: Path | None = None
_active_log: Path | None = None      # what setup() actually configured
_tee: "_Tee | None" = None

#: Anything whose NAME looks like this is redacted in a diagnostics bundle.
_SECRET_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|pin|credential|"
    r"client[_-]?secret|private[_-]?key)", re.I)

_REDACTED = "<redacted>"


def log_dir() -> Path:
    """Where logs live. `JARVIS_LOG_DIR` overrides; default is ~/.jarvis/logs.

    Not inside the app folder on purpose: a frozen build may be read-only, and
    a checkout should not collect runtime noise that has to be git-ignored.
    """
    override = os.environ.get("JARVIS_LOG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jarvis" / "logs"


def log_path() -> Path:
    """The path a log WOULD go to, given the current environment."""
    return log_dir() / "jarvis.log"


def active_log_path() -> Path:
    """The file logging is actually going to.

    `log_path()` answers "where would a log go", which is the wrong question
    once `setup(directory=…)` has chosen somewhere else — both `mirror_prints`
    and `export_diagnostics` asked it anyway and quietly used the default.
    Tests found both; this is the one place that remembers.
    """
    return _active_log or log_path()


def get_logger(name: str) -> logging.Logger:
    """Namespaced logger. `get_logger(__name__)` in a module gives
    `core.taskstore` in the file, which is what makes a log readable."""
    return logging.getLogger(name if name.startswith("jarvis") else f"jarvis.{name}")


# ── the print tee ───────────────────────────────────────────────────────────

class _Tee(io.TextIOBase):
    """Writes through to the real stream AND to the log file.

    Terminal semantics are forwarded (`isatty`, `encoding`, `fileno`) so a
    progress-bar library or a colour check still sees a terminal and keeps
    behaving. A failure to write to the file is swallowed: logging must never
    be the reason the app dies.
    """

    def __init__(self, stream, log: Path):
        self._stream = stream
        self._log = log

    def write(self, data):                       # type: ignore[override]
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            with open(self._log, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(data)
        except Exception:
            pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return bool(getattr(self._stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._stream, "errors", "replace")


def mirror_prints(on: bool = True, target: Path | None = None) -> bool:
    """Start/stop mirroring `print()` into the log file. Idempotent.

    `target` defaults to the configured log; `setup()` passes the file it just
    configured so a custom directory is honoured here too (it was not, and the
    mirror quietly wrote to the default location).

    Returns whether mirroring is active afterwards. `JARVIS_LOG_MIRROR=0`
    disables it for anyone who would rather have a log of logger calls only.
    """
    global _tee
    if os.environ.get("JARVIS_LOG_MIRROR", "1").strip() in ("0", "false", "no"):
        on = False
    if on:
        if _tee is not None:
            return True
        try:
            dest = target or active_log_path()
            dest.parent.mkdir(parents=True, exist_ok=True)
            _tee = _Tee(sys.stdout, dest)
            sys.stdout = _tee
            return True
        except Exception:
            _tee = None
            return False
    if _tee is not None:
        if sys.stdout is _tee:
            sys.stdout = _tee._stream
        _tee = None
    return False


# ── setup ───────────────────────────────────────────────────────────────────

def setup(level: int | str = logging.INFO, *,
          console: bool | None = None,
          mirror: bool | None = None,
          directory: Path | None = None) -> Path:
    """Configure logging once. Returns the log file path.

    `console` defaults to on when stderr is a terminal — so a user running
    `python main.py` sees warnings in the console, while a GUI launch (no
    terminal) does not spew into a window that is not reading it.

    Repeated calls are safe and do not stack handlers; that matters because
    the app reconnects, and because a test may call it to inspect behaviour.
    """
    global _configured, _log_dir, _active_log
    if directory is not None:
        _log_dir = Path(directory)
    target = (_log_dir or log_dir()) / "jarvis.log"
    _active_log = target
    target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    root.setLevel(level)

    # Mark our handlers so a second call replaces rather than duplicates them
    # (and so we never remove a handler a host application installed).
    for h in list(root.handlers):
        if getattr(h, "_jarvis_handler", False):
            root.removeHandler(h)

    fh = RotatingFileHandler(target, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                             encoding="utf-8", delay=True)
    fh.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    fh._jarvis_handler = True                      # type: ignore[attr-defined]
    root.addHandler(fh)

    if console is None:
        console = bool(getattr(sys.stderr, "isatty", lambda: False)())
    if console and not any(getattr(h, "_jarvis_console", False)
                           for h in root.handlers):
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter(_FMT, _DATEFMT))
        ch._jarvis_console = True                  # type: ignore[attr-defined]
        root.addHandler(ch)

    if mirror is None:
        mirror = True
    mirror_prints(False)              # re-point the tee at the new file
    mirror_prints(mirror, target)

    _configured = True
    get_logger("setup").info("%s on %s %s · log → %s",
                             _version(), platform.system(),
                             platform.release(), target)
    return target


def _version() -> str:
    try:
        from core.version import version_string
        return version_string()
    except Exception:
        return "JARVIS (version unknown)"


def is_configured() -> bool:
    return _configured


def install_excepthook() -> None:
    """Uncaught exceptions reach the log before the interpreter dies.

    The default behaviour prints to stderr, which on a GUI launch goes
    nowhere. This keeps the print AND records it with a traceback.
    """
    previous = sys.excepthook

    def _hook(kind, value, tb):
        try:
            get_logger("crash").critical("uncaught %s", kind.__name__,
                                         exc_info=(kind, value, tb))
        except Exception:
            pass
        previous(kind, value, tb)

    sys.excepthook = _hook


# ── diagnostics ─────────────────────────────────────────────────────────────

def redact(value, key: str = ""):
    """Recursively replace anything that looks like a credential.

    Used on the config before it goes into a bundle. A diagnostics zip is
    something users email to strangers, so the default has to be safe rather
    than convenient.
    """
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    if key and _SECRET_RE.search(key):
        return _REDACTED if value not in ("", None, False) else value
    if isinstance(value, str) and _SECRET_RE.search(key or ""):
        return _REDACTED
    return value


def diagnostics_summary() -> str:
    """Human-readable one-screen state — what a bug report should start with."""
    lines = [_version()]
    lines.append(f"python   : {platform.python_version()} "
                 f"({sys.implementation.name})")
    lines.append(f"platform : {platform.system()} {platform.release()} "
                 f"{platform.machine()}")
    lines.append(f"log      : {active_log_path()}")
    try:
        from core import paths
        lines.append(f"app root : {paths.base_dir()}")
        cfg = paths.read_config()
        lines.append(f"config   : {len(cfg)} key(s) — "
                     f"{', '.join(sorted(cfg)) or 'none'}")
    except Exception as e:
        lines.append(f"config   : unavailable ({e})")
    try:
        from core import audit_chain
        lines.append(f"audit    : {audit_chain.verify()}")
    except Exception as e:
        lines.append(f"audit    : unavailable ({e})")
    return "\n".join(lines)


def export_diagnostics(dest: Path | None = None) -> Path:
    """Zip the log + a redacted config + a state summary. Returns the path.

    Never raises for a missing file — a diagnostics bundle that fails because
    there was nothing to report is worse than useless.
    """
    import json
    import time

    dest = Path(dest) if dest else Path.cwd() / (
        time.strftime("jarvis-diagnostics-%Y%m%d-%H%M%S.zip"))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        summary = diagnostics_summary()
        z.writestr("summary.txt", summary)
        path = active_log_path()
        if path.exists():
            # Keep the tail: a 5 MB log is not something anyone reads.
            text = path.read_text(encoding="utf-8", errors="replace")
            z.writestr("jarvis.log", text[-400_000:])
        try:
            from core import paths
            cfg = redact(paths.read_config())
            z.writestr("config.redacted.json",
                       json.dumps(cfg, indent=2, ensure_ascii=False))
        except Exception as e:
            z.writestr("config.redacted.json", f'{{"error": "{e}"}}')
        try:
            from core import audit_chain
            z.writestr("audit.txt", audit_chain.recent(50))
        except Exception as e:
            z.writestr("audit.txt", f"unavailable: {e}")
    return dest
