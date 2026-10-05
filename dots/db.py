# dots/db.py
"""One sqlite file for the whole platform: memory/dots.db (gitignored —
it holds the owner's documents). Migrate-on-open (CREATE IF NOT EXISTS),
WAL, busy_timeout; a module lock serialises writers like taskstore does.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dots (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  role_instructions TEXT NOT NULL DEFAULT '',
  permissions_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS spaces (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
  id INTEGER PRIMARY KEY,
  space_id INTEGER NOT NULL,
  parent_id INTEGER,
  title TEXT NOT NULL DEFAULT '',
  content_json TEXT NOT NULL DEFAULT '[]',
  content_md TEXT NOT NULL DEFAULT '',
  rev INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL DEFAULT 'owner',
  sources_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pages_space ON pages(space_id);
CREATE TABLE IF NOT EXISTS page_revisions (
  id INTEGER PRIMARY KEY,
  page_id INTEGER NOT NULL,
  rev INTEGER NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  content_json TEXT NOT NULL DEFAULT '[]',
  content_md TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL DEFAULT 'owner',
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rev_page ON page_revisions(page_id, rev);
CREATE TABLE IF NOT EXISTS pending_changes (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                -- 'create' | 'edit'
  page_id INTEGER,                   -- NULL for create
  space_id INTEGER,
  parent_id INTEGER,
  dot_id INTEGER NOT NULL,
  base_rev INTEGER NOT NULL DEFAULT 0,
  title TEXT NOT NULL DEFAULT '',
  content_json TEXT NOT NULL DEFAULT '[]',
  content_md TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  sources_json TEXT,
  created_at REAL NOT NULL,
  decided_at REAL
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_changes(status);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  dot_id INTEGER NOT NULL,
  page_id INTEGER,
  status TEXT NOT NULL DEFAULT 'active',
  provider TEXT NOT NULL DEFAULT 'none',
  started_at REAL NOT NULL,
  ended_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  convo_key TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_convo ON messages(convo_key, id);
CREATE TABLE IF NOT EXISTS preferences (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  value TEXT NOT NULL,
  allowed_json TEXT NOT NULL DEFAULT '"*"',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS computers (
  id INTEGER PRIMARY KEY,
  dot_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'stopped',
  root_path TEXT NOT NULL,
  perms_json TEXT NOT NULL DEFAULT '{"browser":false,"files":true,"shell":false}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS computer_audit (
  id INTEGER PRIMARY KEY,
  computer_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  ok INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_comp ON computer_audit(computer_id, id);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  instruction TEXT NOT NULL,
  every_seconds INTEGER NOT NULL,
  dot_id INTEGER,                -- nullable: NULL = main-brain task
  status TEXT NOT NULL DEFAULT 'active',
  next_run_at REAL NOT NULL,
  last_run_at REAL,
  created_at REAL NOT NULL,
  schedule_kind TEXT NOT NULL DEFAULT 'every',  -- every | cron | at
  cron_expr TEXT,                               -- valid when kind='cron'
  run_at REAL                                   -- epoch when kind='at'
);
CREATE TABLE IF NOT EXISTS task_runs (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  status TEXT NOT NULL DEFAULT 'running',
  output TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skills (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  body_md TEXT NOT NULL DEFAULT '',
  source_note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft',
  created_at REAL NOT NULL,
  published_at REAL
);
CREATE TABLE IF NOT EXISTS slack_threads (
  convo_key TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  thread_ts TEXT NOT NULL,
  updated_at REAL NOT NULL
);
"""


def _db_path() -> Path:
    from config import get_base_dir
    d = get_base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "dots.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        c = sqlite3.connect(_db_path(), check_same_thread=False,
                            timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(_SCHEMA)
        _migrate(c)
        c.commit()
        _CONN = c
    return _CONN


def _migrate(c: sqlite3.Connection) -> None:
    """Column migrations for DBs created before this batch.
    CREATE TABLE IF NOT EXISTS never alters an existing table, so
    sources_json (research links, batch 6b) lands via guarded ALTER, and the
    tasks.dot_id nullability change (main-brain tasks) lands via a guarded
    table rebuild — SQLite cannot ALTER nullability in place."""
    for ddl in (
        "ALTER TABLE pages ADD COLUMN sources_json TEXT",
        "ALTER TABLE pending_changes ADD COLUMN sources_json TEXT",
        # cron/at scheduling (guarded — additive, defaults keep old rows)
        "ALTER TABLE tasks ADD COLUMN schedule_kind TEXT"
        " NOT NULL DEFAULT 'every'",
        "ALTER TABLE tasks ADD COLUMN cron_expr TEXT",
        "ALTER TABLE tasks ADD COLUMN run_at REAL",
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass  # already exists

    try:
        info = {r[1]: r for r in c.execute("PRAGMA table_info(tasks)")}
    except sqlite3.Error:
        info = {}
    if info.get("dot_id") and info["dot_id"][3]:     # notnull flag = 1 (old)
        # NOTE: runs AFTER the additive ALTERs above, so the schedule_*
        # columns already exist on the old table — carry them through, or a
        # fully-old DB would lose them the moment the rebuild drops it.
        cols = ["id", "name", "instruction", "every_seconds", "dot_id",
                "status", "next_run_at", "last_run_at", "created_at",
                "schedule_kind", "cron_expr", "run_at"]
        c.execute(
            "CREATE TABLE tasks_rebuild ("
            " id INTEGER PRIMARY KEY,"
            " name TEXT NOT NULL,"
            " instruction TEXT NOT NULL,"
            " every_seconds INTEGER NOT NULL,"
            " dot_id INTEGER,"
            " status TEXT NOT NULL DEFAULT 'active',"
            " next_run_at REAL NOT NULL,"
            " last_run_at REAL,"
            " created_at REAL NOT NULL,"
            " schedule_kind TEXT NOT NULL DEFAULT 'every',"
            " cron_expr TEXT,"
            " run_at REAL)")
        c.execute("INSERT INTO tasks_rebuild (" + ",".join(cols) + ")"
                  " SELECT " + ",".join(cols) + " FROM tasks")
        c.execute("DROP TABLE tasks")
        c.execute("ALTER TABLE tasks_rebuild RENAME TO tasks")


def reset_for_tests() -> None:
    """Drop the cached connection (tests re-point _db_path first)."""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
        _CONN = None
