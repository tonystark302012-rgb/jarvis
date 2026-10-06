# core/audit_chain.py
"""Tamper-evident audit log — append-only sqlite rows + SHA-256 hash chain.

Every tool call that crosses ActionRegistry.run appends one row:
    (id, ts, tool, params, status, detail, prev_hash, row_hash)
where row_hash = sha256(prev_hash | ts | tool | params | status | detail).
Editing or deleting any historic row breaks the chain from that point on —
`verify()` names the first broken id. That is the whole guarantee:
append-only + chained hashes, no external service, no signing keys.

Secrets never land in the log: parameter values are redacted by KEY
(api_key/token/password/…) and by VALUE (ghp_…, sk-…, AIza…, and every
string stored in config/api_keys.json).

Wired at the single dispatch choke point (ActionRegistry.run) so model
tool-calls, orchestrator steps, rule firings and palette runs are all
covered without each caller remembering to log.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None

_GENESIS = "0" * 64
_REDACT_KEY = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|credential|"
    r"authorization|auth_?header|bearer|private[_-]?key|session[_-]?id)",
    re.IGNORECASE)
_VALUE_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),          # GitHub tokens
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),              # OpenAI/Groq-style
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),             # Google API keys
    re.compile(r"ya29\.[A-Za-z0-9_\-]{16,}"),           # Google OAuth
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),       # Slack
    re.compile(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),  # JWTs
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.=/+]{12,}"),
]
_cfg_cache: tuple[float, set[str]] = (-1.0, set())


def _db_path() -> Path:
    from config import get_base_dir
    p = get_base_dir() / "memory"
    p.mkdir(parents=True, exist_ok=True)
    return p / "audit.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        c = sqlite3.connect(str(_db_path()), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE IF NOT EXISTS audit_log ("
            " id INTEGER PRIMARY KEY,"
            " ts REAL NOT NULL,"
            " tool TEXT NOT NULL,"
            " params TEXT NOT NULL,"
            " status TEXT NOT NULL,"
            " detail TEXT NOT NULL DEFAULT '',"
            " prev_hash TEXT NOT NULL,"
            " row_hash TEXT NOT NULL)")
        c.commit()
        _CONN = c
    return _CONN


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


# ── redaction ───────────────────────────────────────────────────────────────

def _config_secrets() -> set[str]:
    """String values ≥8 chars from config/api_keys.json (mtime-cached)."""
    global _cfg_cache
    try:
        from config import get_base_dir
        p = get_base_dir() / "config" / "api_keys.json"
        mt = p.stat().st_mtime
        if _cfg_cache[0] == mt:
            return _cfg_cache[1]
        data = json.loads(p.read_text(encoding="utf-8"))
        vals = {str(v).strip() for v in data.values()
                if isinstance(v, str) and len(v.strip()) >= 8}
        _cfg_cache = (mt, vals)
        return vals
    except Exception:
        return _cfg_cache[1]


def _mask(text: str) -> str:
    for v in _config_secrets():
        if v and v in text:
            text = text.replace(v, "[redacted]")
    for rx in _VALUE_PATTERNS:
        text = rx.sub("[redacted]", text)
    return text


def redact(obj) -> str:
    """JSON form of any parameter object with secrets removed (by key AND
    by value) — the only shape that ever reaches the disk."""
    def _clean(o):
        if isinstance(o, dict):
            return {k: ("[redacted]" if _REDACT_KEY.search(str(k)) else _clean(v))
                    for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [_clean(x) for x in o]
        if isinstance(o, str):
            return _mask(o)
        return o
    try:
        return _mask(json.dumps(_clean(obj or {}), ensure_ascii=False,
                                default=str))
    except Exception:
        return "[unserialisable]"


# ── chain ───────────────────────────────────────────────────────────────────

def _row_hash(prev: str, ts: float, tool: str, params: str,
              status: str, detail: str) -> str:
    raw = f"{prev}|{ts:.6f}|{tool}|{params}|{status}|{detail}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record(tool: str, parameters, status: str = "ok",
           detail: str = "") -> None:
    """Append one audited tool call. NEVER raises — auditing must not be
    able to break the call it is auditing."""
    try:
        params = redact(parameters)
        detail = _mask(str(detail or ""))[:400]
        status = str(status or "ok")[:40]
        tool = str(tool or "?")[:80]
        ts = time.time()
        with _LOCK:
            c = _conn()
            prev_row = c.execute(
                "SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev = prev_row[0] if prev_row else _GENESIS
            h = _row_hash(prev, ts, tool, params, status, detail)
            c.execute(
                "INSERT INTO audit_log (ts, tool, params, status, detail,"
                " prev_hash, row_hash) VALUES (?,?,?,?,?,?,?)",
                (ts, tool, params, status, detail, prev, h))
            c.commit()
    except Exception:
        pass


def verify() -> str:
    """Walk the whole chain. First divergence → honest report with id."""
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT id, ts, tool, params, status, detail, prev_hash,"
            " row_hash FROM audit_log ORDER BY id").fetchall()
    prev = _GENESIS
    for r in rows:
        if r["prev_hash"] != prev:
            return (f"Audit chain BROKEN before #{r['id']}"
                    f" ({r['tool']}) — prev_hash does not link.")
        expect = _row_hash(prev, r["ts"], r["tool"], r["params"],
                           r["status"], r["detail"])
        if expect != r["row_hash"]:
            return (f"Audit chain BROKEN at #{r['id']} ({r['tool']}) — "
                    "row content does not match its hash (edited).")
        prev = r["row_hash"]
    return f"Audit chain OK — {len(rows)} entrie(s), genesis → tip linked."


def recent(limit: int = 15) -> str:
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT id, ts, tool, params, status, detail FROM audit_log"
            " ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),)
        ).fetchall()
    if not rows:
        return "Audit log is empty — no tool calls recorded yet."
    lines = []
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
        one = r["params"].replace("\n", " ")
        if len(one) > 110:
            one = one[:107] + "…"
        det = f" — {r['detail'][:60]}" if r["detail"] else ""
        lines.append(f"#{r['id']} [{when}] {r['tool']} ({r['status']}) "
                     f"{one}{det}")
    return (f"Audit log (last {len(rows)}):\n" + "\n".join(lines))


def audit_log(parameters: dict | None = None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "recent")).lower().strip()
    if action == "verify":
        return verify()
    try:
        n = int(params.get("limit", 15))
    except (TypeError, ValueError):
        n = 15
    return recent(n)


TOOL = {
    "name": "audit_log",
    "description": (
        "Tamper-evident audit of every tool call (append-only sqlite + "
        "SHA-256 hash chain, secrets redacted before writing). Actions: "
        "recent (default, pass `limit`) — what ran and when; verify — "
        "re-hash the chain and report the first broken entry if anyone "
        "edited history. Use for 'what did you run', 'show audit', "
        "'has the log been tampered'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "recent | verify."},
            "limit": {"type": "STRING", "description": "Rows for recent."},
        },
        "required": [],
    },
    "handler": audit_log,
}
