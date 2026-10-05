"""
graph — Graphiti-lite knowledge graph over memory (Report R2 §4).

Flat key-value memory can answer "what is X" but not "what connects to
X". This adds a small temporal knowledge graph:

  * entities  — distinct named things (people, orgs, places, tools)
  * edges     — typed relations with VALIDITY WINDOWS (valid_from /
                valid_to NULL = still true) — a temporal graph, not a
                scratch dump
  * extract() — free, offline verb-cue extraction ("Priya works_with
                Rahul") so facts land in the graph WITHOUT an LLM call;
                heuristic recall, honest about it
  * search()  — multi-hop BFS from query-matched entities, returning a
                connected subgraph with dates ("how connected are A→B")

Storage: sqlite at <base>/memory/graph.db (`_db_path` is the only
seam — tests point it at tmp files). Zero network, zero API.

WHY NOT A DUPLICATE: memory_manager is flat category→key→value entries
with no edges, no validity, no traversal. This is the graph the flat
store cannot express.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

# relation cues — kept small and unambiguous on purpose (heuristic tool)
_CUES: list[tuple[str, str]] = [
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+works[\s_]?with\s+"
     r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "works_with"),
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+(?:is\s+)?the\s+"
     r"(?:founder|ceo|cto|lead|manager|owner)\s+of\s+"
     r"([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "founded"),
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+lives?\s+in\s+"
     r"([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "lives_in"),
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+works\s+(?:at|for)\s+"
     r"([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "works_at"),
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+married\s+to\s+"
     r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "married_to"),
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+manages\s+"
     r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "manages"),
    (r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})\s+uses\s+"
     r"(\b[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,2})", "uses"),
]


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _db_path() -> Path:
    d = _base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "graph.db"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS entities ("
        " id INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL COLLATE NOCASE UNIQUE,"
        " kind TEXT NOT NULL DEFAULT 'thing',"
        " created_at REAL NOT NULL)")
    c.execute(
        "CREATE TABLE IF NOT EXISTS edges ("
        " id INTEGER PRIMARY KEY,"
        " src INTEGER NOT NULL REFERENCES entities(id),"
        " dst INTEGER NOT NULL REFERENCES entities(id),"
        " rel TEXT NOT NULL,"
        " weight REAL NOT NULL DEFAULT 1.0,"
        " valid_from REAL NOT NULL,"
        " valid_to REAL,"
        " note TEXT NOT NULL DEFAULT '',"
        " created_at REAL NOT NULL)")
    c.execute("CREATE INDEX IF NOT EXISTS edges_src ON edges(src)")
    c.execute("CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst)")
    return c


# ── seams for tests / time control ─────────────────────────────────────────

def _now() -> float:
    return time.time()


def _guess_kind(name: str) -> str:
    n = name.strip()
    if n.lower() in ("jaipur", "delhi", "mumbai", "india", "usa") or \
            n.lower().endswith(("pur", "stan")):
        return "place"
    if n[:1].isupper() and any(w in n for w in ("Labs", "Inc", "Ltd",
                                                "Corp", "Org", "HQ",
                                                "Technologies")):
        return "org"
    return "person" if n[:1].isupper() else "thing"


# ── core ops ────────────────────────────────────────────────────────────────

def upsert_entity(name: str, kind: str = "") -> int:
    name = str(name or "").strip().strip(".,;:")
    if not name:
        raise ValueError("empty entity name")
    c = _conn()
    try:
        row = c.execute("SELECT id FROM entities WHERE name = ?",
                        (name,)).fetchone()
        if row:
            c.commit()
            return int(row["id"])
        cur = c.execute(
            "INSERT INTO entities (name, kind, created_at) VALUES (?,?,?)",
            (name, kind or _guess_kind(name), _now()))
        c.commit()
        return int(cur.lastrowid)
    finally:
        c.close()


def link(a: str, b: str, rel: str, note: str = "",
         at: float | None = None) -> dict:
    """Create (or re-affirm) an ACTIVE edge a —rel→ b at time `at`."""
    rel = str(rel or "").strip().lower().replace(" ", "_")
    if not rel:
        raise ValueError("empty relation")
    if str(a).strip().lower() == str(b).strip().lower():
        raise ValueError("self-relation refused")
    src, dst = upsert_entity(a), upsert_entity(b)
    ts = at if at is not None else _now()
    c = _conn()
    try:
        # an identical active edge → bump weight instead of duplicating
        row = c.execute(
            "SELECT id, weight FROM edges WHERE src=? AND dst=? AND rel=?"
            " AND valid_to IS NULL", (src, dst, rel)).fetchone()
        if row:
            c.execute("UPDATE edges SET weight = weight + 1.0,"
                      " note = ? WHERE id = ?",
                      (str(note) or "", int(row["id"])))
            c.commit()
            eid = int(row["id"])
            mode = "affirmed"
        else:
            cur = c.execute(
                "INSERT INTO edges (src, dst, rel, weight, valid_from,"
                " note, created_at) VALUES (?,?,?,1.0,?,?,?)",
                (src, dst, rel, ts, str(note) or "", ts))
            c.commit()
            eid = int(cur.lastrowid)
            mode = "linked"
        return {"id": eid, "mode": mode, "a": str(a).strip(),
                "b": str(b).strip(), "rel": rel, "at": ts}
    finally:
        c.close()


def close(a: str, b: str, rel: str, at: float | None = None) -> dict:
    """End an active edge — temporal validity: facts STOP being true."""
    rel = str(rel or "").strip().lower().replace(" ", "_")
    ts = at if at is not None else _now()
    c = _conn()
    try:
        row = c.execute(
            "SELECT e.id FROM edges e"
            " JOIN entities s ON s.id = e.src"
            " JOIN entities d ON d.id = e.dst"
            " WHERE s.name = ? COLLATE NOCASE AND d.name = ? COLLATE NOCASE"
            " AND e.rel = ? AND e.valid_to IS NULL",
            (str(a).strip(), str(b).strip(), rel)).fetchone()
        if not row:
            raise KeyError(f"no active {rel!r} edge {a!r}→{b!r}")
        c.execute("UPDATE edges SET valid_to = ? WHERE id = ?",
                  (ts, int(row["id"])))
        c.commit()
        return {"closed": int(row["id"]), "at": ts}
    finally:
        c.close()


def edge_valid_at(edge: dict | sqlite3.Row, ts: float) -> bool:
    vf = float(edge["valid_from"])
    vt = edge["valid_to"]
    return vf <= ts and (vt is None or ts < float(vt))


# ── extraction (free, offline) ──────────────────────────────────────────────

def extract(text: str) -> list[dict]:
    """Verb-cue relation extraction — heuristic recall (≈15–40% of a
    well-formed sentence), ZERO API. Returns the links created."""
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for pattern, rel in _CUES:
        for m in re.finditer(pattern, str(text or "")):
            a, b = m.group(1).strip(), m.group(2).strip()
            if not a or not b or a.lower() == b.lower():
                continue
            key = (a.lower(), b.lower(), rel)
            if key in seen:
                continue
            seen.add(key)
            try:
                out.append(link(a, b, rel, note="extracted"))
            except ValueError:
                continue
    return out


# ── traversal search ────────────────────────────────────────────────────────

def search(query: str, hops: int = 2, limit: int = 12) -> str:
    """Multi-hop BFS over ACTIVE edges from query-matched entities →
    an 'A —rel→ B (since DATE)' text subgraph. Honest empty state."""
    words = [w for w in re.findall(r"[A-Za-z0-9']{2,}", str(query or ""))
             if w]
    if not words:
        return "Give me a name to look up in the graph."
    c = _conn()
    try:
        seeds: list[int] = []
        for w in words:
            rows = c.execute(
                "SELECT id FROM entities WHERE name LIKE ? LIMIT 5",
                (f"%{w}%",)).fetchall()
            seeds += [int(r["id"]) for r in rows]
        seeds = list(dict.fromkeys(seeds))
        if not seeds:
            return (f"No graph entities match {query!r}. Feed me facts "
                    "first (graph action=remember …).")
        seen_entities = set(seeds)
        seen_edges: set[int] = set()
        frontier = list(seeds)
        lines: list[str] = []
        for _ in range(max(1, hops)):
            nxt: list[int] = []
            for eid in frontier:
                rows = c.execute(
                    "SELECT e.*, s.name AS sn, d.name AS dn FROM edges e"
                    " JOIN entities s ON s.id = e.src"
                    " JOIN entities d ON d.id = e.dst"
                    " WHERE e.valid_to IS NULL AND (e.src = ? OR e.dst = ?)"
                    " ORDER BY e.weight DESC LIMIT 8", (eid, eid)).fetchall()
                for r in rows:
                    if int(r["id"]) in seen_edges:
                        continue
                    seen_edges.add(int(r["id"]))
                    since = time.strftime("%Y-%m-%d",
                                          time.localtime(r["valid_from"]))
                    lines.append(f"{r['sn']} —{r['rel']}→ {r['dn']} "
                                 f"(since {since})")
                    other = int(r["dst"] if r["src"] == eid else r["src"])
                    if other not in seen_entities:
                        seen_entities.add(other)
                        nxt.append(other)
            frontier = nxt[:20]
            if len(lines) >= limit:
                break
        if not lines:
            return (f"Entity match for {query!r} exists but has no active "
                    "relations yet.")
        head = f"Connected subgraph for {query!r} ({len(lines)} active " \
               f"relation(s), {hops} hop(s)):\n"
        return head + "\n".join(f"• {l}" for l in lines[:limit])
    finally:
        c.close()


def timeline(name: str) -> str:
    """ALL edges touching `name` with validity windows — past AND
    present (the temporal bit plain memory cannot do)."""
    c = _conn()
    try:
        rows = c.execute(
            "SELECT e.*, s.name AS sn, d.name AS dn FROM edges e"
            " JOIN entities s ON s.id = e.src"
            " JOIN entities d ON d.id = e.dst"
            " WHERE s.name = ? COLLATE NOCASE OR d.name = ? COLLATE NOCASE"
            " ORDER BY e.valid_from", (str(name).strip(),
                                       str(name).strip())).fetchall()
        if not rows:
            return f"No relations for {name!r} in the graph."
        out = [f"Timeline for {name}:"]
        for r in rows:
            frm = time.strftime("%Y-%m-%d", time.localtime(r["valid_from"]))
            to = ("now" if r["valid_to"] is None else
                  time.strftime("%Y-%m-%d", time.localtime(r["valid_to"])))
            out.append(f"• {r['sn']} —{r['rel']}→ {r['dn']}: {frm} → {to}")
        return "\n".join(out)
    finally:
        c.close()


# ── tool ────────────────────────────────────────────────────────────────────

def graph(parameters: dict = None, player=None,
          session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "search")).lower().strip()
    try:
        if action in ("remember", "link"):
            text = str(params.get("text", "") or "").strip()
            if text and not (params.get("a") or params.get("b")):
                made = extract(text)
                if made:
                    return ("Extracted + linked: " + "; ".join(
                        f"{m['a']} —{m['rel']}→ {m['b']}"
                        for m in made))
                return (f"No relation cues found in {text!r} — try e.g. "
                        "'Priya works_with Rahul' or pass a/b/rel.")
            return str(link(str(params.get("a", "")),
                            str(params.get("b", "")),
                            str(params.get("rel", "")),
                            note=str(params.get("note", ""))))
        if action == "close":
            return str(close(str(params.get("a", "")),
                             str(params.get("b", "")),
                             str(params.get("rel", ""))))
        if action == "timeline":
            return timeline(str(params.get("query", "") or
                                params.get("name", "")))
        return search(str(params.get("query", "")))
    except (ValueError, KeyError) as e:
        return f"graph: {e}"
    except Exception as e:
        return f"graph unavailable ({e.__class__.__name__}: {e})"


TOOL = {
    "name": "graph",
    "description": (
        "Graphiti-lite temporal knowledge graph: remember typed "
        "relations (action=remember with free-text 'X works_with Y' "
        "extraction, or explicit a/b/rel), search multi-hop connections "
        "(action=search query=…), close stale relations (action=close), "
        "or show a validity timeline (action=timeline). Use for 'how is "
        "A connected to B', 'who does A work with', relationship "
        "questions memory KV cannot answer. All local sqlite, no API."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "search | remember | close | "
                                      "timeline."},
            "query": {"type": "STRING",
                      "description": "Lookup text (search/timeline)."},
            "text": {"type": "STRING",
                     "description": "Free-text fact for extraction "
                                    "(remember)."},
            "a": {"type": "STRING", "description": "Source entity."},
            "b": {"type": "STRING", "description": "Target entity."},
            "rel": {"type": "STRING", "description": "Relation type."},
            "note": {"type": "STRING", "description": "Optional note."},
        },
        "required": [],
    },
    "handler": graph,
}
