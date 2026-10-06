#!/usr/bin/env python3
"""Audit the `except: pass` handlers — which ones are hiding something.

    python tools/silent_except_audit.py            # summary + the top offenders
    python tools/silent_except_audit.py --list     # every handler, one per line
    python tools/silent_except_audit.py --count    # just the number (for a gate)
    python tools/silent_except_audit.py --risky    # only the ones worth a look

WHY
    Not every silent handler is a bug. This codebase says so itself, in comments:
    "pythonw / redirected pipes — never fatal", "a resize during teardown", "the
    audio device disappeared mid-session". A cleanup path that raises a second
    exception while handling the first is worse than the first failure.

    The problem is the other kind: a handler that swallows a failure in a place
    where the feature is *supposed to work*, so nothing appears in any log and
    the user reports "the button does nothing". Those are indistinguishable from
    the good ones by reading a diff — you have to look at all of them, and there
    are 300.

    So this classifies instead of judging:

      C — commented, or followed by a comment explaining the silence (fine)
      R — the handler is named as a risky one: no log, no comment, and the
          enclosing function looks like a feature (not a teardown/probe)
      S — a cleanup/teardown/probe path by name (stop, close, shutdown, probe,
          teardown, __del__, atexit, check, is_*, _try_*, _probe)

    It is a report, not a gate: a script cannot tell whether silence is correct.
    The number it prints is the one that should go down, and `--count` exists so
    CI can hold it there.
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", ".git", "build", "dist", "__pycache__", "node_modules"}
SKIP_FILES = {"test_", "silent_except_audit"}

#: Function-name fragments where swallowing is usually the point.
_CLEANUP = ("close", "stop", "shutdown", "teardown", "cleanup", "reset",
            "cancel", "abort", "release", "unregister", "unlink",
            "probe", "check", "is_", "try_", "maybe_", "safe_", "best_",
            "fallback", "escape", "on_timeout", "__del__", "atexit", "log")

#: Statements that count as "did nothing about it".
SILENT_BODIES = ("pass", "continue", "break")

RISK_HIGH = "high"      # a feature path with nothing recording the failure
RISK_LOW = "low"        # teardown/probe by name
RISK_COMMENTED = "comment"
RISK_LOGGED = "logged"


@dataclass
class Finding:
    path: str
    line: int
    function: str
    handler: str            # the exception types caught
    risk: str
    body: str
    why: str = ""


def _func_name(node: ast.AST, parents: dict) -> str:
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        if isinstance(cur, ast.ClassDef):
            return f"{cur.name}.{_func_name(cur, parents)}"
        cur = parents.get(id(cur))
    return "<module>"


def _parents(tree: ast.AST) -> dict:
    out: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


#: Return values that throw the failure away: nothing, or an empty stand-in.
#: A return that *reports* — an f-string carrying `{e}`, a call to a logger —
#: is the function's normal error contract, and a return of a considered
#: default is a decision the code made on purpose. Counting either would make
#: this report useless by inflating it with correct code.
_EMPTY_LITERALS = (ast.Dict, ast.List, ast.Tuple, ast.Set, ast.DictComp,
                   ast.ListComp)


def _reports(stmt: ast.stmt) -> bool:
    """True if this statement tells someone what happened."""
    text = ast.unparse(stmt)
    return any(token in text for token in
               ("log.", "logging.", "logger", "warn", "print(", "traceback",
                "raise", "{e}", "{err}", "{exc}", "{error}"))


def _is_silent(body: list[ast.stmt]) -> tuple[bool, str]:
    """True if the handler does nothing with the exception."""
    if len(body) != 1:
        return False, ""
    stmt = body[0]
    if isinstance(stmt, ast.Pass):
        return True, "pass"
    if isinstance(stmt, ast.Continue):
        return True, "continue"
    if isinstance(stmt, ast.Break):
        return True, "break"
    if isinstance(stmt, ast.Raise):
        return False, ""                 # re-raising is not silence
    if isinstance(stmt, ast.Return):
        if stmt.value is None:
            return True, "return None"
        if _reports(stmt):
            return False, ""
        if isinstance(stmt.value, ast.Constant):
            # `return ""` / `return 0` / `return False` throw the failure away;
            # `return "system default"` is the code saying what to do instead
            return (True, "return (empty)") if not stmt.value.value else (False, "")
        if isinstance(stmt.value, (ast.Tuple, ast.List, ast.Set)):
            # `return []` throws the failure away; `return ["default"]` does not.
            # Checked structurally: literal_eval chokes on an f-string element,
            # and a report tool must never crash on the code it is describing.
            if not stmt.value.elts:
                return True, f"return {ast.unparse(stmt.value)}"
            return False, ""
        if isinstance(stmt.value, ast.Dict):
            if not stmt.value.keys:
                return True, "return {}"
            return False, ""
        if isinstance(stmt.value, (ast.DictComp, ast.ListComp, ast.SetComp,
                                   ast.GeneratorExp)):
            return False, ""             # a comprehension produced a decision
        return False, ""                 # a meaningful value is a decision
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True, "docstring only"
    if _reports(stmt):
        return False, ""
    return False, ""


def _commented(source_lines: list[str], line: int, span: int = 6) -> bool:
    """A comment in the handler or just above it is how this repo documents
    deliberate silence."""
    lo = max(0, line - span)
    for text in source_lines[lo:line + span]:
        stripped = text.strip()
        if stripped.startswith("#") and len(stripped) > 3:
            return True
    return False


def scan_file(path: Path, source: str | None = None) -> list[Finding]:
    src = source if source is not None else path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    parents = _parents(tree)
    out: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        silent, body_text = _is_silent(node.body)
        if not silent:
            continue
        name = _func_name(node, parents)
        handler = ast.unparse(node.type) if node.type is not None else "Bare except"
        if _commented(lines, node.lineno):
            risk, why = RISK_COMMENTED, "a comment explains the silence"
        elif any(frag in name.lower() for frag in _CLEANUP):
            risk, why = RISK_LOW, "teardown/probe-shaped function"
        else:
            risk, why = RISK_HIGH, "no log, no comment, feature-shaped function"
        out.append(Finding(str(path.relative_to(ROOT)), node.lineno, name,
                           handler, risk, body_text, why))
    return out


def scan(risky_only: bool = False) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if any(path.name.startswith(skip) for skip in SKIP_FILES):
            continue
        try:
            findings += scan_file(path)
        except Exception as e:          # a report tool must never fail the scan
            print(f"  (skipped {path.relative_to(ROOT)}: {e})", file=sys.stderr)
    if risky_only:
        findings = [f for f in findings if f.risk == RISK_HIGH]
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="one line per handler")
    ap.add_argument("--count", action="store_true",
                    help="print only the number of high-risk handlers")
    ap.add_argument("--risky", action="store_true",
                    help="only handlers with no log and no comment")
    args = ap.parse_args(argv)

    findings = scan()
    high = [f for f in findings if f.risk == RISK_HIGH]

    if args.count:
        print(len(high))
        return 0

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.risk] = counts.get(f.risk, 0) + 1

    print(f"silent exception handlers: {len(findings)}")
    for risk in (RISK_COMMENTED, RISK_LOW, RISK_HIGH):
        print(f"  {risk:<9} {counts.get(risk, 0)}")

    by_file: dict[str, int] = {}
    for f in high:
        by_file[f.path] = by_file.get(f.path, 0) + 1

    show = high if (args.list or args.risky) else high[:20]
    if not args.risky:
        print("\ntop files by high-risk count:")
        for path, n in sorted(by_file.items(), key=lambda kv: -kv[1])[:12]:
            print(f"  {n:3d}  {path}")

    print(f"\n{'high-risk handlers' if args.risky else 'first 20 high-risk handlers'}:")
    for f in show:
        print(f"  {f.path}:{f.line}  {f.function}()  except {f.handler}: {f.body}")

    if len(high) > len(show):
        print(f"\n  … {len(high) - len(show)} more (--list for all)")
    print("\nA comment saying why the silence is correct moves a handler out of "
          "the high-risk list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
