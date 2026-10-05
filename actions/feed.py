"""
feed — RSS/Atom subscriptions + GitHub release watching (all free, no login).

Registry lives in config/feeds.json as [{"name", "url"}]. A GitHub repo is
watched through its public releases feed:

    https://github.com/<owner>/<repo>/releases.atom

Actions:
  list    (default) — show the registry
  add     name=… url=…   — add or update a subscription
  remove  which=<name|index>
  check   which=<name|index|all> limit=5 — newest entries right now

Parsing uses feedparser (in requirements.txt); when it is missing the action
says so instead of silently returning nothing.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


def _feeds_path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "feeds.json"


def _get(url: str, timeout: float = 20.0) -> bytes:
    """Fetch seam — tests monkeypatch this instead of hitting the network."""
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _load() -> list[dict]:
    try:
        rows = json.loads(_feeds_path().read_text(encoding="utf-8"))
        return [r for r in rows if isinstance(r, dict) and r.get("url")]
    except Exception:
        return []


def _save(rows: list[dict]) -> None:
    p = _feeds_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    tmp.replace(p)                                # atomic — no torn registry


def _pick(rows: list[dict], which: str) -> list[dict]:
    """which: '' / 'all' → every row; #3 or 3 → by index; else name match."""
    w = (which or "").strip()
    if not w or w.lower() == "all":
        return rows
    if w.startswith("#"):
        w = w[1:]
    if w.isdigit():
        i = int(w) - 1
        return [rows[i]] if 0 <= i < len(rows) else []
    return [r for r in rows if str(r.get("name", "")).lower() == w.lower()]


def _entry_line(name: str, e) -> str:
    title = str(getattr(e, "title", "") or "(untitled)").strip()
    if len(title) > 90:
        title = title[:87] + "…"
    when = str(getattr(e, "published", "") or getattr(e, "updated", "")
               or "").strip()
    link = str(getattr(e, "link", "") or "").strip()
    parts = [f"  • {title}"]
    if when:
        parts.append(when)
    if link:
        parts.append(link)
    return " — ".join(parts)


def feed(parameters: dict = None, player=None, session_memory=None) -> str:
    try:
        import feedparser
    except Exception:
        return ("feed parser missing — pip install feedparser (it is in "
                "requirements.txt).")
    p = parameters or {}
    action = str(p.get("action") or "list").strip().lower() or "list"
    rows = _load()

    if action == "list":
        if not rows:
            return ("No feeds yet — feed action=add name=jarvis "
                    "url=https://github.com/tonystark302012-rgb/jarvis/"
                    "releases.atom")
        return "\n".join(
            f"#{i + 1} {r.get('name', '?')}: {r['url']}"
            for i, r in enumerate(rows))

    if action == "add":
        name = str(p.get("name") or "").strip()
        url = str(p.get("url") or "").strip()
        if not name or not url:
            return "feed action=add needs name=… and url=…"
        if not url.startswith(("http://", "https://")):
            return f"Feed URL must be http(s): {url!r}"
        rows = [r for r in rows if str(r.get("name", "")).lower()
                != name.lower()]
        rows.append({"name": name, "url": url})
        _save(rows)
        return f"Subscribed: {name} → {url} (feed action=check to read)"

    if action == "remove":
        which = str(p.get("which") or p.get("name") or "").strip()
        hits = _pick(rows, which)
        if not hits:
            return f"No feed {which!r} — feed action=list"
        keep = [r for r in rows if r not in hits]
        _save(keep)
        return ("Removed: " + ", ".join(str(r.get("name", "?"))
                                        for r in hits))

    if action == "check":
        if not rows:
            return "No feeds to check — add one first (feed action=add)."
        targets = _pick(rows, str(p.get("which") or ""))
        if not targets:
            return f"No feed {p.get('which')!r} — feed action=list"
        try:
            limit = max(1, min(20, int(p.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        out: list[str] = []
        for r in targets:
            name = str(r.get("name", "?"))
            try:
                data = _get(r["url"])
            except Exception as e:               # noqa: BLE001
                out.append(f"{name}: unreachable ({type(e).__name__}: {e})")
                continue
            parsed = feedparser.parse(data)
            entries = list(getattr(parsed, "entries", []) or [])
            if not entries:
                why = "feed has no entries"
                if getattr(parsed, "bozo", False):
                    why = f"parse error: {str(parsed.bozo_exception)[:120]}"
                out.append(f"{name}: {why}")
                continue
            out.append(f"{name}: {len(entries)} shown of "
                       f"{len(entries)}+ entries")
            out.extend(_entry_line(name, e) for e in entries[:limit])
        return "\n".join(out)

    return (f"Unknown feed action {action!r} — list | add | remove | check")


TOOL = {
    "name": "feed",
    "description": (
        "RSS/Atom feed subscriptions and GitHub release watching (free, "
        "unauthenticated). Actions: list, add (name+url), remove (which), "
        "check (which?, limit?) reads the newest entries now. Use when the "
        "user says 'any updates from …', 'watch this blog', 'new release of "
        "…'.")
    ,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | add | remove | check."},
            "name": {"type": "STRING",
                     "description": "Subscription name (add/remove target)."},
            "url": {"type": "STRING",
                    "description": "Feed URL (http/https), e.g. "
                                   "https://github.com/OWNER/REPO/releases.atom."},
            "which": {"type": "STRING",
                      "description": "Feed to check/remove: name, #index, or all."},
            "limit": {"type": "INTEGER",
                      "description": "Entries per feed on check (1-20, default 5)."},
        },
        "required": [],
    },
    "handler": feed,
}
