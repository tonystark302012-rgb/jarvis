# actions/terminal.py
"""terminal — sandboxed terminal + git assistant.

WHAT IT DOES
    `run`   — execute ONE allowlisted command as argv (no shell ever),
              confined to the workspace, with a timeout and output cap.
    `git`   — git assistant: read ops (status/log/diff/show/blame/branch)
              always allowed; write ops (add/commit/push/checkout/…) need
              confirm=yes and stay inside the repo.
    `help`  — what's allowed.

SANDBOX RULES (all enforced, this is the "safe agentic terminal" from
the roadmap — not a shell in a trenchcoat):
    * NO shell: subprocess gets a list; shlex parses quoting only.
    * NO metacharacters: & | ; < > ` $ \\ \n are refused even as args —
      so even if someone later swaps to shell=True, nothing works.
    * head-token allowlist for `run`: interpreters/tools (python, npm,
      make, git, pytest, pip, ruff, node, …) — same philosophy as
      dev_agent._validate_run_command, extended with CLI verbs.
    * cwd confinement: every command runs inside the repo/base dir; any
      argv token that resolves outside (../, /etc, ~) is refused for
      path-taking verbs, and git must be a repo with .git visible from cwd.
    * timeout (default 60 s, hard 300) + stdout+stderr cap (64 KB) —
      a runaway process is killed, not awaited.
    * git write ops gated behind confirm=yes (voice-safe: "commit karo"
      is confirmable by the caller; the tool never auto-pushes).

FREE: stdlib subprocess only. Works offline; git is already required
by the repo.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

# ── allowlists ───────────────────────────────────────────────────────────────

_RUN_ALLOWED = frozenset({
    "python", "python3", "py", "pip", "pip3", "pytest", "ruff",
    "node", "npm", "npx", "yarn", "pnpm", "make", "gcc", "g++",
    "cargo", "go", "java", "ruby", "php", "deno", "tsc",
    "git", "ls", "cat", "head", "tail", "wc", "grep", "find",
    "rg", "jq", "curl", "wget", "docker", "sqlite3", "duckdb",
})

# git verbs that can change state → confirm=yes required
_GIT_WRITE = frozenset({
    "add", "commit", "push", "pull", "merge", "rebase", "checkout",
    "switch", "restore", "reset", "cherry-pick", "tag", "mv", "rm",
    "init", "clone", "stash", "branch", "remote", "clean",
})
# dangerous even WITH confirm (destructive, no undo in this tool)
_GIT_REFUSED = frozenset({"filter-branch", "push--force", "am"})

_BAD_CHARS = re.compile(r"[&|;<>`$\n\r\\]")
_TIMEOUT_HARD = 300
_OUT_CAP = 64_000


def _base_dir() -> Path:
    from config import get_base_dir
    return Path(get_base_dir()).resolve()


def _workspace() -> Path:
    """Commands run here: the git checkout when present, else base dir."""
    cwd = Path.cwd().resolve()
    for probe in (cwd, *_base_dir().parents, cwd, _base_dir()):
        try:
            if (probe / ".git").exists():
                return probe
        except OSError:
            continue
    return cwd


def _refuse(msg: str) -> str:
    return f"Terminal refused: {msg}"


def _parse(command: str) -> list[str] | None:
    """Safe argv or None. No metacharacters, no shell, ever."""
    if not command or not command.strip():
        return None
    if _BAD_CHARS.search(command):
        return None
    try:
        parts = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return None
    if not parts:
        return None
    # refuse absolute/parent paths that escape when used as plain tokens
    for tok in parts[1:]:
        if tok.startswith("~"):
            return None
        if tok.startswith("/") and not tok.startswith(("/tmp", "/dev/null")):
            # absolute paths: only inside workspace or /tmp
            try:
                Path(tok).resolve().relative_to(_base_dir())
            except ValueError:
                try:
                    Path(tok).resolve().relative_to("/tmp")
                except ValueError:
                    return None
        if tok == ".." or tok.startswith("../"):
            return None
    return parts


def _run_argv(parts: list[str], timeout: int, cwd: Path) -> str:
    head = Path(parts[0]).name.lower()
    if head.endswith(".exe"):
        head = head[:-4]
    if head == "python" or head == "python3" or head == "py":
        parts = [sys.executable, *parts[1:]]
    try:
        proc = subprocess.run(
            parts, cwd=str(cwd), timeout=max(1, min(timeout, _TIMEOUT_HARD)),
            capture_output=True, text=True, errors="replace",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                 "GIT_PAGER": "cat", "PAGER": "cat"},
        )
    except subprocess.TimeoutExpired:
        return f"⏱ Timed out after {min(timeout, _TIMEOUT_HARD)}s: {shlex.join(parts)}"
    except FileNotFoundError:
        return _refuse(f"{parts[0]!r} is not installed on this machine.")
    except Exception as e:
        return _refuse(f"could not start: {e}")
    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr)
                                 if proc.stderr and proc.stdout else
                                 (proc.stderr or ""))
    out = out.strip() or "(no output)"
    if len(out) > _OUT_CAP:
        out = out[:_OUT_CAP] + "\n…(output truncated)"
    return f"$ {shlex.join(parts)}\n[exit {proc.returncode}]\n{out}"


def _git_check(cwd: Path) -> str | None:
    if not (cwd / ".git").exists():
        # walk up: cwd may be a subdir of the repo
        for parent in cwd.parents:
            if (parent / ".git").exists():
                return None
        return _refuse("not inside a git repository.")
    return None


def terminal(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()
    cwd = _workspace()

    if action in ("help", "allowed", ""):
        if action == "" and not params.get("command"):
            pass                              # fall through to help text
        else:
            if action:
                return ("Allowlisted runners: python/pytest/ruff/pip, "
                        "node/npm/npx, make/gcc/cargo/go, git, and read-only "
                        "text tools (ls cat head tail wc grep rg jq). "
                        "No shell operators; cwd = repo; timeout + output "
                        "cap enforced. git write ops need confirm=yes.")
    command = str(params.get("command") or "").strip()
    if not command:
        return ("Give me a command — e.g. terminal command='pytest -q', "
                "or terminal action=git command='log --oneline -5'.")

    # git mode: everything routes through the git rules
    parts = _parse(command)
    if parts is None:
        return _refuse("shell metacharacters/unbalanced quotes/escaping "
                       "tokens are not allowed.")
    head = Path(parts[0]).name.lower()

    if action == "git" or head == "git":
        if head != "git":
            parts = ["git", *parts]
        err = _git_check(cwd)
        if err:
            return err
        verb = parts[1] if len(parts) > 1 else "status"
        if verb in _GIT_REFUSED or verb.startswith("filter"):
            return _refuse(f"git {verb} is on the permanent deny list.")
        confirm = str(params.get("confirm") or "").lower() in ("1", "true", "yes")
        if verb in _GIT_WRITE and not confirm:
            return (f"'git {verb}' changes state — re-run with "
                    f"confirm=yes if that's what you meant.")
        # push force specifically: never via this tool
        if verb == "push" and any(a in ("-f", "--force") for a in parts):
            return _refuse("force-push is not permitted from voice.")
        if verb in ("log", "diff", "show", "status", "blame", "branch",
                    "remote", "stash", "describe", "shortlog", "rev-parse",
                    "ls-files", "grep", "var", "help"):
            # make pagers/tty behave
            if verb == "log" and not any(a.startswith("-") and
                                         "oneline" in a or a == "--stat"
                                         for a in parts[1:]):
                parts = parts[:2] + ["--no-color"] + parts[2:]
        timeout = int(params.get("timeout") or 30)
        return _run_argv(parts, timeout, cwd)

    # generic allowlisted run
    if head not in _RUN_ALLOWED:
        return _refuse(f"{head!r} is not allowlisted. Allowed: "
                       + ", ".join(sorted(_RUN_ALLOWED)[:18]) + ", …")
    timeout = int(params.get("timeout") or 60)
    return _run_argv(parts, timeout, cwd)


TOOL = {
    "name": "terminal",
    "description": (
        "Sandboxed terminal and git assistant. command= runs ONE "
        "allowlisted tool (python, pytest, ruff, npm, make, git, ls, "
        "grep, …) as argv — no shell operators, repo-confined, timeout "
        "+ output cap. Use action=git for git verbs: read ops "
        "(status/log/diff/show/blame) are free; write ops (add/commit/"
        "push/…) require confirm=yes; force-push refused. Use for 'run "
        "the tests', 'git log dikhao', 'commit karo', 'pipeline chalao'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "command": {"type": "STRING",
                        "description": "Command string, e.g. 'pytest -q'"},
            "action": {"type": "STRING",
                       "description": "git (default when cmd starts with git) | help"},
            "confirm": {"type": "STRING",
                        "description": "yes to allow state-changing git verbs"},
            "timeout": {"type": "INTEGER", "description": "Seconds — default 60"},
        },
        "required": ["command"],
    },
    "handler": terminal,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return terminal(params,
                    player=(ctx or {}).get("player"),
                    session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(terminal({"command": "git status"}))
