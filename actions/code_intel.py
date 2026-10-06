"""
code_intel — IDE-grade code intelligence inside JARVIS (jedi + pylsp).

WHY NOT A DUPLICATE
    code_outline lists STRUCTURE (tree-sitter/ast symbols).
    dev_agent BUILDS and FIXES projects with an LLM.
    This answers NAVIGATION and DIAGNOSTIC questions — where is this
    defined, who else calls it, what does this symbol do, what is
    currently wrong with this file — the goto-def / find-refs / hover /
    diagnostics layer every editor takes for granted.

ENGINES (free, offline — the Report-E pylsp + jedi stack)
    * jedi (pure Python, in-process)  → complete | goto | refs | hover
    * python-lsp-server (pylsp) over stdio JSON-RPC → diagnostics
      (syntax errors via jedi; pyflakes/pycodestyle when present)
    * symbols → delegates to actions.code_outline (one outline, reused)

`action=setup` pip-installs what is missing, allowlist-checked first
(same safety philosophy as dev_agent's dependency installer).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_MAX_REFS = 20
_MAX_COMPLETIONS = 40
_SETUP_SAFE = {"jedi", "python-lsp-server", "python-lsp-json-rpc",
               "pyflakes", "pycodestyle", "pluggy", "ujson",
               "docstring-to-markdown", "jedi-language-server"}


# ── seams ───────────────────────────────────────────────────────────────────

def _jedi():
    try:
        import jedi
        return jedi
    except Exception:
        return None


def _resolve(params: dict) -> tuple[Path | None, str]:
    """File path + honest errors for missing/odd requests."""
    raw = str(params.get("path", "") or "").strip()
    if not raw:
        return None, "Give a file `path` (e.g. actions/rules.py)."
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except Exception:
        return None, f"Cannot resolve {raw!r}."
    if not p.exists():
        return None, f"No such file: {p}"
    if not p.is_file():
        return None, f"Not a file: {p}"
    if p.suffix.lower() not in (".py", ".pyi"):
        return None, (f"jedi navigation is Python-only — {p.suffix or p.name}"
                      " belongs to code_outline (multi-language symbols).")
    return p, ""


def _line_col(params: dict, text: str) -> tuple[int, int]:
    """Human params are 1-based (line AND col); jedi wants 1-based line +
    0-based column — convert here so callers never think about it.
    Defaults to the end of the buffer."""
    try:
        line = int(params.get("line", 0))
    except (TypeError, ValueError):
        line = 0
    has_col = params.get("col") not in (None, "")
    try:
        col = int(params.get("col", 0))
    except (TypeError, ValueError):
        col = 0
    lines = text.splitlines() or [""]
    if line <= 0:                      # default: end of buffer
        return len(lines), len(lines[-1])
    line = max(1, min(line, len(lines)))
    if has_col:
        col = max(1, min(col, len(lines[line - 1]))) - 1   # → 0-based
    else:
        col = len(lines[line - 1])
    return line, col


def _fmt_pos(path: Path, line: int, col: int, snippet: str = "") -> str:
    rel = path.name
    snip = snippet.strip().replace("\n", " ")
    if len(snip) > 90:
        snip = snip[:87] + "…"
    return f"{rel}:{line}:{col}{('  — ' + snip) if snip else ''}"


# ── jedi actions ────────────────────────────────────────────────────────────

def _complete(params: dict) -> str:
    jedi = _jedi()
    if jedi is None:
        return ("jedi is not installed — say 'setup code intel' or run: "
                "pip install jedi")
    p, err = _resolve(params)
    if err:
        return err
    text = p.read_text(encoding="utf-8", errors="replace")
    line, col = _line_col(params, text)
    try:
        comps = jedi.Script(code=text, path=p).complete(line, col)
    except Exception as e:
        return f"Completion failed at {p.name}:{line}:{col} ({e})."
    if not comps:
        return f"No completions at {p.name}:{line}:{col}."
    names = []
    for c in comps[:_MAX_COMPLETIONS]:
        try:
            names.append(f"{c.name} ({c.type})")
        except Exception:
            names.append(str(c.name))
    more = len(comps) - len(names)
    head = f"{len(comps)} completion(s) at {p.name}:{line}:{col}:"
    return head + "\n" + "\n".join(names) + (
        f"\n… and {more} more." if more > 0 else "")


def _goto(params: dict) -> str:
    jedi = _jedi()
    if jedi is None:
        return ("jedi is not installed — say 'setup code intel' or run: "
                "pip install jedi")
    p, err = _resolve(params)
    if err:
        return err
    text = p.read_text(encoding="utf-8", errors="replace")
    line, col = _line_col(params, text)
    try:
        defs = jedi.Script(code=text, path=p).goto(line, col,
                                                   follow_imports=True)
    except Exception as e:
        return f"goto failed at {p.name}:{line}:{col} ({e})."
    if not defs:
        return f"Nothing defined for the symbol at {p.name}:{line}:{col}."
    out = []
    for d in defs[:8]:
        if d.line is None:
            out.append(f"{d.module_path.name} (builtin/module {d.name})")
            continue
        snip = ""
        try:
            line_txt = (d.module_path.read_text(encoding="utf-8",
                                                errors="replace")
                        .splitlines()[d.line - 1])
            snip = line_txt.strip()
        except Exception:
            pass
        out.append(_fmt_pos(Path(str(d.module_path)), d.line,
                            (d.column or 0) + 1, snip))
    return f"Definition(s) for {p.name}:{line}:{col}:\n" + "\n".join(out)


def _refs(params: dict) -> str:
    jedi = _jedi()
    if jedi is None:
        return ("jedi is not installed — say 'setup code intel' or run: "
                "pip install jedi")
    p, err = _resolve(params)
    if err:
        return err
    text = p.read_text(encoding="utf-8", errors="replace")
    line, col = _line_col(params, text)
    try:
        refs = jedi.Script(code=text, path=p).get_references(
            line, col, include_builtins=False)
    except Exception as e:
        return f"references failed at {p.name}:{line}:{col} ({e})."
    if not refs:
        return f"No references found from {p.name}:{line}:{col}."
    src_lines = text.splitlines()
    out = []
    for r in refs[:_MAX_REFS]:
        # jedi Name (same file) has module_path; references from other
        # modules expose .path — normalise both shapes
        mpath = getattr(r, "module_path", None)
        if mpath is None:
            mpath = Path(str(getattr(r, "path", p) or p))
        rline = getattr(r, "line", None)
        rcol = getattr(r, "column", None)
        snip = ""
        try:
            if Path(str(mpath)) == p and rline and 1 <= rline <= len(src_lines):
                snip = src_lines[rline - 1].strip()
        except Exception:
            pass
        out.append(f"{Path(str(mpath)).name}:{rline}:{(rcol or 0) + 1}"
                   f"{('  — ' + snip) if snip else ''}")
    more = len(refs) - len(out)
    return (f"{len(refs)} reference(s) for the symbol at "
            f"{p.name}:{line}:{col}:\n" + "\n".join(out)
            + (f"\n… and {more} more." if more > 0 else ""))


def _hover(params: dict) -> str:
    jedi = _jedi()
    if jedi is None:
        return ("jedi is not installed — say 'setup code intel' or run: "
                "pip install jedi")
    p, err = _resolve(params)
    if err:
        return err
    text = p.read_text(encoding="utf-8", errors="replace")
    line, col = _line_col(params, text)
    try:
        hovers = jedi.Script(code=text, path=p).infer(line, col)
    except Exception as e:
        return f"hover failed at {p.name}:{line}:{col} ({e})."
    if not hovers:
        return (f"Nothing known about the symbol at {p.name}:{line}:{col} "
                "(unresolved or builtin).")
    out = []
    for h in hovers[:3]:
        kind = h.type
        try:
            sig = h.get_signatures()
            head = str(sig[0]) if sig else h.name
        except Exception:
            head = h.name
        doc = ""
        try:
            doc = (h.docstring(raw=True) or "").strip()
        except Exception:
            pass
        if len(doc) > 500:
            doc = doc[:497] + "…"
        out.append(f"{kind}: {head}" + (f"\n{doc}" if doc else ""))
    return f"Hover at {p.name}:{line}:{col}:\n" + "\n—\n".join(out)


# ── pylsp diagnostics (stdio JSON-RPC, no external client library) ──────────

def _lsp_diagnostics(p: Path, text: str, timeout: float = 8.0) -> str | None:
    """Talks to `python -m pylsp` over stdio. Returns formatted diagnostics
    or None when the server cannot be started (caller gives the hint)."""
    import time
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "pylsp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
    except Exception:
        return None

    def send(msg: dict) -> None:
        body = json.dumps(msg).encode("utf-8")
        assert proc.stdin is not None
        proc.stdin.write(b"Content-Length: " + str(len(body)).encode()
                         + b"\r\n\r\n" + body)
        proc.stdin.flush()

    def read() -> dict | None:
        assert proc.stdout is not None
        length = 0
        while True:
            hdr = proc.stdout.readline()
            if not hdr:
                return None
            hdr = hdr.strip()
            if not hdr:
                break
            if hdr.lower().startswith(b"content-length:"):
                length = int(hdr.split(b":", 1)[1].strip())
        if length <= 0:
            return None
        body = b""
        while len(body) < length:
            chunk = proc.stdout.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"processId": None,
                         "rootUri": p.parent.as_uri(),
                         "capabilities": {}}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = read()
            if msg is not None and msg.get("id") == 1:
                break
        else:
            return None
        send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        send({"jsonrpc": "2.0", "method": "textDocument/didOpen",
              "params": {"textDocument": {
                  "uri": p.as_uri(), "languageId": "python",
                  "version": 1, "text": text}}})
        deadline = time.time() + min(timeout, 5.0)
        while time.time() < deadline:
            msg = read()
            if msg and msg.get("method") == "textDocument/publishDiagnostics":
                diags = (msg.get("params") or {}).get("diagnostics") or []
                send({"jsonrpc": "2.0", "id": 99, "method": "shutdown"})
                send({"jsonrpc": "2.0", "method": "exit"})
                return diags
        return []                              # server up, no findings
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _diag(params: dict) -> str:
    p, err = _resolve(params)
    if err:
        return err
    text = p.read_text(encoding="utf-8", errors="replace")
    diags = _lsp_diagnostics(p, text)
    if diags is None:
        return ("pylsp is not available — say 'setup code intel' or run: "
                "pip install python-lsp-server pyflakes pycodestyle")
    if not diags:
        return f"{p.name}: no diagnostics — file is clean."
    lines = []
    for d in diags[:30]:
        rng = d.get("range", {}) or {}
        start = rng.get("start", {}) or {}
        ln = int(start.get("line", 0)) + 1
        sev = str(d.get("severity", 3))
        tag = {"1": "Error", "2": "Warning"}.get(sev, "Info")
        src = d.get("source") or "pylsp"
        lines.append(f"[{tag}] {p.name}:{ln}: {d.get('message', '')} ({src})")
    errs = sum(1 for d in diags if d.get("severity") == 1)
    return (f"{p.name}: {len(diags)} diagnostic(s), {errs} error(s):\n"
            + "\n".join(lines))


# ── setup (allowlisted pip) ─────────────────────────────────────────────────

def _setup(params: dict) -> str:
    want = ["jedi", "python-lsp-server", "pyflakes", "pycodestyle"]
    unsafe = [w for w in want if w not in _SETUP_SAFE]
    if unsafe:
        return f"Refusing non-allowlisted packages: {unsafe}"
    have_jedi = _jedi() is not None
    try:
        import pylsp  # noqa: F401
        have_lsp = True
    except Exception:
        have_lsp = False
    todo = [w for w, ok in (("jedi", have_jedi),
                            ("python-lsp-server", have_lsp)) if not ok]
    if not todo:
        return "Code intel already installed (jedi + python-lsp-server)."
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", *want],
            capture_output=True, text=True, timeout=300)
    except Exception as e:
        return f"pip failed to run ({e})."
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-400:]
        return f"pip install failed (rc={r.returncode}): {tail}"
    ok_jedi = _jedi() is not None
    try:
        import pylsp  # noqa: F401
        ok_lsp = True
    except Exception:
        ok_lsp = False
    return (f"Setup done — jedi: {'yes' if ok_jedi else 'NO'}, "
            f"pylsp: {'yes' if ok_lsp else 'NO'}."
            if ok_jedi or ok_lsp else
            "pip ran but the imports still fail — check pip's output.")


# ── tool entry ──────────────────────────────────────────────────────────────

def code_intel(parameters: dict | None = None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "hover")).lower().strip()
    if action in ("complete", "completion", "completions"):
        return _complete(params)
    if action in ("goto", "def", "definition"):
        return _goto(params)
    if action in ("refs", "references", "usages", "find_refs"):
        return _refs(params)
    if action in ("diag", "diagnostics", "lint", "check"):
        return _diag(params)
    if action == "setup":
        return _setup(params)
    if action == "symbols":
        try:
            from actions import code_outline
            return code_outline.code_outline(params)
        except Exception as e:
            return f"outline delegation failed ({e})."
    if action in ("hover", "info", "describe"):
        return _hover(params)
    return (f"Unknown code_intel action {action!r} — use one of: hover, "
            "complete, goto, refs, diag, symbols, setup.")


TOOL = {
    "name": "code_intel",
    "description": (
        "IDE-grade code intelligence for Python (jedi in-process + pylsp "
        "diagnostics over stdio). Actions: hover (docstring/signature "
        "inferred at line:col), complete (completions at line:col), goto "
        "(definitions), refs (all references/usages), diag (file "
        "diagnostics via python-lsp-server — syntax + pyflakes + "
        "pycodestyle), symbols (delegates to code_outline for any "
        "language), setup (allowlisted pip install of jedi + pylsp). Use "
        "for 'where is this defined', 'who calls this', 'what's wrong "
        "with this file', 'explain this function'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "hover | complete | goto | refs |"
                                      " diag | symbols | setup."},
            "path": {"type": "STRING",
                     "description": "Source file to inspect."},
            "line": {"type": "STRING",
                     "description": "1-based cursor line (default: EOF)."},
            "col": {"type": "STRING",
                    "description": "1-based cursor column (default: EOL)."},
        },
        "required": [],
    },
    "handler": code_intel,
}
