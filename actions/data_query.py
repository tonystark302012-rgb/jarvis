# actions/data_query.py
"""data — SQL over your data files, offline (DuckDB).

WHAT IT DOES
    Runs one SQL statement against a CSV / Parquet / JSON file — or a
    glob of them — and returns a formatted table. DuckDB scans the whole
    dataset in-process: no server, no cloud, no pandas-to-Python rows.

        data query="SELECT city, AVG(price) FROM 'rentals/*.csv'
                    GROUP BY city ORDER BY 2 DESC LIMIT 10"

    describe — schema + row count of a file (no SQL needed).

FREE: duckdb is MIT-licensed and a single wheel. This is the ₹0 SQL
engine from the roadmap (CSV/Parquet pe SQL — poore dataset seconds mein).

SAFETY: DuckDB is read-only by default for file access; the statement
itself is SQL, not shell — no injection surface beyond the DB. LIMIT is
auto-appended when the user didn't cap results so a SELECT * on a big
file can't flood the reply (500 rows max).
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import duckdb as _duckdb
    _OK = True
except ImportError:                                    # pragma: no cover
    _duckdb = None
    _OK = False

_MAX_ROWS = 500


def _expand(raw: str) -> list[str]:
    """Resolve user paths/globs into existing files (relative → base dir)."""
    from config import get_base_dir
    base = get_base_dir()
    out: list[str] = []
    for part in re.split(r"[;,]", raw or ""):
        part = part.strip().strip("'\"")
        if not part:
            continue
        p = Path(part).expanduser()
        if not p.is_absolute():
            p = base / p
        if p.is_file():
            out.append(str(p))
            continue
        # glob (covers ** patterns the user typed)
        try:
            matches = sorted(str(m) for m in base.glob(part)
                             if m.is_file()) if not Path(part).is_absolute() \
                else sorted(str(m) for m in Path("/").glob(part.lstrip("/"))
                            if m.is_file())
            out.extend(matches[:50])
        except Exception:
            pass
    return out


def _fmt_table(columns: list[str], rows: list[tuple]) -> str:
    if not rows:
        return "(0 rows)"
    widths = [len(str(c)) for c in columns]
    str_rows = []
    for r in rows:
        cells = ["" if v is None else str(v) for v in r]
        str_rows.append(cells)
        for i, v in enumerate(cells):
            if i < len(widths):
                widths[i] = min(60, max(widths[i], len(v)))
    def line(ch="-"):
        return "+" + "+".join(ch * (w + 2) for w in widths) + "+"
    out = [line(), "| " + " | ".join(str(c).ljust(w)
                                      for c, w in zip(columns, widths)) + " |",
           line()]
    for cells in str_rows:
        out.append("| " + " | ".join(
            (v[:w]).ljust(w) for v, w in zip(cells, widths)) + " |")
    out.append(line())
    out.append(f"({len(rows)} row(s))")
    return "\n".join(out)


def data_query(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    if not _OK:
        return ("DuckDB isn't installed — pip install duckdb (one MIT "
                "wheel, fully offline once installed).")
    source = str(params.get("file") or params.get("path") or "").strip()
    action = str(params.get("action") or "").lower().strip()
    sql = str(params.get("query") or params.get("sql") or "").strip()

    if not source and not sql:
        return ("Give me a file and a query — e.g. data file=rent.csv "
                "query=\"SELECT * FROM data LIMIT 5\".")

    files = _expand(source) if source else []
    if source and not files:
        return f"No files match {source!r}."

    try:
        con = _duckdb.connect(":memory:")
    except Exception as e:
        return f"Couldn't start DuckDB: {e}"

    try:
        if action == "describe" or (files and not sql):
            if not files:
                return "Which file? Pass file=…"
            target = files[0]
            low = target.lower()
            reader = ("read_csv_auto" if low.endswith(".csv") else
                      "read_parquet" if low.endswith((".parquet", ".pq"))
                      else "read_json_auto")
            # views/DESCRIBE can't take prepared params — quote the path
            qpath = target.replace("'", "''")
            rows = con.execute(
                f"SELECT column_name, column_type FROM "
                f"(DESCRIBE SELECT * FROM {reader}('{qpath}'))").fetchall()
            cnt = con.execute(
                f"SELECT COUNT(*) FROM {reader}('{qpath}')").fetchone()[0]
            return (f"{Path(target).name}: {cnt} row(s)\n" +
                    _fmt_table(["column", "type"], rows))

        # regular query
        if sql:
            lowered = sql.rstrip().rstrip(";").lower()
            capped = bool(re.search(r"\blimit\s+\d+\s*$", lowered)) or \
                bool(re.search(r"\blimit\s+\d+\s*;\s*$", sql.lower()))
            if not capped and lowered.startswith(("select", "with", "describe",
                                                  "show", "summarize")):
                sql = sql.rstrip().rstrip(";") + f" LIMIT {_MAX_ROWS}"
        if not sql:
            return "No query given."
        if files:
            # views can't bind parameters — inline escaped paths
            for i, f in enumerate(files):
                alias = "data" if i == 0 else f"f{i}"
                low = f.lower()
                reader = ("read_csv_auto" if low.endswith(".csv") else
                          "read_parquet" if low.endswith((".parquet", ".pq"))
                          else "read_json_auto")
                qpath = f.replace("'", "''")
                con.execute(f"CREATE VIEW {alias} AS "
                            f"SELECT * FROM {reader}('{qpath}')")
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(_MAX_ROWS)
        return _fmt_table(cols, rows)
    except Exception as e:
        return f"Query failed: {e}"
    finally:
        try:
            con.close()
        except Exception:
            pass


TOOL = {
    "name": "data",
    "description": (
        "Run SQL over the user's data files with DuckDB (offline, free). "
        "Pass file= (a path, glob, or several separated by ;) and query= "
        "(SELECT/CTE; LIMIT auto-applied). action=describe shows schema + "
        "row count without SQL. The first file is also available as the "
        "view `data`. Use for 'CSV pe SQL chalao', 'average rent by "
        "city', 'this parquet file mein kya hai'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file": {"type": "STRING",
                     "description": "Path/glob of CSV, Parquet or JSON"},
            "query": {"type": "STRING", "description": "SQL to run"},
            "action": {"type": "STRING", "description": "describe (optional)"},
        },
        "required": [],
    },
    "handler": data_query,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return data_query(params,
                      player=(ctx or {}).get("player"),
                      session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(data_query({}))
