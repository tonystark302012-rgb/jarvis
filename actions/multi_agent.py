# actions/multi_agent.py
"""multi_agent — planner → coder → tester pipeline with a dry-run diff.

Three LLM roles collaborate on one task, and NOTHING touches disk until
the tester has passed the result and the caller asked for apply:

  1. PLANNER  — task + file inventory → JSON plan (steps + files to
                create/modify, with per-file instructions).
  2. CODER    — one file at a time: current content + instruction →
                full new content.  Bound by a byte budget so a rogue
                reply cannot balloon a 40-line file into a novel.
  3. TESTER   — sees the UNIFIED DIFF (not the coder's self-assessment)
                and must return {"verdict": "pass"|"fail", "issues": [...]}.
                Deterministic gates run first: a Python file that does
                not compile can never be "pass" regardless of the LLM.

A failed verdict (or a syntax error) sends the file back to the coder ONCE
with the tester's issues as feedback — one bounded revision round, then
the pipeline reports honestly instead of looping forever.

Dry-run is the DEFAULT: the result is a unified diff + tester verdict.
apply=true writes the passing files.

Every LLM touchpoint goes through one injectable `llm` callable, so the
whole pipeline is unit-testable with a scripted fake — the roles are
prompt plumbing, the diff/gate/apply logic is what must be proven.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

# Hard ceiling on coder output per file (bytes).  A file bigger than
# this either already exists at scale or the model went off the rails —
# either way the pipeline refuses it rather than trusting the reply.
_MAX_NEW_BYTES = 200_000

# Feedback rounds per file after the first attempt.
_MAX_REVISIONS = 1


def _parse_plan(raw: str) -> dict:
    """PLANNER reply → {"steps": [...], "files": [{path, action, instruction}]}.

    Raises ValueError on anything unstructured — a plan without files is
    not a plan this pipeline can execute.
    """
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        raise ValueError("no JSON object in planner reply")
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid plan JSON: {e}") from e
    if not isinstance(d, dict):
        raise ValueError("plan is not an object")
    steps = [str(s) for s in (d.get("steps") or []) if str(s).strip()]
    files = []
    for f in d.get("files") or []:
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip()
        if not path:
            continue
        action = str(f.get("action") or "modify").lower().strip()
        if action not in ("create", "modify"):
            action = "modify"
        files.append({
            "path": path,
            "action": action,
            "instruction": str(f.get("instruction") or f.get("description") or "")[:2000],
        })
    if not files:
        raise ValueError("plan lists no files to work on")
    return {"steps": steps or ["implement planned files"], "files": files}


def _strip_fences(text: str) -> str:
    """Drop a single surrounding markdown code fence from coder output."""
    t = str(text or "").strip()
    m = re.match(r"^```[A-Za-z0-9_+-]*\n(.*)\n```$", t, re.S)
    return (m.group(1) if m else t).strip() + "\n"


def _make_diff(old: str, new: str, path: str) -> str:
    """Unified diff — empty string when nothing changed."""
    if old == new:
        return ""
    lines = difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(lines)


def _syntax_check(code: str, path: str) -> str | None:
    """Deterministic gate before any LLM verdict. Returns an error string
    or None when the code is acceptable. Only Python is checked — for
    other languages the LLM tester is the only judge, and we say so."""
    if not str(path).endswith(".py"):
        return None
    try:
        compile(code, path, "exec")
        return None
    except SyntaxError as e:
        return f"SyntaxError line {e.lineno}: {e.msg}"


def _parse_verdict(raw: str) -> dict:
    """TESTER reply → {"verdict": "pass"|"fail", "issues": [...]}. Unknown
    verdicts count as fail — the safe direction."""
    m = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not m:
        return {"verdict": "fail", "issues": ["tester reply was not JSON"]}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"verdict": "fail", "issues": ["tester reply was not valid JSON"]}
    if not isinstance(d, dict):
        return {"verdict": "fail", "issues": ["tester reply was not an object"]}
    verdict = str(d.get("verdict") or "").lower().strip()
    if verdict not in ("pass", "fail"):
        verdict = "fail"
    issues = [str(i) for i in (d.get("issues") or []) if str(i).strip()]
    if verdict == "fail" and not issues:
        issues = ["tester rejected without specifics"]
    return {"verdict": verdict, "issues": issues}


def run_pipeline(task: str, files: dict[str, str], llm, *, apply: bool = False,
                 root: Path | None = None, log=None) -> str:
    """Run planner→coder→tester over `task`.

    files   — {relative_path: current_content} inventory given to the
              planner ("" for files that do not exist yet).
    llm     — callable(prompt, system=None) -> str. Injected so tests
              script every role; production passes core.llm_client.
    apply   — False (default) = dry-run: diffs + verdicts only.
    root    — where apply=true writes (default: cwd / None → error).

    Returns a human report: plan, per-file diff, tester verdicts.
    """
    if not str(task or "").strip():
        return "multi_agent needs a task description."
    if not callable(llm):
        return "multi_agent: no LLM available."

    # ── 1. PLAN ──
    inventory = "\n".join(
        f"- {p} ({'exists, ' + str(len(c)) + ' bytes' if c else 'new file'})"
        for p, c in sorted(files.items())
    ) or "(no files given — planner must propose new files)"
    plan_prompt = (
        "You are the PLANNER in a planner→coder→tester pipeline.\n"
        f"TASK: {task}\n\nFILE INVENTORY:\n{inventory}\n\n"
        "Reply with ONLY one JSON object:\n"
        '{"steps": ["<ordered step>", ...], "files": ['
        '{"path": "<relative path>", "action": "create"|"modify", '
        '"instruction": "<what the coder must do to this file>"}, ...]}\n'
        "List ONLY files this task actually needs."
    )
    try:
        plan = _parse_plan(llm(plan_prompt, system="Output exactly one JSON object."))
    except Exception as e:
        return f"multi_agent: planning failed — {e}"
    if log:
        log(f"plan: {len(plan['files'])} file(s), {len(plan['steps'])} step(s)")

    # ── 2+3. CODE → TEST per file ──
    results: list[dict] = []
    for f in plan["files"]:
        path = f["path"]
        old = files.get(path)
        if old is None:
            # planned file outside the given inventory → treat as new
            old = ""
        content = old
        verdict: dict = {"verdict": "fail",
                         "issues": ["coder produced no output"]}
        syntax_err: str | None = None
        attempts = 1 + _MAX_REVISIONS
        feedback = ""
        for attempt in range(1, attempts + 1):
            # ── CODE ──
            code_prompt = (
                "You are the CODER in a planner→coder→tester pipeline.\n"
                f"TASK: {task}\nFILE: {path} ({'modify' if old else 'create'})\n"
                f"INSTRUCTION: {f['instruction']}\n"
                + (f"CURRENT CONTENT:\n```\n{old}\n```\n" if old else "")
                + ("TESTER ISSUES FROM PREVIOUS ATTEMPT:\n- "
                   + "\n- ".join(feedback) + "\n" if feedback else "")
                + "\nReply with ONLY the complete new file content, no commentary."
            )
            try:
                new = _strip_fences(llm(
                    code_prompt,
                    system="Output only file content."))
            except Exception as e:
                verdict = {"verdict": "fail", "issues": [f"coder failed: {e}"]}
                break
            if len(new.encode("utf-8", "ignore")) > _MAX_NEW_BYTES:
                verdict = {"verdict": "fail",
                           "issues": [f"coder output exceeds {_MAX_NEW_BYTES} bytes"]}
                break
            if new.strip() == old.strip():
                verdict = {"verdict": "fail",
                           "issues": ["coder returned the file unchanged"]}
                break
            content = new

            # ── TEST (deterministic gate first) ──
            syntax_err = _syntax_check(content, path)
            if syntax_err:
                verdict = {"verdict": "fail",
                           "issues": [syntax_err, "(auto-rejected before review)"]}
                if log:
                    log(f"{path}: {syntax_err} (attempt {attempt})")
                feedback = [syntax_err]
                continue     # revision round

            test_prompt = (
                "You are the TESTER in a planner→coder→tester pipeline.\n"
                f"TASK: {task}\nFILE: {path}\n\nUNIFIED DIFF:\n"
                f"{_make_diff(old, content, path)}\n\n"
                "Judge ONLY the diff against the task. Reply with ONLY:\n"
                '{"verdict": "pass"|"fail", "issues": ["<concrete problem>", ...]}\n'
                'pass = correct, safe, complete for the stated task.'
            )
            try:
                verdict = _parse_verdict(llm(
                    test_prompt, system="Output exactly one JSON object."))
            except Exception as e:
                verdict = {"verdict": "fail", "issues": [f"tester failed: {e}"]}
            if verdict["verdict"] == "pass":
                break
            if log:
                log(f"{path}: tester fail — {verdict['issues'][:1]}")
            feedback = verdict["issues"][:6]

        diff = _make_diff(old, content, path)
        results.append({"path": path, "diff": diff, "old": old, "new": content,
                        "verdict": verdict, "syntax": syntax_err})

    # ── REPORT ──
    passed = [r for r in results if r["verdict"]["verdict"] == "pass"]
    failed = [r for r in results if r["verdict"]["verdict"] != "pass"]
    out: list[str] = [f"PLAN — {len(plan['steps'])} step(s):"]
    out += [f"  {i}. {s}" for i, s in enumerate(plan["steps"], 1)]
    for r in results:
        tag = "PASS" if r["verdict"]["verdict"] == "pass" else "FAIL"
        out.append(f"\n[{tag}] {r['path']}")
        if r["verdict"]["issues"]:
            for issue in r["verdict"]["issues"][:5]:
                out.append(f"  - {issue}")
        if r["diff"]:
            diff_lines = r["diff"].rstrip("\n").splitlines()
            if len(diff_lines) > 60:
                diff_lines = diff_lines[:60] + [f"  ... ({len(diff_lines) - 60} more diff lines)"]
            out.append("  " + "\n  ".join(diff_lines))
        elif r["verdict"]["verdict"] == "pass":
            out.append("  (no changes)")
    if not apply:
        out.append("\nDRY-RUN: nothing written. Pass apply=true to write "
                   f"the {len(passed)} passing file(s).")
    elif failed:
        out.append(f"\nNOT APPLIED: {len(failed)} file(s) failed testing — "
                   "fix the issues above and retry.")
    else:
        if root is None:
            root = Path.cwd()
        written = []
        try:
            for r in passed:
                if not r["diff"]:
                    continue
                dest = (root / r["path"]).resolve()
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(r["new"], encoding="utf-8")
                written.append(r["path"])
        except Exception as e:
            return ("\n".join(out) +
                    f"\n\nAPPLY FAILED after {len(written)} file(s): {e}")
        out.append(f"\nAPPLIED: wrote {len(written)} file(s): "
                   + (", ".join(written) if written else "none (no changes)"))
    return "\n".join(out)


def _default_llm(prompt: str, system: str | None = None) -> str:
    from core.llm_client import call_llm_text
    return call_llm_text(prompt, system=system, timeout=120)


def multi_agent(parameters: dict = None, response=None, player=None,
                session_memory=None, speak=None) -> str:
    """Handler — parameters:
        task    : what to build/change (required)
        files   : {relative_path: content} inventory (optional; a single
                  file_path+content pair also works)
        apply   : false (default) = dry-run diff; true = write passes
        root    : base directory for apply (default: cwd)
    """
    p = parameters or {}
    task = str(p.get("task") or p.get("description") or "").strip()
    if not task:
        return ("multi_agent needs a task — e.g. "
                "task='add retry to the fetch helper'.")

    files_in = p.get("files")
    if not isinstance(files_in, dict):
        files_in = {}
    fp = str(p.get("file_path") or "").strip()
    if fp and not files_in:
        content = str(p.get("content") or p.get("code") or "")
        files_in = {fp: content}
    # normalise values to str
    files = {str(k): (v if isinstance(v, str) else str(v or ""))
             for k, v in files_in.items()}

    apply_raw = p.get("apply", False)
    if isinstance(apply_raw, str):
        apply = apply_raw.strip().lower() in ("true", "1", "yes", "on")
    else:
        apply = bool(apply_raw)
    root_raw = p.get("root")
    root = Path(str(root_raw)).expanduser() if root_raw else None

    if player is not None:
        try:
            player.write_log(f"[multi_agent] task: {task[:60]}")
        except Exception:
            pass
    return run_pipeline(task, files, _default_llm, apply=apply, root=root,
                        log=(lambda m: player.write_log(f"[multi_agent] {m[:70]}"))
                        if player is not None else None)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "multi_agent",
    "description": ("Planner→coder→tester pipeline for code changes. Three LLM "
                    "roles: PLANNER proposes files+steps, CODER rewrites each "
                    "file, TESTER reviews the unified diff (Python files must "
                    "also compile — a deterministic gate the LLM cannot talk "
                    "past). One bounded revision round on failure. Default is "
                    "DRY-RUN: returns plan + diffs + verdicts, writes nothing; "
                    "apply=true writes only the files that passed testing."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task": {
                "type": "STRING",
                "description": "What to build or change, e.g. 'add exponential retry to fetch()'."
            },
            "files": {
                "type": "OBJECT",
                "description": "Inventory of {relative_path: current_content}. Use \"\" for new files. Omit to send a single file_path instead."
            },
            "file_path": {
                "type": "STRING",
                "description": "Single-file shorthand: path of the file to work on."
            },
            "content": {
                "type": "STRING",
                "description": "Single-file shorthand: current content of file_path (\"\" for a new file)."
            },
            "apply": {
                "type": "BOOLEAN",
                "description": "false (default) = dry-run diff only; true = write files that passed the tester."
            },
            "root": {
                "type": "STRING",
                "description": "Base directory for apply=true writes (default: cwd)."
            }
        },
        "required": ["task"]
    },
    "handler": multi_agent,
}
