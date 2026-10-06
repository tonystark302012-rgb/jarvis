"""
clip_history — persistent, searchable clipboard history (SQLite + FTS5).

v2 (upgrade over the old in-memory ring):
  * PERSISTENT — memory/clip.db survives restarts; newest 500 kept.
  * SEARCH — FTS5 prefix match AND substring LIKE merged (deduped), so
    'example' finds a URL and 'amp' still finds it inside a word.
  * IMAGES — while watching, a copied image is saved to
    memory/clip_images/ and listed as an entry (text copies behave as
    before).
  * OCR — action=ocr <index> reads text from an image entry through
    actions.region_ocr._read_text (tesseract offline → Gemini vision),
    never a second OCR stack.
  * REDACT — save with redact=on masks emails / phone numbers / card
    numbers / IPv4 addresses BEFORE they touch disk (privacy-first).

Everything is local: SQLite, no network unless you explicitly OCR an
image without tesseract (then the same Gemini path the app already
uses, gated by privacy mode).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_WATCHER: threading.Thread | None = None
_STOP = threading.Event()
_INTERVAL = 1.5
_MAX = 500
_CLIP_IMAGES: list = []          # recent image hashes for dedupe (ring)


# ── storage ─────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "memory" / "clip.db"


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        p = _db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(p), check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("CREATE TABLE IF NOT EXISTS clips ("
                  " id INTEGER PRIMARY KEY,"
                  " ts REAL NOT NULL,"
                  " kind TEXT NOT NULL DEFAULT 'text',"
                  " text TEXT NOT NULL DEFAULT '',"
                  " path TEXT,"
                  " meta TEXT)")
        c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING"
                  " fts5(text)")
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


def _prune(c: sqlite3.Connection) -> None:
    row = c.execute("SELECT id FROM clips ORDER BY id DESC LIMIT 1"
                    ).fetchone()
    if row and row["id"] > _MAX:
        cut = row["id"] - _MAX
        ids = [r["id"] for r in
               c.execute("SELECT id FROM clips WHERE id <= ?", (cut,))]
        if ids:
            marks = ",".join("?" * len(ids))
            c.execute(f"DELETE FROM clips WHERE id IN ({marks})", ids)
            c.execute(f"DELETE FROM clips_fts WHERE rowid IN ({marks})",
                      ids)


def _last_text(c: sqlite3.Connection) -> str:
    row = c.execute("SELECT text FROM clips WHERE kind = 'text'"
                    " ORDER BY id DESC LIMIT 1").fetchone()
    return row["text"] if row else ""


# ── redaction (before anything touches disk) ────────────────────────────────

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s]{7,}\d)(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IPV4 = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")


def _redact(text: str) -> str:
    text = _CARD.sub("[card]", text)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    text = _IPV4.sub("[ip]", text)
    return text


# ── seams (tests monkeypatch these instead of a real clipboard) ─────────────

def _clip_get() -> str | None:
    try:
        import pyperclip
        return pyperclip.paste()
    except Exception:
        return None


def _clip_set(text: str) -> bool:
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _clip_image():
    """PIL Image currently on the clipboard, or None (no clipboard / not an
    image / headless). Never raises."""
    try:
        from PIL import ImageGrab
        img = ImageGrab.grabclipboard()
        if img is None or not hasattr(img, "tobytes"):
            return None
        return img
    except Exception:
        return None


# ── writes ──────────────────────────────────────────────────────────────────

def record(text: str, kind: str = "text", path: str | None = None,
           meta: str | None = None, redact: bool = False) -> bool:
    """Store one entry (text deduped against the newest text row).
    redact=True masks emails/phones/cards/IPs before the write."""
    text = (text or "").strip() if kind == "text" else (text or "")
    if kind == "text":
        if not text:
            return False
        if redact:
            text = _redact(text)
        with _LOCK:
            c = _conn()
            if text == _last_text(c):
                return False
            cur = c.execute(
                "INSERT INTO clips (ts, kind, text, path, meta)"
                " VALUES (?,?,?,?,?)",
                (time.time(), kind, text[:8000], path, meta))
            c.execute("INSERT INTO clips_fts (rowid, text) VALUES (?, ?)",
                      (cur.lastrowid, text[:8000]))
            _prune(c)
            c.commit()
        return True
    # images/audio: always stored (dedupe by caller via meta hash)
    with _LOCK:
        c = _conn()
        if meta:
            same = c.execute("SELECT id FROM clips WHERE kind = ? AND"
                             " meta = ? ORDER BY id DESC LIMIT 1",
                             (kind, meta)).fetchone()
            if same:
                return False
        cur = c.execute(
            "INSERT INTO clips (ts, kind, text, path, meta) VALUES (?,?,?,?,?)",
            (time.time(), kind, text, path, meta))
        if text:
            c.execute("INSERT INTO clips_fts (rowid, text) VALUES (?, ?)",
                      (cur.lastrowid, text))
        _prune(c)
        c.commit()
    return True


def _record_image(img, base: Path) -> bool:
    """Save a clipboard image under memory/clip_images/ + store the row."""
    import hashlib
    try:
        data = img.tobytes()
    except Exception:
        return False
    digest = hashlib.sha1(
        f"{img.size}:{img.mode}".encode() + data).hexdigest()[:16]
    if digest in _CLIP_IMAGES:
        return False
    _CLIP_IMAGES.append(digest)
    del _CLIP_IMAGES[:-20]
    out_dir = base / "memory" / "clip_images"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"clip-{time.strftime('%Y%m%d-%H%M%S')}-{digest[:6]}.png"
    try:
        img.save(p, format="PNG")
    except Exception:
        return False
    return record("", kind="image", path=str(p), meta=digest)


def _watch_loop():
    from config import get_base_dir
    base = get_base_dir()
    while not _STOP.is_set():
        val = _clip_get()
        if val:
            record(val)
        img = _clip_image()
        if img is not None:
            try:
                _record_image(img, base)
            except Exception:
                pass
        _STOP.wait(_INTERVAL)


def watch(on: bool) -> str:
    global _WATCHER
    if on:
        if _WATCHER and _WATCHER.is_alive():
            return "Clipboard watch already on."
        if _clip_get() is None and _clip_image() is None:
            return ("No clipboard available on this system (pyperclip "
                    "missing or headless).")
        _STOP.clear()
        _WATCHER = threading.Thread(target=_watch_loop, daemon=True,
                                    name="clip-history")
        _WATCHER.start()
        return ("Clipboard watch on — new text and image copies will be "
                "recorded.")
    _STOP.set()
    _WATCHER = None
    return "Clipboard watch off."


# ── reads ───────────────────────────────────────────────────────────────────

def _search(c: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """FTS5 prefix match ∪ substring LIKE — merged, newest first."""
    tokens = re.findall(r"\w+", query)
    hits: dict[int, sqlite3.Row] = {}
    if tokens:
        match = " ".join(t + "*" for t in tokens)
        try:
            for r in c.execute(
                    "SELECT id, ts, kind, text, path, meta FROM clips"
                    " WHERE id IN (SELECT rowid FROM clips_fts WHERE"
                    " clips_fts MATCH ?) ORDER BY id DESC", (match,)):
                hits[r["id"]] = r
        except sqlite3.OperationalError:
            pass                                   # odd match syntax → LIKE only
    like = f"%{query}%"
    for r in c.execute(
            "SELECT id, ts, kind, text, path, meta FROM clips"
            " WHERE text LIKE ? ORDER BY id DESC", (like,)):
        hits.setdefault(r["id"], r)
    return sorted(hits.values(), key=lambda r: -r["id"])


def _row_line(i: int, it: sqlite3.Row) -> str:
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(it["ts"]))
    if it["kind"] != "text":
        return f"{i}. [{when}] [{it['kind']}] {it['path'] or ''}"
    one = it["text"].replace("\n", " ")
    if len(one) > 100:
        one = one[:97] + "…"
    return f"{i}. [{when}] {one}"


def clip_history(parameters: dict | None = None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    c = _conn()

    if action == "watch":
        state = str(params.get("state", "on")).lower().strip()
        result = watch(state in ("on", "true", "1", "start"))

    elif action == "clear":
        with _LOCK:
            c.execute("DELETE FROM clips")
            c.execute("DELETE FROM clips_fts")
            c.commit()
        result = "Clipboard history cleared."

    elif action == "save":
        ok = record(str(params.get("text", "")),
                    redact=str(params.get("redact", "")).lower()
                    in ("on", "1", "true", "yes"))
        result = ("Saved" + (" (redacted)." if str(
            params.get("redact", "")).lower() in ("on", "1", "true", "yes")
            else ".")) if ok else "Nothing new to save."

    elif action == "ocr":
        try:
            idx = int(params.get("index", 1))
        except (TypeError, ValueError):
            idx = 1
        with _LOCK:
            rows = c.execute("SELECT * FROM clips ORDER BY id DESC"
                             ).fetchall()
        pick = rows[idx - 1] if 1 <= idx <= len(rows) else None
        if pick is None:
            return f"No entry #{idx}. History has {len(rows)} entries."
        if pick["kind"] != "image" or not pick["path"]:
            return f"Entry #{idx} is not an image — nothing to OCR."
        try:
            from PIL import Image
            from actions.region_ocr import _read_text
            img = Image.open(pick["path"])
            text = _read_text(img, mode="text")
            return f"OCR of entry #{idx}:\n{text or '(no text found)'}"
        except Exception as e:                     # noqa: BLE001
            return (f"OCR unavailable for entry #{idx} "
                    f"({type(e).__name__}: {e}) — needs tesseract or a "
                    "Gemini key (same reader region_ocr uses).")

    elif action in ("use", "paste", "get"):
        try:
            idx = int(params.get("index", 1))
        except (TypeError, ValueError):
            idx = 1
        with _LOCK:
            rows = c.execute("SELECT * FROM clips ORDER BY id DESC"
                             ).fetchall()
        if not rows:
            return "Clipboard history is empty."
        pick = rows[idx - 1] if 1 <= idx <= len(rows) else None
        if pick is None:
            return f"No entry #{idx}. History has {len(rows)} entries."
        if pick["kind"] != "text":
            return (f"Entry #{idx} is an {pick['kind']} at "
                    f"{pick['path']} — open it to reuse (clipboard stores "
                    "text only here).")
        if _clip_set(pick["text"]):
            result = f"Entry #{idx} copied back to the clipboard."
        else:
            result = (f"Entry #{idx} (clipboard unavailable to write):\n"
                      f"{pick['text'][:500]}")

    else:
        query = str(params.get("query", "")).strip()
        with _LOCK:
            if query:
                items = _search(c, query)
            else:
                items = c.execute(
                    "SELECT id, ts, kind, text, path, meta FROM clips"
                    " ORDER BY id DESC").fetchall()
        if not items:
            return (f"No clipboard entries"
                    f"{f' matching {query!r}' if query else ''}.")
        shown = items[:15]
        lines = [_row_line(i, it) for i, it in enumerate(shown, 1)]
        total = len(items)
        result = (f"Clipboard history ({total} "
                  f"{'matching ' + repr(query) if query else 'total'}):\n"
                  + "\n".join(lines))

    if player is not None and action == "list":
        try:
            player.show_content("CLIPBOARD HISTORY", result[:4000])
        except Exception:
            pass
    return result


TOOL = {
    "name": "clip_history",
    "description": (
        "Persistent clipboard history (SQLite, survives restarts; newest "
        "500). Actions: list (default) with substring+FTS query search, "
        "use (copy an entry back by index, 1=most recent), watch on/off "
        "(record new text AND image copies), save (record given text; "
        "redact=on masks emails/phones/cards/IPs first), ocr (read text "
        "from a copied image), clear. Use when the user says 'what did I "
        "copy', 'paste that thing from earlier', 'clipboard history', "
        "'read the text in that screenshot I copied'.")
    ,
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | use | watch | save | ocr | clear."},
            "query": {"type": "STRING",
                      "description": "Search text for list (FTS + substring)."},
            "index": {"type": "STRING",
                      "description": "Entry number for use/ocr (1=recent)."},
            "state": {"type": "STRING", "description": "on/off for watch."},
            "text": {"type": "STRING",
                     "description": "Text to record for save."},
            "redact": {"type": "STRING",
                       "description": "on — mask emails/phones/cards/IPs "
                                      "before storing (save)."},
        },
        "required": [],
    },
    "handler": clip_history,
}
