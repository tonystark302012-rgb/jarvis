# actions/data_query.py
"""data — SQL over your data files, offline (DuckDB).

WHAT IT DOES
    Runs one SQL statement against a CSV / Parquet / JSON file — or a
    glob of them — and returns a formatted table. DuckDB scans the whole
    dataset in-process: no server, no cloud, no pandas-to-Python rows.

        data query="SELECT city, AVG(price) FROM 'rentals/*.csv'
                    GROUP BY city ORDER BY 2 DESC LIMIT 10"

    describe — schema + row count of a file (no SQL needed).
    summarize — DuckDB's own column statistics (count/distinct/null/min/
                max/avg/quantiles) for the whole file, no SQL needed.

    Auto-chart: a SELECT that comes back with a label column + a numeric
    column ALSO saves an SVG bar chart (actions/charts.py) and prints the
    path — 'average rent by city' gives the table AND the picture.

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


def _maybe_chart(cols: list[str], rows: list, title: str) -> str:
    """Bar-chart the first (label, numeric) column pair a result offers.
    Honest about when it can't: returns '' instead of a junk SVG."""
    if not cols or len(rows) < 3 or len(cols) < 2:
        return ""
    # first column whose values are short, unique-ish strings = labels
    label_i = None
    for i in range(len(cols) - 1):
        vals = [r[i] for r in rows]
        if all(v is not None and len(str(v)) <= 40 for v in vals) and \
                len({str(v) for v in vals}) >= max(2, len(rows) // 2):
            label_i = i
            break
    if label_i is None:
        return ""
    num_i = None
    for j in range(len(cols)):
        if j == label_i:
            continue
        ok = True
        for r in rows:
            try:
                float(r[j])
            except (TypeError, ValueError):
                ok = False
                break
        if ok and any(r[j] is not None for r in rows):
            num_i = j
            break
    if num_i is None:
        return ""
    pairs = []
    for r in rows[:30]:
        try:
            pairs.append([str(r[label_i])[:28], float(r[num_i])])
        except (TypeError, ValueError):
            return ""
    try:
        from actions.charts import chart
        return chart({"kind": "bar", "data": pairs,
                      "title": f"{title}: {cols[num_i]} by {cols[label_i]}"})
    except Exception:                               # charting is a bonus
        return ""


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
        if action == "summarize":
            if not files:
                return "Which file? Pass file=… (summarize needs one)"
            target = files[0]
            low = target.lower()
            reader = ("read_csv_auto" if low.endswith(".csv") else
                      "read_parquet" if low.endswith((".parquet", ".pq"))
                      else "read_json_auto")
            qpath = target.replace("'", "''")
            cur = con.execute(
                f"SUMMARIZE SELECT * FROM {reader}('{qpath}')")
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchmany(_MAX_ROWS)
            return (f"{Path(target).name} — column summary "
                    f"(DuckDB SUMMARIZE):\n" + _fmt_table(cols, rows))

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
        out = _fmt_table(cols, rows)
        note = _maybe_chart(cols, rows,
                            Path(files[0]).name if files else "query")
        if note:
            out += "\n" + note
        return out
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
        "row count without SQL; action=summarize shows DuckDB column "
        "statistics (min/max/avg/nulls/quantiles) without SQL. SELECT "
        "results with a label+numeric column pair also save an SVG bar "
        "chart and print its path. The first file is also available as the "
        "view `data`. Use for 'CSV pe SQL chalao', 'average rent by "
        "city', 'is data mein kya hai', 'give me column stats'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file": {"type": "STRING",
                     "description": "Path/glob of CSV, Parquet or JSON"},
            "query": {"type": "STRING", "description": "SQL to run"},
            "action": {"type": "STRING",
                       "description": "describe | summarize (optional)"},
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
