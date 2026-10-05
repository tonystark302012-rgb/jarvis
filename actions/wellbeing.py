"""
wellbeing — where did browsing GO today (visit analytics, Report D3).

Reads Chrome/Edge/Brave/Opera (Chromium `History`) and Firefox
(`places.sqlite`) through a LOCKED COPY — the browser's own file is never
opened, so a running browser can't block or corrupt the read. Visits are
grouped by domain for today / last 7 days, and the top site links into
the existing focus session ("say 'focus start'").

HONEST SCOPE: browsers record VISITS, not dwell time — no browser stores
"minutes on page". Every report says so instead of inventing minutes.
Zero new deps: stdlib sqlite3 + urllib.parse + shutil.

WHY NOT A DUPLICATE: scanner reads processes/network, browser_control
drives a live browser. This is the daily "kitna time diya" report that
neither keeps.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

_CHROME_EPOCH = 11644473600          # seconds between 1601-01-01 and 1970
_DAY = 86400
_WEEK = 7 * _DAY


# ── discovery ───────────────────────────────────────────────────────────────

def _chromium_roots() -> list[Path]:
    import sys
    home = Path.home()
    roots: list[Path] = []
    if sys.platform.startswith("win"):
        local = Path.home() / "AppData" / "Local"
        roam = Path.home() / "AppData" / "Roaming"
        roots += [local / "Google" / "Chrome" / "User Data",
                  local / "Microsoft" / "Edge" / "User Data",
                  local / "BraveSoftware" / "Brave-Browser" / "User Data",
                  local / "Vivaldi" / "User Data",
                  roam / "Opera Software" / "Opera Stable"]
    elif sys.platform == "darwin":
        lib = home / "Library" / "Application Support"
        roots += [lib / "Google" / "Chrome", lib / "Microsoft Edge",
                  lib / "BraveSoftware" / "Brave-Browser", lib / "Vivaldi",
                  lib / "com.operasoft.Opera"]
    else:
        cfg = home / ".config"
        roots += [cfg / "google-chrome", cfg / "chromium",
                  cfg / "microsoft-edge",
                  cfg / "BraveSoftware" / "Brave-Browser",
                  cfg / "vivaldi", cfg / "opera", cfg / "opera-gx"]
    return [r for r in roots if r.exists()]


def _firefox_roots() -> list[Path]:
    import sys
    home = Path.home()
    if sys.platform.startswith("win"):
        base = home / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support" / "Firefox" / "Profiles"
    else:
        base = home / ".mozilla" / "firefox"
    return [base] if base.exists() else []


def _history_dbs() -> list[tuple[str, Path]]:
    """(kind, sqlite file) for every browser profile found on this machine.
    kind: 'chromium' | 'firefox'."""
    out: list[tuple[str, Path]] = []
    for root in _chromium_roots():
        for prof in sorted(root.iterdir()) if root.is_dir() else []:
            if prof.is_dir() and (prof / "History").is_file():
                out.append(("chromium", prof / "History"))
    for base in _firefox_roots():
        for prof in sorted(base.iterdir()) if base.is_dir() else []:
            if prof.is_dir() and (prof / "places.sqlite").is_file():
                out.append(("firefox", prof / "places.sqlite"))
    return out


# ── read (locked copy — never touch the live file) ──────────────────────────

def _copy_locked(src: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-bw-"))
    dst = tmp / src.name
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):        # carry pending writes along
        side = Path(str(src) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(dst) + suffix))
    return dst


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host.split(":")[0]


def _collect(kind: str, db: Path, since_unix: float) -> list[tuple[str, int, int]]:
    """(url, count, last_unix) rows since the cutoff — from a COPY."""
    copy = _copy_locked(db)
    try:
        c = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            if kind == "chromium":
                cutoff = int((since_unix + _CHROME_EPOCH) * 1_000_000)
                rows = c.execute(
                    "SELECT u.url, COUNT(*), MAX(v.visit_time)"
                    " FROM visits v JOIN urls u ON u.id = v.url_id"
                    " WHERE v.visit_time >= ? GROUP BY u.url", (cutoff,)
                ).fetchall()
                return [(u, int(n), int(last) // 1_000_000 - _CHROME_EPOCH)
                        for u, n, last in rows if last is not None]
            cutoff = int(since_unix * 1_000_000)
            rows = c.execute(
                "SELECT p.url, COUNT(*), MAX(f.visit_date)"
                " FROM moz_historyvisits f JOIN moz_places p"
                " ON p.id = f.place_id"
                " WHERE f.visit_date >= ? GROUP BY p.url", (cutoff,)
            ).fetchall()
            return [(u, int(n), int(last) // 1_000_000)
                    for u, n, last in rows if last is not None]
        finally:
            c.close()
    finally:
        shutil.rmtree(copy.parent, ignore_errors=True)


def _report(since_unix: float, label: str, limit: int = 10) -> str:
    agg: dict[str, dict] = {}
    dbs = _history_dbs()
    for kind, db in dbs:
        try:
            rows = _collect(kind, db, since_unix)
        except Exception:
            continue                       # a locked/corrupt profile ≠ fatal
        for url, n, last in rows:
            dom = _domain(url)
            if not dom:
                continue
            slot = agg.setdefault(dom, {"n": 0, "last": 0})
            slot["n"] += n
            slot["last"] = max(slot["last"], last)
    if not dbs:
        return ("No browser history found (Chrome/Edge/Brave/Opera/"
                "Firefox profiles not on this machine) — nothing to report.")
    if not agg:
        return (f"Browser wellbeing ({label}): no visits in this window."
                " Either a quiet streak or history is cleared.")
    ranked = sorted(agg.items(), key=lambda kv: (-kv[1]["n"], kv[0]))
    total = sum(v["n"] for v in agg.values())
    lines = [f"Browser wellbeing ({label}) — {len(agg)} site(s), "
             f"{total} visit(s):"]
    for i, (dom, v) in enumerate(ranked[:limit], 1):
        last = time.strftime("%H:%M", time.localtime(v["last"]))
        lines.append(f"{i}. {dom} — {v['n']} visit(s), last seen {last}")
    lines.append("(Visit counts, not minutes: browsers don't store dwell "
                 "time.)")
    top = ranked[0][0]
    if top and ranked[0][1]["n"] >= 3:
        lines.append(f"Top site today is {top}. Say 'focus start' to begin "
                     "a focus session that mutes proactive alerts.")
    return "\n".join(lines)


# ── tool ────────────────────────────────────────────────────────────────────

def wellbeing(parameters: dict = None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "today")).lower().strip()
    now = time.time()
    if action == "week":
        out = _report(now - _WEEK, "last 7 days")
    elif action == "domains":
        # raw domain list for scripts/proactive rules
        rows: dict[str, int] = {}
        for kind, db in _history_dbs():
            try:
                for url, n, _last in _collect(kind, db, now - _WEEK):
                    dom = _domain(url)
                    if dom:
                        rows[dom] = rows.get(dom, 0) + n
            except Exception:
                continue
        if not rows:
            out = "No browser history found for the last 7 days."
        else:
            top = sorted(rows.items(), key=lambda kv: (-kv[1], kv[0]))[:30]
            out = ("7-day domains:\n" +
                   "\n".join(f"{d}: {n}" for d, n in top))
    else:
        # today = local midnight
        lt = time.localtime(now)
        midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
        out = _report(midnight, "today")

    if player is not None:
        try:
            player.show_content("WELLBEING", out[:4000])
        except Exception:
            pass
    return out


TOOL = {
    "name": "wellbeing",
    "description": (
        "Digital-wellbeing: browser visit analytics read from a locked "
        "COPY of Chrome/Edge/Brave/Opera/Firefox history (the live file "
        "is never touched). Actions: today (default — sites visited "
        "since midnight, ranked), week (last 7 days), domains (top "
        "domains list). Honest about scope: visit counts, not minutes — "
        "browsers don't record dwell time. Top site links to focus: "
        "say 'focus start' to begin a session. Use for 'how much did I "
        "browse today', 'kitni der chalaya', 'site usage report'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "today | week | domains."},
        },
        "required": [],
    },
    "handler": wellbeing,
}
