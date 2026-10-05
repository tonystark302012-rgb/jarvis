# core/autonomy.py
"""Autonomy modes — how much the agent may do WITHOUT asking.

Three modes, one choke point:

    observe   read-only agent: mutating tools are refused honestly at the
              interface (main._execute_tool) before anything runs.
    ask       TODAY'S behaviour exactly: existing confirm gates apply,
              destructive agent steps need confirmation. This is the
              default — installing this module changes nothing until the
              user opts in.
    auto      unattended operation: the INTERFACE (never the model) injects
              confirm/allow_destructive for a small UNDOABLE set, so agent
              runs can proceed. Genuinely irreversible tools are never
              enhanced — they keep their own unforgeable UI gate
              (core/confirm.request), which autonomy does not touch.

Trust model (mirrors core/confirm): the mode lives in config/api_keys.json
and is only changed through the `autonomy` action or the UI — a model tool
call CAN reach that action like any other, but the model writing a param on
some other tool can never escalate anything (enhancement happens in
_execute_tool, which the model does not control).

TTL: modes can expire ("auto for 60 minutes"); an expired TTL falls back to
`ask` — checked on every read, so a run's later steps degrade safely.
"""
from __future__ import annotations

import json
import time

# ── classification tables ───────────────────────────────────────────────

#: Tools NEVER enhanced/allowed unattended, whatever the mode says.
NEVER_AUTO_TOOLS = frozenset({
    "shutdown_jarvis",   # powering off is not an undo
    "send_message",      # a sent message is gone
    "vault",             # credential writes
    "macro",             # replay types/clicks blind
})

#: Arg-level never-auto (same idea, scoped to the dangerous action only).
NEVER_AUTO_ARGS: dict[str, set[str]] = {
    "computer_settings": {"shutdown", "restart", "reboot", "suspend",
                           "sleep", "hibernate", "lock"},
    "procman": {"kill", "end"},
}

#: Tools whose every invocation counts as mutating (observe blocks).
MUTATING_TOOLS = frozenset({
    "send_message",
    "terminal",          # running a command mutates by definition
    "macro",             # record + replay
})

#: Tools that SELF-GATE in observe mode instead of being blocked outright:
#: their handlers return a plan PREVIEW (task_agent) or a single-frame
#: preview (gui_agent) when observe is on. mutating() returns False for
#: them so the interface lets the handler decide.
SELF_GATING = frozenset({"task_agent", "gui_agent"})

#: Arg-level mutating additions. Everything in orchestrator.DESTRUCTIVE /
#: _DESTRUCTIVE_ARGS is mutating too (imported, not duplicated).
_POWER_ACTIONS = frozenset({
    "shutdown", "restart", "reboot", "suspend", "sleep", "hibernate",
    "lock", "type_text", "write_on_screen", "type", "write",
})

MUTATING_ARGS: dict[str, set[str]] = {
    "desktop_control": {"clean"},
    "mcp": {"call", "add", "remove"},     # external side effects / config
    "smart_home": {"pub", "publish", "send"},
    "vault": {"set", "del", "delete"},
    "rules": {"add", "remove"},
    "computer_settings": set(_POWER_ACTIONS),
}

#: Auto-enhance: (tool) → params injected at the interface when mode=auto.
#: Every member is undo-protected (undo journal) or agent-policy scoped.
ENHANCE_TOOLS = frozenset({
    "task_agent",        # allow_destructive=True (orchestrator still applies
                         # its own DESTRUCTIVE table + never-auto at step level)
    "gui_agent",         # confirm=yes — step budget + cancel still apply
})

#: git verbs that are local + revertible (auto may inject confirm for these).
#: push is deliberately absent: the remote copy is not undoable from here.
_AUTO_GIT_OK = frozenset({"add", "commit", "checkout", "branch", "stash",
                          "restore", "tag"})

MODES = ("observe", "ask", "auto")


# ── state ───────────────────────────────────────────────────────────────

def _path():
    from config import get_base_dir
    return get_base_dir() / "config" / "api_keys.json"


def _load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                 encoding="utf-8")


def get_mode(now: float | None = None) -> str:
    """Current mode; expired TTL falls back to 'ask'. Never raises."""
    try:
        data = _load()
        mode = str(data.get("autonomy_mode") or "ask").lower().strip()
        if mode not in MODES:
            mode = "ask"
        exp = data.get("autonomy_expires")
        if exp is not None and (now if now is not None else time.time()) \
                >= float(exp):
            return "ask"
        return mode
    except Exception:
        return "ask"


def set_mode(mode: str, minutes: float | None = None,
             now: float | None = None) -> dict:
    """Persist a mode. minutes=None → no expiry. Returns the state."""
    mode = str(mode or "").lower().strip()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    data = _load()
    data["autonomy_mode"] = mode
    if minutes is None:
        data.pop("autonomy_expires", None)
    else:
        data["autonomy_expires"] = (now if now is not None else time.time()) \
            + max(0.1, float(minutes)) * 60.0
    _save(data)
    return status(now=now)


def status(now: float | None = None) -> dict:
    data = _load()
    exp = data.get("autonomy_expires")
    return {
        "mode": get_mode(now),
        "expires": (None if exp is None else float(exp)),
        "configured": str(data.get("autonomy_mode") or "ask"),
    }


# ── classification ──────────────────────────────────────────────────────

def _never_auto(tool: str, args: dict | None) -> bool:
    if tool in NEVER_AUTO_TOOLS:
        return True
    bad = NEVER_AUTO_ARGS.get(tool)
    if bad:
        val = str((args or {}).get("action", "")).lower().strip()
        if val in bad:
            return True
    return False


def mutating(tool: str, args: dict | None = None) -> bool:
    """True if this call changes state (observe mode blocks it).
    Self-gating tools (task_agent/gui_agent) return False — their handlers
    implement observe→preview themselves."""
    args = args or {}
    if tool in SELF_GATING:
        return False
    # native MCP tools act on EXTERNAL systems — always mutating
    if tool.startswith("mcp__"):
        return True
    # whole-tool mutating
    if tool in MUTATING_TOOLS:
        return True
    # orchestrator's table (single source of truth for destructive scope)
    try:
        from core import orchestrator
        if orchestrator.is_destructive(tool, args):
            return True
    except Exception:
        pass
    # arg-level table
    action = str(args.get("action") or "").lower().strip()
    bad = MUTATING_ARGS.get(tool)
    if bad and action and action in bad:
        return True
    # file ops: any non-read action of file_controller
    if tool == "file_controller" and action and action not in {
            "list", "read", "head", "tail", "search", "find", "info",
            "exists", "stat"}:
        return True
    return False


def gate(tool: str, args: dict | None = None, mode: str | None = None) -> str | None:
    """Observe-mode block. Returns an honest refusal or None (proceed)."""
    m = mode or get_mode()
    if m != "observe":
        return None
    if not mutating(tool, args):
        return None
    return (f"Autonomy mode is OBSERVE (read-only) — '{tool}' would change "
            f"state, so I didn't run it. Say 'autonomy auto' (or 'ask') to "
            f"allow actions again.")


def enhancing(tool: str, args: dict | None, mode: str | None = None) -> dict:
    """Auto-mode interface enhancement. Returns args (copy) with
    allow/confirm injected ONLY for the UNDOABLE set. ask/observe → args
    unchanged. NEVER_AUTO is never enhanced."""
    args = dict(args or {})
    m = mode or get_mode()
    if m != "auto":
        return args
    if _never_auto(tool, args):
        return args
    if tool in ENHANCE_TOOLS:
        if tool == "task_agent":
            args["allow_destructive"] = True
        elif tool == "gui_agent":
            args["confirm"] = "yes"
        return args
    if tool == "terminal":
        # only local, revertible git writes; push/force stay gated
        cmd = str(args.get("command") or "").strip()
        parts = cmd.split()
        if len(parts) >= 2 and parts[0] == "git" and \
                parts[1] in _AUTO_GIT_OK:
            args["confirm"] = "yes"
    return args
