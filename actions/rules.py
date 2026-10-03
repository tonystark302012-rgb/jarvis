"""
rules — when/then automation engine.

Declarative rules: WHEN a trigger fires, THEN run one or more tools. Triggers:
  time   — daily at HH:MM (checked on the app's clock tick)
  file   — a file appears / changes / is removed under a watched folder
  usb    — a block device appears/disappears (Linux/Windows best-effort)
  phrase — fires when the UI relay calls fire_phrase (scene-style voice rules)

Rules live in a JSON file under the user's config dir, survive restarts, and
are executed through an injected runner (main.py passes the action registry).
The tick() function is idempotent and cheap — the app calls it every 30s.

Safety: rules may only call tools through the same runner as everything else,
so destructive steps hit the orchestrator's rules only if the rule explicitly
sets allow_destructive (default false, and the UI never sets it).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

_LOCK = threading.Lock()
_RULES: list[dict] = []
_LOADED = False
_RUNNER: Callable[[str, dict], str] | None = None
_NOTIFY: Callable[[str], str] | None = None
_LAST_FIRE: dict[str, float] = {}          # rule-id -> epoch (debounce)
_STATE: dict[str, set] = {}                # rule-id -> file snapshot
_DAY_KEYS: dict[str, str] = {}             # rule-id -> YYYY-MM-DD (daily cap)


def _path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "automation_rules.json"


def set_runner(fn) -> None:
    global _RUNNER
    _RUNNER = fn


def set_notifier(fn) -> None:
    """fn(text) -> reply; used to announce rule firings conversationally."""
    global _NOTIFY
    _NOTIFY = fn


def _load() -> None:
    global _LOADED
    with _LOCK:
        if _LOADED:
            return
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            if isinstance(data, list):
                _RULES.extend(r for r in data if isinstance(r, dict))
        except Exception:
            pass
        _LOADED = True


def _save() -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_RULES, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[Rules] could not save rules: {e}")


def _mk_id(rule: dict) -> str:
    trig = rule.get("trigger", {})
    return f"{trig.get('type','?')}:{trig.get('value','')}:{trig.get('path','')}"


def add_rule(trigger: dict, tool: str, args: dict | None = None,
             label: str = "", steps: list | None = None) -> str:
    """Single-tool rule when `steps` is absent; a scene (ordered tool calls)
    when present. `tool` may be empty for scenes — label or first step names it."""
    _load()
    if not isinstance(trigger, dict) or not trigger.get("type"):
        return "Trigger needs a type: time | file | phrase | usb."
    clean_steps: list[dict] = []
    if isinstance(steps, list):
        for st in steps:
            if isinstance(st, dict) and st.get("tool"):
                clean_steps.append({"tool": str(st["tool"]),
                                    "args": dict(st.get("args") or {})})
    if not clean_steps and not tool:
        return "A rule must name a tool to run."
    rule = {
        "id": "",
        "label": (label or tool or
                  (clean_steps[0]["tool"] if clean_steps else "scene"))[:80],
        "trigger": trigger,
        "tool": tool or (clean_steps[0]["tool"] if clean_steps else ""),
        "args": dict(args or {}),
        "enabled": True,
        "created": time.strftime("%Y-%m-%d %H:%M"),
    }
    if clean_steps:
        rule["steps"] = clean_steps
    rule["id"] = f"{_mk_id(rule)}#{int(time.time()*1000) % 100000}"
    with _LOCK:
        _RULES.append(rule)
        _save()
    kind = f"scene({len(clean_steps)} steps)" if clean_steps else tool
    return (f"Rule added: WHEN {trigger.get('type')} "
            f"{trigger.get('value') or trigger.get('path','')} → {kind}.")


def remove_rule(which: str) -> str:
    _load()
    which = str(which or "").strip().lower()
    with _LOCK:
        before = len(_RULES)
        if which.isdigit():
            idx = int(which) - 1
            if 0 <= idx < len(_RULES):
                removed = _RULES.pop(idx)
                _save()
                return f"Removed rule: {removed.get('label')}"
            return f"No rule #{which}."
        _RULES[:] = [r for r in _RULES
                     if which not in str(r.get("id", "")).lower()
                     and which not in str(r.get("label", "")).lower()]
        removed_n = before - len(_RULES)
        if removed_n:
            _save()
            return f"Removed {removed_n} rule(s)."
    return f"No rule matching {which!r}."


def list_rules() -> str:
    _load()
    with _LOCK:
        rules = list(_RULES)
    if not rules:
        return ("No automation rules. Example: WHEN time 08:00 THEN "
                "weather_report {city: Jaipur}.")
    lines = []
    for i, r in enumerate(rules, 1):
        t = r.get("trigger", {})
        state = "" if r.get("enabled", True) else " [off]"
        steps = r.get("steps")
        if isinstance(steps, list) and steps:
            then = (f"scene({len(steps)} steps): " +
                    " → ".join(str(s.get("tool")) for s in steps))
        else:
            then = f"{r.get('tool')} {r.get('args', {})}"
        lines.append(f"{i}. {r.get('label')}{state} — "
                     f"WHEN {t.get('type')} {t.get('value') or t.get('path','')} "
                     f"→ {then}")
    return "Automation rules:\n" + "\n".join(lines)


# ── trigger evaluation ───────────────────────────────────────────────────────

def _run_rule(r: dict, why: str) -> str:
    """Execute a rule's tool (or every step of a scene) via the injected
    runner. Returns '' if no runner. A scene runs its steps in order and
    stops at the first failure — same contract as the task orchestrator."""
    if _RUNNER is None:
        print(f"[Rules] fired ({why}) but no runner wired — "
              f"{r.get('tool')} not executed.")
        return ""
    try:
        steps = r.get("steps")
        if isinstance(steps, list) and steps:
            outs, done = [], 0
            for st in steps:
                if not isinstance(st, dict):
                    continue
                tool = str(st.get("tool") or "")
                if not tool:
                    continue
                try:
                    o = _RUNNER(tool, dict(st.get("args") or {}))
                except Exception as se:
                    msg = (f"Scene '{r.get('label')}' failed at step "
                           f"{done + 1} ({tool}): {se}")
                    if _NOTIFY:
                        try:
                            _NOTIFY(msg)
                        except Exception:
                            pass
                    return msg
                done += 1
                if o:
                    outs.append(str(o))
            msg = (f"Scene '{r.get('label')}' fired ({why}): "
                   f"{done}/{len(steps)} steps done.")
            if _NOTIFY:
                try:
                    _NOTIFY(msg)
                except Exception:
                    pass
            return " | ".join(outs + [msg])
        out = _RUNNER(r.get("tool", ""), dict(r.get("args") or {}))
        msg = f"Rule '{r.get('label')}' fired ({why})."
        if _NOTIFY:
            try:
                _NOTIFY(msg)
            except Exception:
                pass
        return str(out or "")
    except Exception as e:
        return f"Rule '{r.get('label')}' failed: {e}"


def _due_time(r: dict, now: float, today: str) -> bool:
    val = str(r.get("trigger", {}).get("value", "")).strip()  # HH:MM
    if not val:
        return False
    lt = time.localtime(now)
    hhmm = time.strftime("%H:%M", lt)
    # fire within the tick window (30-60s ticks), once per day
    if hhmm != val and not (val.count(":") == 1 and hhmm[:5] == val[:5]):
        return False
    if _DAY_KEYS.get(r["id"]) == today:
        return False
    _DAY_KEYS[r["id"]] = today
    return True


def _files_snapshot(path: Path) -> set:
    try:
        if path.is_file():
            return {path.name} if path.exists() else set()
        if path.is_dir():
            return {p.name for p in path.iterdir() if p.is_file()}
    except Exception:
        pass
    return set()


def _due_file(r: dict) -> bool:
    trig = r.get("trigger", {})
    raw = str(trig.get("path", "")).strip()
    if not raw:
        return False
    mode = str(trig.get("value", "appears")).lower()
    p = Path(raw).expanduser()
    now = _files_snapshot(p)
    prev = _STATE.get(r["id"])
    if prev is None:
        _STATE[r["id"]] = now            # first tick: baseline, no fire
        return False
    if mode == "appears":
        added = now - prev
        _STATE[r["id"]] = now
        if added:
            _LAST_FIRE[r["id"]] = time.time()
            r.setdefault("_last_files", sorted(added)[:5])
            return True
    elif mode == "removed":
        gone = prev - now
        _STATE[r["id"]] = now
        if gone:
            return True
    else:  # changed — newest mtime moves
        _STATE[r["id"]] = now
        if now != prev:
            return True
    return False


def _due_phrase(r: dict) -> bool:
    # phrases fire only through fire_phrase(), never on tick
    return False


_DUE = {"time": _due_time, "file": _due_file, "usb": lambda r: False,
        "phrase": _due_phrase}


def fire_phrase(text: str) -> list[str]:
    """Scene/voice trigger: run every enabled phrase rule whose keyword
    appears in the user's utterance. Called by main.py on each text/voice turn."""
    _load()
    text_l = str(text or "").lower()
    if not text_l:
        return []
    fired = []
    with _LOCK:
        rules = [r for r in _RULES if r.get("enabled", True)
                 and str(r.get("trigger", {}).get("type")) == "phrase"]
    for r in rules:
        key = str(r.get("trigger", {}).get("value", "")).lower().strip()
        if key and key in text_l:
            out = _run_rule(r, f"phrase '{key}'")
            fired.append(out or r.get("label", "rule"))
    return fired


def tick(now: float | None = None) -> list[str]:
    """Evaluate all triggers. Called every ~30s by the app; safe to call
    from tests with a controlled `now`."""
    _load()
    now = now if now is not None else time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    results: list[str] = []
    with _LOCK:
        rules = [r for r in _RULES if r.get("enabled", True)]
    for r in rules:
        t = str(r.get("trigger", {}).get("type", ""))
        fn = _DUE.get(t)
        if fn is None:
            continue
        try:
            if fn(r, now, today) if t == "time" else fn(r):
                out = _run_rule(r, t)
                if out:
                    results.append(out)
        except Exception as e:
            results.append(f"rule error: {e}")
    return results


# ── tool entry ───────────────────────────────────────────────────────────────

def manage_rules(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()

    if action == "add":
        trigger = params.get("trigger") or {
            "type": params.get("trigger_type", "time"),
            "value": params.get("value", ""),
            "path": params.get("path", ""),
        }
        tool = str(params.get("tool", "")).strip()
        args = params.get("tool_args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        steps = params.get("steps")
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except ValueError:
                steps = None
        result = add_rule(trigger if isinstance(trigger, dict) else {},
                          tool, args, str(params.get("label", "")),
                          steps=steps if isinstance(steps, list) else None)
    elif action == "remove":
        result = remove_rule(str(params.get("which", "")))
    elif action == "tick":
        fired = tick()
        result = "\n".join(fired) if fired else "No rules due."
    else:
        result = list_rules()

    if player is not None and action == "list":
        try:
            player.show_content("AUTOMATION RULES", result[:4000])
        except Exception:
            pass
    return result


TOOL = {
    "name": "rules",
    "description": (
        "When/then automation rules that run tools on triggers. Actions: "
        "list (default), add (trigger_type: time HH:MM | file path + value "
        "appears/removed/changed | phrase keyword; plus tool + tool_args — "
        "OR steps: JSON list of {tool, args} to run in order as a scene), "
        "remove (by number or match), tick (evaluate now). Use when the user "
        "wants 'every morning at 8 do X', 'when a file appears in Downloads "
        "do Y', 'when I say Z do W', or a multi-action routine like "
        "'movie mode' (open player, dim lights, silence notifications)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | add | remove | tick"},
            "trigger_type": {"type": "STRING", "description": "time | file | phrase"},
            "value": {"type": "STRING",
                      "description": "HH:MM for time, keyword for phrase, appears/removed for file"},
            "path": {"type": "STRING", "description": "Folder/file path for file trigger"},
            "tool": {"type": "STRING", "description": "Tool to run when triggered"},
            "tool_args": {"type": "STRING", "description": "JSON args for the tool"},
            "steps": {"type": "STRING",
                      "description": "JSON list of {tool, args} — ordered scene steps; "
                                    "runs instead of tool/tool_args when present"},
            "label": {"type": "STRING", "description": "Human name for the rule"},
            "which": {"type": "STRING", "description": "Rule number or text for remove"},
        },
        "required": [],
    },
    "handler": manage_rules,
}
