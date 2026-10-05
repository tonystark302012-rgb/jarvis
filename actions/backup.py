"""
backup — timestamped zip of JARVIS's data, safe to take while the app runs.

Scope (edit _targets() to taste): memory/ (prefs, long-term, history + the
SQLite databases), config/ (api keys, rules, feeds — NOT config/certs, that
TLS identity regenerates itself), macros/ (learned skills), research/ (saved
reports). Regenerated output (charts/, diagrams/, models/) is left out, and
meetings/ recordings are left out because of size.

Databases are copied with SQLite VACUUM INTO — a consistent snapshot even
though the scheduler is writing — falling back to a plain file copy if the
db is read-only or corrupt (the zip still carries whatever bytes exist).

Actions:
  create  (default) → backups/jarvis-backup-YYYYmmdd-HHMMSS.zip
  list              → the zips on disk with size and age
  restore which=<zip|index> → extracts into a NEW folder (never overwrites
                      live data) and prints the path

The archive contains secrets (API keys) — the output says so; store it like
you would a password manager export.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
import zipfile
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _backup_dir() -> Path:
    return _base_dir() / "backups"


def _targets() -> tuple[str, ...]:
    return ("memory", "config", "macros", "research")


def _snapshot_db(db: Path, dest_dir: Path) -> Path:
    """Consistent copy of a live SQLite db → dest_dir/db.name."""
    dest = dest_dir / db.name
    try:
        src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            src.execute("VACUUM INTO ?", (str(dest),))
        finally:
            src.close()
        return dest
    except Exception:
        # Read-only/corrupt source: take the raw bytes + WAL sidecars so at
        # least a recoverable copy exists.
        for suffix in ("", "-wal", "-shm"):
            side = Path(str(db) + suffix)
            if side.exists():
                shutil.copy2(side, dest_dir / side.name)
        return dest


def create(parameters: dict = None) -> str:
    base = _base_dir()
    out_dir = _backup_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"jarvis-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    written = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _targets():
            root = base / name
            if not root.is_dir():
                continue
            with __import__("tempfile").TemporaryDirectory() as td:
                tmp = Path(td)
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    if "certs" in path.parts and path.suffix in (".key", ".crt"):
                        continue          # machine TLS identity — regenerates
                    if path.name.endswith(("-wal", "-shm")):
                        # snapshot via VACUUM INTO already flushed the wal —
                        # a stale sidecar next to it would corrupt restores
                        continue
                    if path.suffix == ".db":
                        stored = _snapshot_db(path, tmp)
                        zf.write(stored, arcname=str(path.relative_to(base)))
                    else:
                        zf.write(path, arcname=str(path.relative_to(base)))
                    written += 1
    kb = zip_path.stat().st_size / 1024
    return (f"Saved: {zip_path} ({kb:.0f} KB, {written} files from "
            f"{', '.join(_targets())}). "
            "NOTE: contains API keys — store it like a password export.")


def list_backups(parameters: dict = None) -> str:
    out_dir = _backup_dir()
    rows = sorted(out_dir.glob("jarvis-backup-*.zip"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not rows:
        return "No backups yet — backup action=create"
    lines = []
    for i, p in enumerate(rows, 1):
        age = time.time() - p.stat().st_mtime
        when = (f"{age / 86400:.1f}d" if age >= 86400
                else f"{age / 3600:.1f}h" if age >= 3600
                else f"{age / 60:.0f}m")
        lines.append(f"#{i} {p.name} — {p.stat().st_size / 1024:.0f} KB, "
                     f"{when} ago")
    return "\n".join(lines)


def restore(parameters: dict = None) -> str:
    p = parameters or {}
    which = str(p.get("which") or "").strip()
    if not which:
        return "restore needs which=<zip name or #index> — backup action=list"
    rows = sorted(_backup_dir().glob("jarvis-backup-*.zip"),
                  key=lambda q: q.stat().st_mtime, reverse=True)
    target: Path | None = None
    w = which.lstrip("#")
    if w.isdigit() and 1 <= int(w) <= len(rows):
        target = rows[int(w) - 1]
    else:
        cand = _backup_dir() / Path(which).name
        if cand.exists():
            target = cand
    if target is None:
        return f"No backup {which!r} — backup action=list"

    dest = _backup_dir() / (f"restore-{time.strftime('%Y%m%d-%H%M%S')}-"
                            + target.stem)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target) as zf:
        zf.extractall(dest)      # zipfile sanitises ../ and absolute members
    n = len([x for x in zf.namelist()])
    return (f"Restored {n} files into {dest} — review it, then copy what you "
            "need over your live folders (nothing was overwritten).")


def backup(parameters: dict = None, player=None, session_memory=None) -> str:
    action = str((parameters or {}).get("action") or "create").strip().lower()
    if action in ("create", "new", "run"):
        return create(parameters)
    if action == "list":
        return list_backups(parameters)
    if action == "restore":
        return restore(parameters)
    return f"Unknown backup action {action!r} — create | list | restore"


TOOL = {
    "name": "backup",
    "description": (
        "Backup/restore JARVIS's data (memory, config+keys, learned macros, "
        "research reports) into a timestamped zip under backups/. SQLite "
        "databases are snapshotted with VACUUM INTO so it is safe while the "
        "app runs. Actions: create (default), list, restore (which=…). "
        "Use when the user says 'backup my data', 'save everything', "
        "'restore the backup'.")
    ,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "create | list | restore."},
            "which": {"type": "STRING",
                      "description": "For restore: zip name or #index from list."},
        },
        "required": [],
    },
    "handler": backup,
}
