# actions/history_search.py
"""history — searchable conversation history.

WHAT IT DOES
    Every conversation turn is appended to sqlite FTS5 the moment it is
    spoken or typed (main.py calls `record()` — the current session log
    only lives in RAM, so this is what makes "last week we discussed X"
    answerable after a restart).

    search  — full-text search across ALL recorded turns, ranked by
              BM25, newest first, with dates.
    recent  — the last N turns (default 10).
    oneshot — append a single turn (used by wiring/tests).

WHY FTS5: built into every sqlite Python ships — no extension to load,
no vector model to download, sub-ms on 100k rows. Free by construction.

WHY NOT A DUPLICATE: `_session_log` is in-memory only and dies at exit;
`save_session_summary` stores one paragraph per session, not turns.
This is the first index over the turns themselves.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_CONN: sqlite3.Connection | None = None


def _db_path() -> Path:
    from config import get_base_dir
    d = get_base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "history.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        c = sqlite3.connect(_db_path(), check_same_thread=False)
        c.execute(
            "CREATE TABLE IF NOT EXISTS turns ("
            " id INTEGER PRIMARY KEY,"
            " ts REAL NOT NULL,"
            " speaker TEXT NOT NULL,"
            " text TEXT NOT NULL)"
        )
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5("
            " text, content='turns', content_rowid='id',"
            " tokenize='unicode61')"
        )
        # keep FTS in sync with the base table (content-table pattern)
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN"
            " INSERT INTO turns_fts(rowid, text) VALUES (new.id, new.text);"
            " END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN"
            " INSERT INTO turns_fts(turns_fts, rowid, text)"
            " VALUES('delete', old.id, old.text);"
            " END"
        )
        c.commit()
        _CONN = c
    return _CONN


def record(speaker: str, text: str) -> None:
    """Append one turn. Never raises — history must not break a reply."""
    text = (text or "").strip()
    if not text:
        return
    # cap: a turn is a turn, not a document
    if len(text) > 4000:
        text = text[:4000] + " …"
    try:
        with _LOCK:
            _conn().execute(
                "INSERT INTO turns (ts, speaker, text) VALUES (?, ?, ?)",
                (time.time(), str(speaker or "user")[:24], text),
            )
            _conn().commit()
    except Exception as e:
        print(f"[History] record failed: {e}")


def recent_user_turns(since_ts: float, limit: int = 500) -> list:
    """(ts, text) of user turns at/after since_ts, newest first.
    For the rules suggest miner — local only, never raises."""
    try:
        with _LOCK:
            rows = _conn().execute(
                "SELECT ts, text FROM turns WHERE ts >= ? AND speaker ="
                " 'user' ORDER BY ts DESC LIMIT ?",
                (float(since_ts), max(1, min(2000, int(limit))))).fetchall()
        return [(float(r[0]), str(r[1] or "")) for r in rows]
    except Exception:
        return []


def _fmt_row(row) -> str:
    ts, speaker, text = row
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    return f"[{when}] {speaker}: {text}"


def _escape_fts(query: str) -> str:
    """Free text → safe FTS5 MATCH: quote each token so that user input
    like 'C++ OR (x)' can't be FTS syntax; join with OR for recall
    (BM25 still ranks multi-term hits first)."""
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:12])


def _recency_weight(rel: float, age_days: float) -> float:
    """Relevance × freshness. rel is SQLite bm25 (more negative = better);
    decay maps age → (0.5, 1.0] so recency at most doubles a score — a
    strong old answer still beats a weak fresh one, but of two equals the
    newer one wins. Future timestamps clamp to age 0."""
    import math
    age = max(0.0, float(age_days))
    return float(rel) * (0.5 + 0.5 * math.exp(-age / 30.0))


def search(query: str, limit: int = 10) -> str:
    match = _escape_fts(query)
    if not match:
        return "Give me something to search for in our conversations."
    limit = max(1, min(50, int(limit)))
    import time as _time
    now = _time.time()
    with _LOCK:
        try:
            # candidate pool > limit so the re-rank can promote/demote
            rows = _conn().execute(
                "SELECT t.ts, t.speaker, t.text,"
                " bm25(turns_fts) AS rel FROM turns t"
                " JOIN turns_fts f ON f.rowid = t.id"
                " WHERE turns_fts MATCH ?"
                " ORDER BY rel ASC LIMIT ?",
                (match, min(200, limit * 10)),
            ).fetchall()
        except sqlite3.OperationalError as e:
            return f"History search failed: {e}"
        except Exception as e:
            return f"History search failed: {e}"
    ranked = sorted(
        rows,
        key=lambda r: (_recency_weight(r[3], (now - r[0]) / 86400.0),
                       -r[0]))[:limit]
    rows = [(r[0], r[1], r[2]) for r in ranked]
    if not rows:
        return f"No conversation turns match {query!r}."
    head = f"Found {len(rows)} turn(s) for {query!r}:"
    return head + "\n" + "\n".join(f"{i + 1}. {_fmt_row(r)}"
                                   for i, r in enumerate(rows))


def recent(limit: int = 10) -> str:
    with _LOCK:
        rows = _conn().execute(
            "SELECT ts, speaker, text FROM turns"
            " ORDER BY ts DESC LIMIT ?",
            (max(1, min(100, int(limit or 10))),),
        ).fetchall()
    if not rows:
        return "No conversation history recorded yet."
    rows.reverse()
    return f"Last {len(rows)} turn(s):\n" + "\n".join(_fmt_row(r) for r in rows)


def stats() -> str:
    with _LOCK:
        n, oldest, newest = _conn().execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM turns").fetchone()
    if not n:
        return "History is empty."
    span = time.strftime("%Y-%m-%d", time.localtime(oldest))
    end = time.strftime("%Y-%m-%d", time.localtime(newest))
    return f"History: {n} turns from {span} to {end}."


def history(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "search").lower().strip()
    if action == "recent":
        return recent(params.get("limit", 10))
    if action == "stats":
        return stats()
    if action == "record":                     # explicit wiring/testing hook
        record(str(params.get("speaker") or "user"),
               str(params.get("text") or ""))
        return "Recorded."
    q = str(params.get("query") or params.get("text") or "").strip()
    if not q:
        # bare 'history' → recent, friendlier than an error
        return recent(params.get("limit", 10))
    return search(q, params.get("limit", 10))


TOOL = {
    "name": "history",
    "description": (
        "Searchable conversation history (local sqlite FTS5, survives "
        "restarts). Actions: search (default — pass `query`), recent "
        "(last turns, `limit`), stats. Use for 'what did we discuss last "
        "week', 'when did I ask about X', 'pichhle hafte kya hua tha' — "
        "anything about PAST conversations. For what the user SAID about "
        "themselves, prefer recall_memory."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "search | recent | stats"},
            "query": {"type": "STRING", "description": "Full-text query"},
            "limit": {"type": "INTEGER", "description": "Max results — default 10"},
        },
        "required": [],
    },
    "handler": history,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return history(params,
                   player=(ctx or {}).get("player"),
                   session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(history({"action": "stats"}))
