# core/metrics_store.py
"""metrics_store — append-only system metric samples (Report L time-series).

The system-monitor loop in main.py already ticks every 10 s for threshold
alerts; each tick now also appends a sample here (cpu / ram / temp / gpu),
so the dashboard question "kal isi waqt se 3x zyada?" becomes answerable
from data instead of vibes.

Small + honest: sqlite WAL, 26 h retention (covers yesterday's same hour),
bucket-averaged series for charts, and a same-hour-yesterday anomaly
ratio. Never raises — a metrics write must never break the alert loop.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_RETENTION_S = 26 * 3600

# metric name → table column (same names; sample() maps the richer
# get_system_status keys onto these)
METRICS = {"cpu": "cpu", "ram": "ram", "temp": "temp", "gpu": "gpu"}
_COLS = ("cpu", "ram", "temp", "gpu")


def _db_path() -> Path:
    from config import get_base_dir
    d = get_base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "metrics.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        c = sqlite3.connect(str(_db_path()), check_same_thread=False)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            " ts REAL NOT NULL,"
            " cpu REAL, ram REAL, temp REAL, gpu REAL)")
        c.execute(
            "CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts)")
        c.commit()
        _CONN = c
    return _CONN


def reset_for_tests() -> None:
    global _CONN
    with _LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except Exception:
                pass
            _CONN = None


def sample(status: dict | None) -> None:
    """Append one snapshot (keys from get_system_status). Never raises."""
    try:
        status = status or {}
        row = tuple(
            (float(status[k]) if status.get(k) is not None else None)
            for k in ("cpu_percent", "ram_percent", "cpu_temp_c",
                      "gpu_percent"))
        with _LOCK:
            c = _conn()
            c.execute(
                "INSERT INTO samples (ts, cpu, ram, temp, gpu)"
                " VALUES (?,?,?,?,?)", (time.time(), *row))
            if int(time.time()) % 17 == 0:          # cheap periodic prune
                c.execute("DELETE FROM samples WHERE ts < ?",
                          (time.time() - _RETENTION_S,))
            c.commit()
    except Exception:
        pass


def count(hours: float = 24) -> int:
    with _LOCK:
        return _conn().execute(
            "SELECT COUNT(*) FROM samples WHERE ts >= ?",
            (time.time() - hours * 3600,)).fetchone()[0]


def series(metric: str, hours: float = 1.0, buckets: int = 30):
    """Bucket-averaged (label, value) pairs for charts. NULLs skipped."""
    col = METRICS.get(metric, metric)
    if col not in _COLS:
        return []
    now = time.time()
    start = now - hours * 3600
    with _LOCK:
        rows = _conn().execute(
            f"SELECT ts, {col} FROM samples WHERE ts >= ? AND {col}"
            " IS NOT NULL ORDER BY ts", (start,)).fetchall()
    if not rows:
        return []
    buckets = max(2, min(240, buckets))
    width = (hours * 3600) / buckets
    acc: dict[int, list[float]] = {}
    for ts, val in rows:
        i = min(buckets - 1, int((ts - start) / width))
        acc.setdefault(i, []).append(float(val))
    out = []
    for i in sorted(acc):
        vals = acc[i]
        label = time.strftime("%H:%M", time.localtime(start + i * width))
        out.append((label, round(sum(vals) / len(vals), 1)))
    return out


def stats(metric: str, hours: float = 1.0) -> dict | None:
    """{avg, min, max, n} over the window — NULLs excluded."""
    col = METRICS.get(metric, metric)
    if col not in _COLS:
        return None
    with _LOCK:
        row = _conn().execute(
            f"SELECT AVG({col}), MIN({col}), MAX({col}), COUNT(*)"
            " FROM samples WHERE ts >= ? AND " + col + " IS NOT NULL",
            (time.time() - hours * 3600,)).fetchone()
    if not row or row[3] == 0:
        return None
    return {"avg": round(row[0], 1), "min": round(row[1], 1),
            "max": round(row[2], 1), "n": row[3]}


def anomaly(metric: str = "cpu") -> str:
    """Same-hour-yesterday comparison. Needs ≥18 h of data; returns ''
    when the comparison is not meaningful (never invents a spike)."""
    col = METRICS.get(metric, metric)
    if col not in _COLS:
        return ""
    now = time.time()
    cur_hour_start = now - 3600
    yst_hour_start = now - 24 * 3600 - 3600
    with _LOCK:
        c = _conn()
        cur = c.execute(
            f"SELECT AVG({col}), COUNT(*) FROM samples WHERE ts >= ?"
            f" AND {col} IS NOT NULL", (cur_hour_start,)).fetchone()
        yst = c.execute(
            f"SELECT AVG({col}), COUNT(*) FROM samples WHERE ts >= ?"
            f" AND ts < ? AND {col} IS NOT NULL",
            (yst_hour_start, now - 24 * 3600)).fetchone()
        total = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    if total < 50 or not cur or not yst or cur[1] < 2 or yst[1] < 2:
        return ""
    if yst[0] <= 0:
        return ""
    ratio = cur[0] / yst[0]
    if ratio >= 2.0:
        return (f"{metric} now {cur[0]:.0f} vs {yst[0]:.0f} at the same "
                f"time yesterday — {ratio:.1f}x higher.")
    if ratio <= 0.5:
        return (f"{metric} now {cur[0]:.0f} vs {yst[0]:.0f} at the same "
                f"time yesterday — {1 / ratio:.1f}x lower.")
    return ""
