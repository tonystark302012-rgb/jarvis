"""
Action discovery, validation, and dispatch — the built-in twin of plugin_loader.

Every actions/*.py that exposes a module-level ``TOOL`` dict is auto-discovered
here, exactly like a drop-in plugin, so main.py never has to hardcode a tool
declaration or a dispatch branch for it. Adding a new bundled action is then the
same one-file operation as writing a plugin: define ``TOOL`` and a handler.

``TOOL`` shape (see actions/open_app.py for a live example):

    TOOL = {
        "name":        "open_app",              # unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
        "description":  "...",                   # what Gemini reads to route the call
        "parameters":  {"type": "OBJECT", ...}, # Gemini function-declaration schema
        "handler":      open_app,                # the callable to run
    }

The handler is invoked through signature introspection: it receives ``parameters``
plus whichever of ``player`` / ``speak`` / ``response`` / ``session_memory`` it
actually declares — so existing action signatures work unchanged.

Discovery runs once at startup; import errors, validation errors, and name
collisions are logged and the offending file is skipped — they NEVER raise out
of discover_actions() and never abort the scan of the remaining files.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}
_CTX_KEYS = ("player", "speak", "response", "session_memory")

# ── result classification — the ONE place that reads a tool's verdict ────────
# A tool never raises out of `run()`: a failure comes back as a STRING. So
# every caller that has to know whether a call worked reads the same text.
# That test used to be copy-pasted into `run()` (for the audit chain) and
# `main._agent_runner` (for the Mission Control timeline) — and the automation
# engine had no copy at all, which is why a rule whose tool answered
# "denied: …" or "No results found for: …" was filed as a success and never
# reached the user. One function, four verdicts, no third copy.
#
# Nothing here is guessed from a tool's own prose. The markers are (a) the
# shapes this module emits, (b) the honest prefixes the tool layer already
# uses, and (c) the explicit "I ran and found nothing" shapes listed below.
# `terminal` is safe to classify because it wraps output as
# "$ cmd\n[exit N]\n…" rather than handing back raw stdout.
RESULT_OK = "ok"
RESULT_EMPTY = "empty"        # ran fine, honestly found nothing
RESULT_BLOCKED = "blocked"    # a policy layer said no (autonomy mode)
RESULT_FAILED = "failed"      # did not work

_FAILED_PREFIXES = ("Action '", "Tool '", "error:", "denied:", "refused:",
                    "unknown tool:")
_FAILED_MARKERS = ("failed:", "not available")
_BLOCKED_PREFIXES = ("Autonomy mode is OBSERVE",)

#: "I ran and there was nothing there" — the shapes bundled tools actually
#: return: "No results found for: X" (web_search), "No files found."
#: (file_controller), "No Steam games found." (game_updater), "No references
#: found from x:1:1." (code_intel). The regex is anchored at the start on
#: purpose — a tool that reports "Saved. No errors found." is a success, and
#: matching keywords alone would file it as empty.
_EMPTY_RE = re.compile(r"^\s*(?:no|zero)\b[^.!?\n]{0,40}?\bfound\b", re.I)
_EMPTY_MARKERS = ("nothing found", "no matches", "0 results", "none found")

_CLASSIFY_HEAD = 120


def classify_result(text: object) -> str:
    """Map a handler's return value to RESULT_* — never raises.

    Order matters: a blocked call is reported as blocked (not failed), and an
    empty result is only considered after the failure shapes, so a tool that
    says "search failed:" is a failure rather than an empty result.
    """
    t = "" if text is None else str(text)
    if not t.strip():
        return RESULT_EMPTY
    if _looks_failed(t):
        return RESULT_FAILED
    if any(t.startswith(p) for p in _BLOCKED_PREFIXES):
        return RESULT_BLOCKED
    head = t[:_CLASSIFY_HEAD]
    if _EMPTY_RE.match(head):
        return RESULT_EMPTY
    low = head.lower()
    if any(m in low for m in _EMPTY_MARKERS):
        return RESULT_EMPTY
    return RESULT_OK


def _looks_failed(text: str) -> bool:
    """The predicate as it shipped, kept byte-compatible on purpose — the
    audit chain and the timeline already depend on exactly these windows."""
    return (("failed:" in text[:70]) or
            text.startswith("Action '") or
            text.startswith("Tool '") or
            ("not available" in text[:80]) or
            any(text.startswith(p) for p in _FAILED_PREFIXES))


# A tool may declare that the model should NOT be held up waiting for it.
# `behavior` goes to the API with the declaration; `scheduling` decides when the
# eventual result is allowed back into the conversation:
#   WHEN_IDLE  — wait for a gap in the speech (the sane default)
#   SILENT     — record it, do not prompt a reply (the tool already announced)
#   INTERRUPT  — cut in immediately (only when the answer cannot wait)
_BEHAVIORS = ("BLOCKING", "NON_BLOCKING")
_SCHEDULING = ("WHEN_IDLE", "SILENT", "INTERRUPT")


def _opt_upper(value, allowed: tuple[str, ...]) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in allowed else None


@dataclass
class ActionRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    handler: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    behavior: Optional[str] = None     # None = the API's default (blocking)
    scheduling: Optional[str] = None   # None = the API's default (WHEN_IDLE)


class ActionRegistry:
    def __init__(self, actions: dict[str, ActionRecord], logger: Callable[[str], None]):
        self._actions = actions          # name -> ActionRecord, VALID entries only
        self._all_records: list[ActionRecord] = []
        self._logger = logger

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        out = []
        for rec in self._actions.values():
            decl = {"name": rec.name, "description": rec.description,
                    "parameters": rec.parameters}
            if rec.behavior:
                decl["behavior"] = rec.behavior
            out.append(decl)
        return out

    def has(self, name: str) -> bool:
        return name in self._actions

    def scheduling(self, name: str) -> Optional[str]:
        """How this action's result should re-enter the conversation, if it said."""
        rec = self._actions.get(name)
        return rec.scheduling if rec else None

    def names(self) -> set[str]:
        return set(self._actions.keys())

    # -- called by main.py from _execute_tool --
    def run(self, name: str, parameters: dict, ctx: dict | None = None) -> str:
        """Single dispatch choke point — every caller (model tool-call,
        orchestrator step, rule firing, palette run) lands here, so this
        is also where the tamper-evident audit chain is written."""
        rec = self._actions.get(name)
        if rec is None or not rec.valid:
            _audit(name, parameters, "unavailable",
                   rec.error if rec else "unknown action")
            return f"Action '{name}' is not available."
        try:
            out = _call_handler(rec.handler, parameters, ctx or {}) or "Done."
        except Exception as e:
            self._logger(f"Action '{name}' crashed during run(): {e}")
            traceback.print_exc()
            _audit(name, parameters, "error", str(e))
            return f"Tool '{name}' failed: {e}"
        text = out if isinstance(out, str) else str(out)
        kind = classify_result(text)
        failed = kind == RESULT_FAILED
        _audit(name, parameters,
               {"failed": "failed", "blocked": "blocked"}.get(kind, "ok"),
               text[:200] if failed else "")
        return out


def _audit(name: str, parameters: dict, status: str, detail: str = "") -> None:
    """Never raises — the audit trail cannot break the call it records."""
    try:
        from core import audit_chain
        audit_chain.record(name, parameters, status=status, detail=detail)
    except Exception:
        pass


def _call_handler(fn: Callable, parameters: dict, ctx: dict) -> str:
    """Invoke the handler passing only the context kwargs it actually declares
    (or all of them if it has **kwargs), so each action's existing signature
    works unchanged."""
    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    kwargs = {}
    for key in _CTX_KEYS:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)
    return fn(parameters=parameters, **kwargs)


def _validate(module, filename: str) -> ActionRecord:
    """Returns an ActionRecord; .valid=False + .error set on any problem. Never raises."""
    tool = getattr(module, "TOOL", None)
    if not isinstance(tool, dict):
        return ActionRecord(name=Path(filename).stem, file=filename,
                            error="No module-level TOOL dict (not a discoverable action).")

    name = tool.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return ActionRecord(name=str(name or Path(filename).stem), file=filename,
                            error="TOOL['name'] missing or not a valid identifier.")

    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        return ActionRecord(name=name, file=filename,
                            error="TOOL['description'] missing or empty.")

    parameters = tool.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return ActionRecord(name=name, file=filename,
                            error="TOOL['parameters'] must be a dict with \"type\": \"OBJECT\".")

    handler = tool.get("handler")
    if not callable(handler):
        return ActionRecord(name=name, file=filename,
                            error="TOOL['handler'] missing or not callable.")

    return ActionRecord(name=name, description=description.strip(), parameters=parameters,
                        handler=handler, file=filename, valid=True, error="",
                        behavior=_opt_upper(tool.get("behavior"), _BEHAVIORS),
                        scheduling=_opt_upper(tool.get("scheduling"), _SCHEDULING))


def discover_actions(actions_dir: Path, reserved_names: set[str] | None = None,
                     logger: Callable[[str], None] = print) -> ActionRegistry:
    """
    Scans actions_dir for *.py files (skips files starting with '_'). A file is
    only treated as an action if it exposes a module-level TOOL dict; files
    without one (shared helpers, capture-only modules) are silently ignored.
    Import/validation errors and name collisions are logged and the file is
    skipped — they NEVER raise out of this function.
    """
    reserved = reserved_names or set()
    actions_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, ActionRecord] = {}
    all_records: list[ActionRecord] = []

    files = sorted(actions_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"actions.{path.stem}"
            # Reuse the already-imported module when present so handlers are the
            # same objects the rest of the app holds.
            module = sys.modules.get(module_name)
            if module is None:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError("could not build import spec")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(module_name, None)
                    raise

            if getattr(module, "TOOL", None) is None:
                continue   # not an action file — a helper/capture-only module

            rec = _validate(module, path.name)

            if rec.valid and rec.name in reserved:
                rec = ActionRecord(name=rec.name, file=path.name,
                                   error=f"Name '{rec.name}' collides with a reserved core tool — rejected.")
            elif rec.valid and rec.name in valid:
                other = valid[rec.name].file
                rec = ActionRecord(name=rec.name, file=path.name,
                                   error=f"Name '{rec.name}' already used by action '{other}' — rejected.")

        except Exception as e:
            rec = ActionRecord(name=path.stem, file=path.name,
                               error=f"Failed to load: {e}")
            traceback.print_exc()

        all_records.append(rec)
        if rec.valid:
            valid[rec.name] = rec
            logger(f"Action loaded: {rec.name} ({path.name})")
        else:
            # Only log a rejection if the file actually tried to be an action.
            logger(f"Action rejected: {path.name} — {rec.error}")

    registry = ActionRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Action discovery complete: {len(valid)} active.")
    return registry
