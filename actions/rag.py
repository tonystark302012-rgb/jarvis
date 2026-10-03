# actions/rag.py
"""rag — ask questions of YOUR files, fully offline.

WHAT IT DOES
    1. index   — walk a folder (or one file): text, markdown, code, CSV
                 are read directly; PDF/DOCX go through the stdlib-free
                 path file_processor already owns (best-effort — unreadable
                 files are listed, not fatal).
    2. ask     — FTS5 retrieval over the chunks (BM25), then an extractive
                 answer: the top passages quoted with file:line context.
                 With a Gemini key AND privacy mode off, the passages are
                 handed to the model for a synthesized answer (still only
                 the passages leave — not the whole 90-page PDF).
    3. status  — what's indexed.

WHY FTS5 FIRST: 90-page agreements, contracts, notes — BM25 over chunks
answers "notice period kya hai" in milliseconds with zero model calls.
The model, when present, only rewrites the already-found evidence.

NO NEW DEPS: sqlite FTS5 (built-in) + the existing file_processor text
extraction. This is the ₹0 offline RAG from the list.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_CONN: sqlite3.Connection | None = None
_MAX_CHUNK = 1200          # chars per chunk
_OVERLAP = 150             # overlap so answers aren't cut mid-sentence
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", "uploads"}
_TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".log",
             ".rst", ".cfg", ".ini", ".yml", ".yaml", ".toml", ".html",
             ".css", ".sh", ".sql", ".tex"}
_DOC_EXT = {".pdf", ".docx", ".pptx", ".xlsx"}      # routed to file_processor


def _db_path() -> Path:
    from config import get_base_dir
    d = get_base_dir() / "memory"
    d.mkdir(parents=True, exist_ok=True)
    return d / "rag.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        c = sqlite3.connect(_db_path(), check_same_thread=False)
        c.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            " id INTEGER PRIMARY KEY,"
            " path TEXT NOT NULL,"
            " seq INTEGER NOT NULL,"
            " text TEXT NOT NULL,"
            " UNIQUE(path, seq))"
        )
        c.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
            " text, content='chunks', content_rowid='id',"
            " tokenize='unicode61')"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks"
            " BEGIN INSERT INTO chunks_fts(rowid, text)"
            " VALUES (new.id, new.text); END"
        )
        c.execute(
            "CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks"
            " BEGIN INSERT INTO chunks_fts(chunks_fts, rowid, text)"
            " VALUES('delete', old.id, old.text); END"
        )
        # wiping a re-indexed path must purge FTS too — do it in SQL
        c.commit()
        _CONN = c
    return _CONN


def _chunk(text: str) -> list[str]:
    """Sentence-ish chunks with overlap; never lose the tail."""
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []
    if len(text) <= _MAX_CHUNK:
        return [text]
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + _MAX_CHUNK)
        if end < len(text):
            # prefer breaking after a sentence/paragraph
            window = text[start:end]
            cut = max(window.rfind(". "), window.rfind("\n"))
            if cut > _MAX_CHUNK // 2:
                end = start + cut + 1
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - _OVERLAP, start + 1)
    return [c for c in chunks if c]


def _read_text(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in _TEXT_EXT:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
    if ext in _DOC_EXT:
        # reuse file_processor's extractor — it already handles PDF/DOCX
        try:
            from actions import file_processor as fp
            out = fp.file_processor({"action": "extract_text", "path": str(path)})
            if isinstance(out, str) and not out.startswith(
                    ("Couldn", "Failed", "Error", "No ", "not installed")):
                return out
        except Exception:
            return None
    return None


def _purge_path(c: sqlite3.Connection, path: str) -> None:
    ids = [r[0] for r in c.execute(
        "SELECT id FROM chunks WHERE path = ?", (path,))]
    if ids:
        c.executemany("DELETE FROM chunks WHERE id = ?",
                      [(i,) for i in ids])
        # FTS triggers fire per delete (content pattern)


def index_paths(raw_paths: list[str], max_files: int = 400) -> str:
    roots: list[Path] = []
    for rp in raw_paths:
        p = Path(str(rp)).expanduser()
        try:
            p = p.resolve()
        except Exception:
            continue
        if p.exists():
            roots.append(p)
    if not roots:
        return ("Nothing to index — give me a folder or file path "
                "(e.g. ~/Documents/contracts).")

    files = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        for p in sorted(root.rglob("*")):
            if len(files) >= max_files:
                break
            if not p.is_file() or p.suffix.lower() not in (_TEXT_EXT | _DOC_EXT):
                continue
            if any(part in _SKIP_DIRS for part in p.parts):
                continue
            files.append(p)

    indexed = unread = 0
    t0 = time.time()
    with _LOCK:
        c = _conn()
        for f in files:
            text = _read_text(f)
            if not text or not text.strip():
                unread += 1
                continue
            _purge_path(c, str(f))
            for seq, chunk in enumerate(_chunk(text)):
                c.execute(
                    "INSERT OR REPLACE INTO chunks (path, seq, text)"
                    " VALUES (?, ?, ?)", (str(f), seq, chunk))
            indexed += 1
        c.commit()

    n_chunks = _conn().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    dt = time.time() - t0
    msg = (f"Indexed {indexed} file(s) in {dt:.1f}s — "
           f"corpus now {n_chunks} chunk(s).")
    if unread:
        msg += f" {unread} file(s) unreadable (skipped)."
    if len(files) >= max_files:
        msg += f" Capped at {max_files} files — index a narrower folder."
    return msg


def _status() -> str:
    with _LOCK:
        c = _conn()
        n_files = c.execute("SELECT COUNT(DISTINCT path) FROM chunks").fetchone()[0]
        n_chunks = c.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not n_chunks:
        return ("Nothing indexed yet. Say 'index <folder>' first — e.g. "
                "'index ~/Documents'.")
    return f"RAG index: {n_files} file(s), {n_chunks} chunk(s). Ask away."


def _escape_fts(query: str) -> str:
    """Free text → safe FTS5 MATCH. Tokens are quoted (so 'C++ OR (x)'
    can't be FTS syntax) and joined with OR: conversational queries
    carry filler ('kya hai') the document never contains, and BM25
    still ranks docs matching MORE terms first."""
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens[:12])


def _retrieve(query: str, k: int = 6) -> list[tuple[str, int, str]]:
    match = _escape_fts(query)
    if not match:
        return []
    with _LOCK:
        try:
            rows = _conn().execute(
                "SELECT t.path, t.seq, t.text FROM chunks t"
                " JOIN chunks_fts f ON f.rowid = t.id"
                " WHERE chunks_fts MATCH ?"
                " ORDER BY bm25(chunks_fts) LIMIT ?",
                (match, max(1, min(20, k))),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [(r[0], r[1], r[2]) for r in rows]


def _extractive(query: str, hits: list[tuple[str, int, str]]) -> str:
    """Offline answer: best passages, quoted. Evidence, not vibes."""
    lines = [f"Top evidence for {query!r}:"]
    for i, (path, seq, text) in enumerate(hits[:4], 1):
        snippet = re.sub(r"\s+", " ", text)
        if len(snippet) > 400:
            # centre the snippet on the query term if present
            m = re.search(re.escape(query.split()[0] if query.split() else ""),
                          snippet, re.I) if query.split() else None
            if m:
                s = max(0, m.start() - 150)
                snippet = "…" + snippet[s:s + 400] + "…"
            else:
                snippet = snippet[:400] + "…"
        lines.append(f"\n[{i}] {path}#chunk{seq}\n    {snippet}")
    lines.append("\n(Offline extraction — say 'index' more folders or "
                 "disable privacy mode for a synthesized answer.)")
    return "\n".join(lines)


def _synthesize(query: str, hits: list[tuple[str, int, str]]) -> str | None:
    from core import gemini
    if not gemini.api_key():
        return None
    from core import privacy as _privacy
    if _privacy.is_on():
        return None                           # privacy: evidence stays here
    ctx = "\n\n".join(f"--- {p}#{s} ---\n{t[:900]}"
                      for p, s, t in hits[:6])
    resp = gemini.call(
        ["Answer the question using ONLY the passages below. Cite file "
         "paths inline. If the passages don't contain the answer, say so.\n"
         f"QUESTION: {query}\n\nPASSAGES:\n{ctx}"],
        tier=gemini.SMART, timeout_ms=45_000)
    if resp is None:
        return None
    try:
        text = "".join(p.text for p in resp.candidates[0].content.parts
                       if getattr(p, "text", None)).strip()
        return text or None
    except Exception:
        return None


def rag(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()

    if action == "index":
        paths = params.get("paths") or params.get("path") or ""
        if isinstance(paths, str):
            paths = [p for p in re.split(r"[;,]", paths) if p.strip()]
        return index_paths([str(p) for p in paths])
    if action == "status" or (not action and not params.get("query")):
        return _status()

    query = str(params.get("query") or params.get("question") or "").strip()
    if not query:
        return _status()

    hits = _retrieve(query)
    if not hits:
        return (f"Nothing in the index matches {query!r}. "
                "Index more files, or reword the question.")
    synth = _synthesize(query, hits)
    if synth:
        return synth
    return _extractive(query, hits)


TOOL = {
    "name": "rag",
    "description": (
        "Ask questions of the user's own files, offline (sqlite FTS5 — no "
        "API needed). Actions: index (pass `path` — folder or file; text, "
        "markdown, code, CSV read directly, PDF/DOCX via file_processor), "
        "ask (default — pass `query`; returns cited passages, or a "
        "synthesized answer when a key is configured and privacy mode is "
        "off), status. Use for 'what does my rent agreement say about "
        "notice period', 'summarise this folder', 'index my notes'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "index | ask | status"},
            "path": {"type": "STRING", "description": "Folder/file to index"},
            "query": {"type": "STRING", "description": "Question to answer"},
        },
        "required": [],
    },
    "handler": rag,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return rag(params,
               player=(ctx or {}).get("player"),
               session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(rag({}))
