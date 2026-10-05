# dots/computer.py
"""Per-Dot isolated computer (doc §3.4).

Layout (persists across stop/start BY CONSTRUCTION — stop only kills
procs + flips status, nothing deletes dirs):

    memory/dots_pcs/<id>/work/     ← files jail: every file op and the
                                      shell's cwd resolve inside HERE
    memory/dots_pcs/<id>/profile/  ← browser profile (survives restarts)

Invariants (T7/T8):
  * `_jail` — Path.resolve() then must be inside work/; `..`, absolute
    escapes and symlinks pointing out are refused (resolve follows the
    link, so the escape is visible before any I/O);
  * shell = argv LIST only, never shell=True; cwd = work/; 30 s default
    / 60 s hard cap; output capped; only when the computer's shell
    permission is on (read LIVE — a toggle takes effect next call);
  * browser = playwright if installed, ops serialized on ONE dedicated
    worker thread per computer (sync API objects stay thread-bound);
    honest refusal when playwright is missing;
  * every action appends `computer_audit` with actor='owner'
    (dashboard/voice endpoints) or 'agent' (brain tool calls).

Free/stdlib only for files+shell. PLAYWRIGHT IS OPTIONAL — Docker too
(doc: neither is a hard requirement).
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import threading
import time
from concurrent.futures import Future, TimeoutError as _FutTimeout
from pathlib import Path
from queue import Queue

from . import db

DEFAULT_PERMS = {"browser": False, "files": True, "shell": False}
_TIME_DEFAULT = 30
_TIME_HARD = 60              # spec: 30 s default / 60 s max
_OUT_CAP = 64 * 1024
_READ_CAP = 200 * 1024
_WRITE_CAP = 2 * 1024 * 1024
_OP_TIMEOUT = 30             # browser op cap (playwright also self-caps)

# ── paths ────────────────────────────────────────────────────────────────────
def _pcs_root() -> Path:
    from config import get_base_dir
    d = Path(get_base_dir()) / "memory" / "dots_pcs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dirs(computer_id: int) -> tuple[Path, Path]:
    root = _pcs_root() / str(int(computer_id))
    return root / "work", root / "profile"


def _ensure_dirs(computer_id: int) -> tuple[Path, Path]:
    work, profile = dirs(computer_id)
    work.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True, exist_ok=True)
    return work, profile


def _jail(path_str: str, work: Path) -> Path:
    """Resolve `path_str` (relative or absolute) and REQUIRE it to stay
    inside `work`. Raises ValueError on any escape (T7)."""
    raw = str(path_str if path_str is not None else "")
    if not raw.strip():
        raise ValueError("path required")
    base = work.resolve()
    target = Path(raw)
    if not target.is_absolute():
        target = base / target
    resolved = target.resolve()          # follows .. and symlinks
    if resolved != base and base not in resolved.parents:
        raise ValueError(
            f"path escapes the computer workspace: {raw!r}")
    return resolved


# ── rows / audit ─────────────────────────────────────────────────────────────
def _comp_row(r) -> dict:
    c = dict(r)
    try:
        c["perms"] = json.loads(c.pop("perms_json") or "{}")
    except ValueError:
        c["perms"] = {}
    c["work"], c["profile"] = (str(p) for p in dirs(c["id"]))
    return c


def _row(cid: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute("SELECT * FROM computers WHERE id = ?",
                               (int(cid),)).fetchone()
    return _comp_row(r) if r else None


def get(cid: int) -> dict | None:
    return _row(cid)


def for_dot(dot_id: int) -> dict | None:
    with db._LOCK:
        r = db._conn().execute(
            "SELECT * FROM computers WHERE dot_id = ? ORDER BY id",
            (int(dot_id),)).fetchone()
    return _comp_row(r) if r else None


def list_computers(dot_id: int | None = None) -> list[dict]:
    with db._LOCK:
        if dot_id is None:
            rows = db._conn().execute(
                "SELECT * FROM computers ORDER BY id").fetchall()
        else:
            rows = db._conn().execute(
                "SELECT * FROM computers WHERE dot_id = ? ORDER BY id",
                (int(dot_id),)).fetchall()
    return [_comp_row(r) for r in rows]


def audit(cid: int, actor: str, action: str, detail: str = "",
          ok: bool = True) -> None:
    with db._LOCK:
        c = db._conn()
        c.execute(
            "INSERT INTO computer_audit (computer_id, actor, action,"
            " detail, ok, created_at) VALUES (?,?,?,?,?,?)",
            (int(cid), str(actor), str(action), str(detail or ""),
             1 if ok else 0, time.time()))
        c.commit()


def audit_rows(cid: int, limit: int = 100) -> list[dict]:
    limit = max(1, min(500, int(limit)))
    with db._LOCK:
        rows = db._conn().execute(
            "SELECT * FROM computer_audit WHERE computer_id = ?"
            " ORDER BY id DESC LIMIT ?", (int(cid), limit)).fetchall()
    out = [dict(r) for r in rows]
    for a in out:
        a["ok"] = bool(a["ok"])
    return out


# ── lifecycle ────────────────────────────────────────────────────────────────
def create_for_dot(dot_id: int, perms: dict | None = None) -> dict:
    from . import store
    if store.get_dot(int(dot_id)) is None:
        raise KeyError(f"no dot #{dot_id}")
    existing = for_dot(dot_id)
    if existing is not None:
        raise ValueError(f"dot #{dot_id} already has computer "
                         f"#{existing['id']}")
    p = dict(DEFAULT_PERMS)
    if isinstance(perms, dict):
        for k in DEFAULT_PERMS:
            if k in perms:
                p[k] = bool(perms[k])
    now = time.time()
    with db._LOCK:
        c = db._conn()
        cur = c.execute(
            "INSERT INTO computers (dot_id, status, root_path, perms_json,"
            " created_at) VALUES (?,'stopped','',?,?)",
            (int(dot_id), json.dumps(p), now))
        cid = cur.lastrowid
        root = _pcs_root() / str(cid)
        c.execute("UPDATE computers SET root_path = ? WHERE id = ?",
                  (str(root), cid))
        c.commit()
    _ensure_dirs(cid)
    comp = _row(cid)
    audit(cid, "owner", "create",
          f"computer for dot #{dot_id}", ok=True)
    return comp


def start(cid: int, actor: str = "owner") -> dict:
    comp = _row(cid)
    if comp is None:
        raise KeyError(f"no computer #{cid}")
    _ensure_dirs(cid)
    _set_status(cid, "running")
    audit(cid, actor, "start", "accepted tools")
    return _row(cid)


def stop(cid: int, actor: str = "owner") -> dict:
    comp = _row(cid)
    if comp is None:
        raise KeyError(f"no computer #{cid}")
    killed = _kill_live(cid)
    _stop_worker(cid)
    _set_status(cid, "stopped")
    audit(cid, actor, "stop", f"killed {killed} live process(es)")
    return _row(cid)


def delete(cid: int, actor: str = "owner") -> bool:
    comp = _row(cid)
    if comp is None:
        return False
    stop(cid, actor=actor)
    import shutil
    root = _pcs_root() / str(int(cid))
    resolved_root = _pcs_root().resolve()
    target = root.resolve() if root.exists() else root
    # safety: only ever delete DIRECTLY under dots_pcs/
    if resolved_root in target.parents or target == resolved_root / str(cid):
        shutil.rmtree(root, ignore_errors=True)
    with db._LOCK:
        c = db._conn()
        c.execute("DELETE FROM computers WHERE id = ?", (int(cid),))
        c.commit()
    return True


def set_perms(cid: int, perms: dict, actor: str = "owner") -> dict:
    comp = _row(cid)
    if comp is None:
        raise KeyError(f"no computer #{cid}")
    if not isinstance(perms, dict):
        raise ValueError("perms must be an object")
    merged = dict(comp["perms"])
    for k in DEFAULT_PERMS:
        if k in perms:
            merged[k] = bool(perms[k])
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE computers SET perms_json = ? WHERE id = ?",
                  (json.dumps(merged), int(cid)))
        c.commit()
    audit(cid, actor, "perms", json.dumps(merged))
    return _row(cid)


def _set_status(cid: int, status: str) -> None:
    with db._LOCK:
        c = db._conn()
        c.execute("UPDATE computers SET status = ? WHERE id = ?",
                  (str(status), int(cid)))
        c.commit()


# ── live processes (stop kills them) ────────────────────────────────────────
_LIVE: dict[int, set] = {}
_LIVE_LOCK = threading.Lock()


def _register_proc(cid: int, proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        _LIVE.setdefault(int(cid), set()).add(proc)


def _unregister_proc(cid: int, proc: subprocess.Popen) -> None:
    with _LIVE_LOCK:
        s = _LIVE.get(int(cid))
        if s:
            s.discard(proc)


def _kill_live(cid: int) -> int:
    with _LIVE_LOCK:
        procs = list(_LIVE.get(int(cid), ()))
    killed = 0
    for p in procs:
        try:
            p.kill()
            killed += 1
        except Exception:
            pass
    return killed


# ── gates ────────────────────────────────────────────────────────────────────
def _comp_for(dot_id: int) -> tuple[dict | None, str | None]:
    comp = for_dot(dot_id)
    if comp is None:
        return None, ("no computer yet — the owner creates one: "
                      "pc action=create dot=… (or POST /api/computers)")
    return comp, None


def _gate(comp: dict, need: str, action: str, actor: str) -> str | None:
    """Permission gate — perms_json is read LIVE from the row every
    call, so an owner toggle takes effect on the very next tool call
    (doc §3.4) → honest refusal string or None."""
    fresh = _row(comp["id"]) or comp
    perms = fresh.get("perms") or {}
    if not perms.get(need):
        msg = f"denied: the computer's {need!r} permission is off"
        audit(comp["id"], actor, action, msg, ok=False)
        return msg
    return None


def _gate_running(comp: dict, action: str, actor: str) -> str | None:
    if comp["status"] != "running":
        msg = (f"computer #{comp['id']} is stopped — the owner must "
               f"start it first (pc action=start / POST .../start)")
        audit(comp["id"], actor, action, msg, ok=False)
        return msg
    return None


# ── files (jailed; allowed while stopped too — they persist) ────────────────
def files_list(comp: dict, sub: str = "", actor: str = "agent") -> dict:
    ref = _gate(comp, "files", "files_list", actor)
    if ref:
        return {"error": ref}
    work = dirs(comp["id"])[0]
    try:
        base = _jail(sub or ".", work)
    except ValueError as e:
        audit(comp["id"], actor, "files_list", str(e), ok=False)
        return {"error": f"denied: {e}"}
    if not base.exists():
        return {"error": f"no such directory: {sub!r}"}
    if not base.is_dir():
        return {"error": f"not a directory: {sub!r}"}
    entries = []
    try:
        for ch in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            rel = ch.relative_to(work)
            entries.append(str(rel) + ("/" if ch.is_dir() else ""))
    except PermissionError as e:
        return {"error": str(e)}
    audit(comp["id"], actor, "files_list", sub or ".")
    return {"ok": True, "path": sub or ".", "entries": entries}


def files_read(comp: dict, path: str, actor: str = "agent") -> dict:
    ref = _gate(comp, "files", "files_read", actor)
    if ref:
        return {"error": ref}
    work = dirs(comp["id"])[0]
    try:
        target = _jail(path, work)
    except ValueError as e:
        audit(comp["id"], actor, "files_read", str(e), ok=False)
        return {"error": f"denied: {e}"}
    if not target.is_file():
        return {"error": f"no such file: {path!r}"}
    try:
        data = target.read_bytes()[:_READ_CAP]
    except OSError as e:
        return {"error": str(e)}
    truncated = target.stat().st_size > _READ_CAP
    audit(comp["id"], actor, "files_read", path)
    return {"ok": True, "path": path,
            "content": data.decode("utf-8", errors="replace"),
            "truncated": truncated}


def files_write(comp: dict, path: str, content: str,
                append: bool = False, actor: str = "agent") -> dict:
    ref = _gate(comp, "files", "files_write", actor)
    if ref:
        return {"error": ref}
    work = dirs(comp["id"])[0]
    try:
        target = _jail(path, work)
    except ValueError as e:
        audit(comp["id"], actor, "files_write", str(e), ok=False)
        return {"error": f"denied: {e}"}
    blob = str(content if content is not None else "").encode("utf-8")
    if len(blob) > _WRITE_CAP:
        return {"error": f"content too large ({len(blob)} > {_WRITE_CAP})"}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if append and target.exists():
            with target.open("ab") as f:
                f.write(blob)
        else:
            target.write_bytes(blob)
    except OSError as e:
        return {"error": str(e)}
    audit(comp["id"], actor, "files_write",
          f"{path} ({len(blob)} bytes, append={append})")
    return {"ok": True, "path": path, "bytes": len(blob)}


# ── shell (argv list, cwd jailed, 30 s default / 60 s hard) ─────────────────
def shell(comp: dict, argv, timeout: int | None = None,
          actor: str = "agent") -> dict:
    ref = _gate(comp, "shell", "exec", actor)
    if ref:
        return {"error": ref}
    ref = _gate_running(comp, "exec", actor)
    if ref:
        return {"error": ref}
    if not isinstance(argv, (list, tuple)) or not argv:
        return {"error": "argv must be a non-empty list of strings "
                         "(no shell string, ever)"}
    argv = [str(a) for a in argv]
    if not all(a.strip() for a in argv):
        return {"error": "argv entries must be non-empty strings"}
    try:
        t = int(timeout) if timeout else _TIME_DEFAULT
    except (TypeError, ValueError):
        t = _TIME_DEFAULT
    t = max(1, min(t, _TIME_HARD))
    work = dirs(comp["id"])[0]
    try:
        proc = subprocess.Popen(
            argv, cwd=str(work),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, errors="replace")
    except (FileNotFoundError, PermissionError, OSError) as e:
        audit(comp["id"], actor, "exec", f"{argv} → {e}", ok=False)
        return {"error": f"cannot run {argv[0]!r}: {e}"}
    _register_proc(comp["id"], proc)
    try:
        out, err = proc.communicate(timeout=t)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        audit(comp["id"], actor, "exec",
              f"{argv} → timeout after {t}s", ok=False)
        return {"error": f"timeout after {t}s (hard cap {_TIME_HARD}s) "
                         f"— process killed"}
    finally:
        _unregister_proc(comp["id"], proc)
    text = (out or "") + (("\n[stderr]\n" + err) if err else "")
    truncated = len(text) > _OUT_CAP
    if truncated:
        text = text[:_OUT_CAP] + "\n… [output truncated]"
    stopped = _row(comp["id"])["status"] != "running"
    audit(comp["id"], actor, "exec",
          f"{' '.join(argv)[:200]} → rc={proc.returncode}"
          + (" (computer stopped mid-run)" if stopped else ""),
          ok=proc.returncode == 0)
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": text, "truncated": truncated, "timeout": t}


# ── browser (playwright optional; one worker thread per computer) ───────────
def _pw_available() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


class _PlaywrightEngine:
    """Real engine — constructed and USED inside the worker thread."""

    def __init__(self, computer_id: int):
        self.work, self.profile = dirs(computer_id)
        self.shots = self.work / "_shots"
        self._pw = None
        self._ctx = None

    def _page(self):
        if self._ctx is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._ctx = self._pw.chromium.launch_persistent_context(
                str(self.profile), headless=True)
        pages = getattr(self._ctx, "pages", None) or []
        return pages[0] if pages else self._ctx.new_page()

    def perform(self, op: str, **kw):
        page = self._page()
        if op == "navigate":
            url = str(kw.get("url") or "")
            scheme = url.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                return {"error": "only http(s) urls may be opened"}
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            return {"ok": True, "url": page.url, "title": page.title()}
        if op == "read":
            body = page.inner_text("body")[:8000]
            return {"ok": True, "url": page.url, "title": page.title(),
                    "text": body}
        if op == "snapshot":
            els = page.eval_on_selector_all(
                "a,button,input,textarea,select,[role=button],[role=link]",
                """els => els.slice(0, 80).map((e, i) => ({
                    tag: e.tagName.toLowerCase(),
                    sel: e.id ? ('#' + e.id) : e.tagName.toLowerCase(),
                    text: ((e.innerText || e.value || e.getAttribute('aria-label')
                            || '') + '').trim().slice(0, 70)
                }))""")
            body = page.inner_text("body")[:6000]
            return {"ok": True, "url": page.url, "title": page.title(),
                    "text": body, "elements": els}
        if op == "screenshot":
            self.shots.mkdir(parents=True, exist_ok=True)
            path = self.shots / f"{int(time.time() * 1000)}.png"
            page.screenshot(path=str(path), full_page=False)
            return {"ok": True, "path": str(path.relative_to(self.work))}
        if op == "click":
            page.click(str(kw.get("target") or ""), timeout=8000)
            return {"ok": True, "clicked": kw.get("target")}
        if op == "type":
            page.fill(str(kw.get("target") or ""),
                      str(kw.get("text") or ""))
            return {"ok": True, "typed": str(kw.get("text") or "")[:40]}
        if op == "key":
            page.keyboard.press(str(kw.get("key") or "Enter"))
            return {"ok": True, "key": kw.get("key")}
        if op == "scroll":
            direction = str(kw.get("direction") or "down").lower()
            amount = int(kw.get("amount") or 600)
            page.mouse.wheel(0, -amount if direction == "up" else amount)
            return {"ok": True, "scrolled": direction}
        return {"error": f"unknown browser op: {op}"}

    def close(self) -> None:
        for closer in (lambda: self._ctx.close(),
                       lambda: self._pw.stop()):
            try:
                closer()
            except Exception:
                pass
        self._ctx = self._pw = None


def _engine_factory(computer_id: int):
    """Seam: tests inject a fake engine; production = playwright."""
    return _PlaywrightEngine(computer_id)


class _Worker:
    """One dedicated thread per computer — playwright sync objects are
    thread-bound, so ALL browser ops serialize through this queue."""

    def __init__(self, computer_id: int):
        self.computer_id = computer_id
        self.engine = _engine_factory(computer_id)
        self.q: Queue = Queue()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"pc-{computer_id}")
        self.thread.start()

    def _run(self) -> None:
        try:
            while True:
                item = self.q.get()
                if item is None:
                    break
                op, kw, fut = item
                try:
                    fut.set_result(self.engine.perform(op, **kw))
                except Exception as e:      # engine bug → honest string
                    try:
                        fut.set_exception(e)
                    except Exception:
                        pass
        finally:
            try:
                self.engine.close()
            except Exception:
                pass

    def submit(self, op: str, **kw) -> dict:
        fut: Future = Future()
        self.q.put((op, kw, fut))
        try:
            return fut.result(timeout=_OP_TIMEOUT)
        except _FutTimeout:
            return {"error": f"browser op {op!r} timed out "
                             f"after {_OP_TIMEOUT}s"}


_WORKERS: dict[int, _Worker] = {}
_WLOCK = threading.Lock()


def _stop_worker(cid: int) -> None:
    with _WLOCK:
        worker = _WORKERS.pop(int(cid), None)
    if worker is None:
        return
    worker.q.put(None)
    worker.thread.join(timeout=5)


def _worker(cid: int) -> _Worker:
    with _WLOCK:
        w = _WORKERS.get(int(cid))
        if w is None:
            w = _Worker(int(cid))
            _WORKERS[int(cid)] = w
        return w


def browser(comp: dict, op: str, actor: str = "agent", **kw) -> dict:
    ref = _gate(comp, "browser", f"browser_{op}", actor)
    if ref:
        return {"error": ref}
    if comp["status"] != "running":
        # honest refusal instead of silently queueing against a dead box
        msg = _gate_running(comp, f"browser_{op}", actor)
        return {"error": msg}
    if not _pw_available():
        msg = ("playwright is not installed in this JARVIS — browser "
               "tools are unavailable (pip install playwright && "
               "playwright install chromium); files and exec still work")
        audit(comp["id"], actor, f"browser_{op}", msg, ok=False)
        return {"error": msg}
    try:
        result = _worker(comp["id"]).submit(op, **kw)
    except Exception as e:
        audit(comp["id"], actor, f"browser_{op}",
              f"{e}", ok=False)
        return {"error": f"browser {op} failed: {e}"}
    audit(comp["id"], actor, f"browser_{op}",
          json.dumps({k: str(v)[:120] for k, v in kw.items()}),
          ok="error" not in result)
    return result


def reset_for_tests() -> None:
    """Drop live workers/procs so each test starts from zero state."""
    with _WLOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for w in workers:
        try:
            w.q.put(None)
            w.thread.join(timeout=2)
        except Exception:
            pass
    with _LIVE_LOCK:
        for procs in _LIVE.values():
            for pr in procs:
                try:
                    pr.kill()
                except Exception:
                    pass
        _LIVE.clear()


# ── brain-side entry (dot → computer → tool name) ────────────────────────────
def run_agent_tool(dot_id: int, name: str, args: dict) -> str:
    """Execute one brain tool call with actor='agent'. Returns a string
    the model reads — dicts are JSON-encoded, errors stay honest."""
    comp, err = _comp_for(dot_id)
    if err:
        return f"error: {err}"   # no computer → nothing to audit against
    a = args if isinstance(args, dict) else {}
    try:
        if name == "files_list":
            r = files_list(comp, str(a.get("path") or a.get("sub") or ""))
        elif name == "files_read":
            r = files_read(comp, str(a.get("path") or ""))
        elif name == "files_write":
            r = files_write(comp, str(a.get("path") or ""),
                            a.get("content"), append=bool(a.get("append")))
        elif name == "exec":
            r = shell(comp, a.get("argv"), a.get("timeout"), actor="agent")
        elif name.startswith("computer_"):
            op = name[len("computer_"):]
            r = browser(comp, op, actor="agent",
                        **{k: v for k, v in a.items()
                           if k not in ("actor",)})
        else:
            return f"unknown tool: {name}"
    except Exception as e:
        audit(comp["id"], "agent", name, str(e), ok=False)
        return f"error: {name} failed ({type(e).__name__}: {e})"
    return _as_tool_text(r)


def _as_tool_text(r) -> str:
    import json as _json
    if isinstance(r, str):
        return r
    if isinstance(r, dict):
        if "error" in r and len(r) == 1:
            return r["error"]
        return _json.dumps(r, ensure_ascii=False, default=str)
    return str(r)

