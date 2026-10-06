"""
rules — when/then automation engine.

Declarative rules: WHEN a trigger fires, THEN run one or more tools. Triggers:
  time   — daily at HH:MM (checked on the app's clock tick)
  file   — a file appears / changes / is removed under a watched folder
  usb    — a block device appears/disappears (Linux/Windows best-effort)
  phrase — fires when the UI relay calls fire_phrase (scene-style voice rules)
  interval — every N minutes/hours/days since the anchor tick
  site   — a URL goes up / down / its head-bytes change (free HTTP probe,
           default every 5m, rate-limited per rule)
  port   — a local TCP port opens or closes (127.0.0.1 connect probe)
  proc   — a process appears or disappears (Linux /proc scan; ps fallback)

Rules live in a JSON file under the user's config dir, survive restarts, and
are executed through an injected runner (main.py passes the action registry).
The tick() function is idempotent and cheap — the app calls it every 30s.

Safety: rules may only call tools through the same runner as everything else,
so destructive steps hit the orchestrator's rules only if the rule explicitly
sets allow_destructive (default false, and the UI never sets it).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from core.action_loader import (RESULT_BLOCKED, RESULT_EMPTY, RESULT_FAILED,
                                classify_result)

_LOCK = threading.Lock()
_RULES: list[dict] = []
_LOADED = False
_RUNNER: Callable[[str, dict], str] | None = None
_NOTIFY: Callable[[str], str] | None = None
_LAST_FIRE: dict[str, float] = {}          # rule-id -> epoch (debounce)
_STATE: dict[str, set] = {}               # rule-id -> file snapshot
_DAY_KEYS: dict[str, str] = {}            # rule-id -> YYYY-MM-DD (daily cap)

# ── self-healing (roadmap: "scheduled NL automations + self-healing retries")
# A rule whose EXECUTION failed gets up to _MAX_HEAL extra attempts with
# exponential backoff, scheduled against the tick clock (tests control `now`).
# On success the incident clears; after _MAX_HEAL failures the rule stops
# retrying but stays visible in `rules action=health` until it next fires
# successfully on its own. Bounded: one entry per rule id.
_MAX_HEAL = 3
_BACKOFF = (30, 120, 600)                 # seconds before attempt 1, 2, 3
_HEALTH: dict[str, dict] = {}             # rule-id -> {fails, retry_at, err, why}


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
        return "Trigger needs a type: time | interval | file | phrase | usb."
    if str(trigger.get("type")) == "interval" and \
            _parse_interval(trigger.get("value")) is None:
        return "Interval needs a value like 30m, 2h, 1d or 1w."
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
    base_id = f"{_mk_id(rule)}#{int(time.time()*1000) % 100000}"
    rid = base_id
    n = 1
    with _LOCK:
        # unique even for rules created in the same millisecond — colliding
        # ids would share trigger state (_STATE) and double-fire or mute
        while any(x.get("id") == rid for x in _RULES):
            n += 1
            rid = f"{base_id}-{n}"
        rule["id"] = rid
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

def _brief(text: str, cap: int = 200) -> str:
    """One line, capped. Firings are announced on a single log line, so a
    tool that returns a paragraph must not turn into a wall of text."""
    one = " ".join(str(text or "").split())
    return one[:cap] + ("…" if len(one) > cap else "")


def _notify_fire(r: dict, why: str, kind: str, text: str) -> None:
    """Tell the user what actually happened — including when nothing did.

    This is the whole point of an unattended rule: nobody is watching the
    tool call, so the notification IS the result. It used to say
    "Rule 'X' fired (time 08:00)." no matter what came back, which meant a
    research digest could find nothing at all and still report success.
    """
    label = r.get("label") or r.get("tool") or "rule"
    head = f"Rule '{label}' ({why})"
    if kind == RESULT_FAILED:
        msg = f"{head} FAILED: {_brief(text)}"
    elif kind == RESULT_BLOCKED:
        msg = f"{head} was BLOCKED: {_brief(text)}"
    elif kind == RESULT_EMPTY:
        msg = f"{head} ran but found nothing: {_brief(text)}"
    elif text.strip():
        msg = f"{head} fired: {_brief(text)}"
    else:
        msg = f"{head} fired."
    if _NOTIFY:
        try:
            _NOTIFY(msg)
        except Exception:
            pass


def _exec_rule(r: dict, why: str) -> tuple[bool, str]:
    """Execute a rule's tool (or every step of a scene) via the injected
    runner. Returns (ok, message). ok=False means an EXECUTION failure
    (exception/refusal) — eligible for the self-heal retry. A missing
    runner returns (False, '') but callers skip health for it (nothing
    ran, nothing to heal). A scene stops at the first failing step —
    same contract as the task orchestrator."""
    if _RUNNER is None:
        print(f"[Rules] fired ({why}) but no runner wired — "
              f"{r.get('tool')} not executed.")
        return False, ""
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
                    return False, msg
                step_text = str(o or "")
                step_kind = classify_result(step_text)
                if step_kind in (RESULT_FAILED, RESULT_BLOCKED):
                    # The docstring above promises a scene stops at the first
                    # failing step. It only ever stopped on an exception, and
                    # a tool reports failure as a STRING — so a scene whose
                    # step was refused ran to the end and called itself done.
                    msg = (f"Scene '{r.get('label')}' stopped at step "
                           f"{done + 1}/{len(steps)} ({tool}) — "
                           f"{'blocked' if step_kind == RESULT_BLOCKED else 'failed'}: "
                           f"{_brief(step_text)}")
                    if _NOTIFY:
                        try:
                            _NOTIFY(msg)
                        except Exception:
                            pass
                    return False, msg
                done += 1
                if step_text:
                    outs.append(step_text)
            msg = (f"Scene '{r.get('label')}' fired ({why}): "
                   f"{done}/{len(steps)} steps done.")
            if _NOTIFY:
                try:
                    _NOTIFY(msg)
                except Exception:
                    pass
            return True, " | ".join(outs + [msg])
        out = _RUNNER(r.get("tool", ""), dict(r.get("args") or {}))
        text = str(out or "")
        kind = classify_result(text)
        _notify_fire(r, why, kind, text)
        return kind not in (RESULT_FAILED, RESULT_BLOCKED), text
    except Exception as e:
        return False, f"Rule '{r.get('label')}' failed: {e}"


def _run_rule(r: dict, why: str) -> str:
    """Back-compat wrapper: message only (phrase path + external callers)."""
    return _exec_rule(r, why)[1]


def _note_health(r: dict, ok: bool, err: str, now: float) -> None:
    """Record a failure (schedule a heal) or clear an incident on success.
    Must be called WITHOUT holding _LOCK (touches _HEALTH only — dict
    ops are atomic enough under the GIL, and tick is single-threaded)."""
    rid = r.get("id") or ""
    if not rid:
        return
    if ok:
        _HEALTH.pop(rid, None)
        return
    h = _HEALTH.get(rid) or {"fails": 0, "retry_at": None,
                             "err": "", "why": ""}
    h["fails"] += 1
    h["err"] = str(err or "")[:200]
    h["why"] = str(r.get("label") or r.get("tool") or rid)[:80]
    if h["fails"] <= _MAX_HEAL:
        h["retry_at"] = now + _BACKOFF[min(h["fails"] - 1,
                                           len(_BACKOFF) - 1)]
    else:
        h["retry_at"] = None              # give up retrying; stay visible
    _HEALTH[rid] = h


def health_report() -> str:
    """Human summary for `rules action=health` — failing rules + when the
    next self-heal attempt runs."""
    if not _HEALTH:
        return "All automation rules healthy — no failures pending."
    lines = []
    for rid, h in sorted(_HEALTH.items(),
                         key=lambda kv: -kv[1]["fails"]):
        if h.get("retry_at"):
            when = time.strftime("%H:%M:%S",
                                 time.localtime(h["retry_at"]))
            state = f"heal retry at {when}"
        else:
            state = (f"gave up after {h['fails']} attempts "
                     f"— will retry on next natural trigger")
        lines.append(f"• {h.get('why', rid)} — {h['fails']} failure(s), "
                     f"{state}. Last error: {h.get('err', '?')[:120]}")
    return "Automation health:\n" + "\n".join(lines)


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


# ── site / port / proc triggers (batch: rules v2) ──────────────────────────
# All three are EDGE-triggered on a state transition with a first-tick
# baseline (same contract as file triggers — never fire on observation #1),
# rate-limited per rule so a 30s tick never hammers the target.

def _site_probe(url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """(reachable, sha1-of-first-64KB). Any HTTP response — even 404/500 —
    means the site is UP; only DNS/conn/timeout failures mean down."""
    import hashlib
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-Rules/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(65536)
    except urllib.error.HTTPError as he:         # server answered → UP
        try:
            data = he.read(65536)
        except Exception:                        # noqa: BLE001
            data = b""
    except Exception:                            # noqa: BLE001
        return False, ""
    return True, hashlib.sha1(data).hexdigest()


def _rate_ok(rid: str, now: float, every: float) -> bool:
    """Per-rule probe interval — checked BEFORE the probe so a 30s tick
    never hammers the target (site) or the local socket (port)."""
    st = _STATE.get(rid)
    if not isinstance(st, dict):
        return True
    return now - float(st.get("probe", 0.0)) >= max(0.0, every)


def _edge_fire(rid: str, now: float, cur_state: str, want: str,
               digest: str | None = None) -> bool:
    """Shared edge-trigger for site/port/proc. `want` is the state that
    fires ('up'/'down'/'changed'/'running'/'gone'); first observation is a
    baseline (never fires) — same contract as file triggers."""
    st = _STATE.get(rid)
    if not isinstance(st, dict):
        st = {}
    st["probe"] = now
    prev = st.get("state")
    if digest is not None:
        prev_hash, st["hash"] = st.get("hash"), digest
    else:
        prev_hash = None
    st["state"] = cur_state
    _STATE[rid] = st
    if prev is None:
        return False                              # baseline tick: no fire
    if want == "changed":
        return (cur_state == "up" and prev == "up"
                and prev_hash is not None and digest != prev_hash)
    return prev != want and cur_state == want     # edge INTO the want-state


def _due_site(r: dict, now: float) -> bool:
    trig = r.get("trigger", {})
    url = str(trig.get("url") or trig.get("value") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    want = str(trig.get("state") or "up").lower()
    if want not in ("up", "down", "changed"):
        want = "up"
    every = _parse_interval(trig.get("every")) or 300
    if not _rate_ok(r["id"], now, every):
        return False
    reachable, digest = _site_probe(url)
    return _edge_fire(r["id"], now,
                      "up" if reachable else "down", want, digest)


def _due_port(r: dict, now: float) -> bool:
    import socket
    trig = r.get("trigger", {})
    raw = str(trig.get("port") or trig.get("value") or "").strip()
    if not raw.isdigit():
        return False
    port = int(raw)
    if not 1 <= port <= 65535:
        return False
    want = str(trig.get("state") or "up").lower()
    if want not in ("up", "down"):
        want = "up"
    every = _parse_interval(trig.get("every")) or 30
    if not _rate_ok(r["id"], now, every):
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.7)
    try:
        sock.connect(("127.0.0.1", port))
        up = True
    except OSError:
        up = False
    finally:
        sock.close()
    return _edge_fire(r["id"], now, "up" if up else "down", want)


def _proc_names() -> set[str]:
    """Lowercased process names right now. Linux: direct /proc scan (no
    subprocess); elsewhere: ps (macOS); last resort: tasklist (Windows)."""
    import os
    names: set[str] = set()
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm", encoding="utf-8",
                          errors="replace") as fh:
                    names.add(fh.read().strip().lower())
            except OSError:
                continue
        if names:
            return names
    except OSError:
        pass
    import subprocess
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ps", "-A", "-o", "comm="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
        else:
            out = subprocess.run(["tasklist", "/FO", "CSV"],
                                 capture_output=True, text=True,
                                 timeout=5).stdout
        for line in out.splitlines():
            if line.strip():
                names.add(line.strip().strip('"').split(",")[0].lower())
    except Exception:                             # noqa: BLE001
        pass
    return names


def _due_proc(r: dict, now: float) -> bool:
    trig = r.get("trigger", {})
    want_name = str(trig.get("value") or trig.get("proc") or "").strip().lower()
    if not want_name:
        return False
    want = str(trig.get("state") or "running").lower()
    if want not in ("running", "gone"):
        want = "running"
    every = _parse_interval(trig.get("every")) or 0   # /proc scan is cheap
    if not _rate_ok(r["id"], now, every):
        return False
    running = any(want_name in n for n in _proc_names())
    return _edge_fire(r["id"], now,
                      "running" if running else "gone", want)


def _parse_interval(value) -> int | None:
    """'30m' | '2h' | '1d' | '1w' | bare minutes → seconds. None = invalid."""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    unit = raw[-1]
    num = raw[:-1] if unit in "mhdw" else raw
    try:
        n = float(num)
    except ValueError:
        return None
    if n <= 0:
        return None
    secs = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit, 60)
    return int(n * secs)


def _due_interval(r: dict, now: float) -> bool:
    """Repeating rule: fires every N seconds since the arm/baseline tick.
    First tick only arms (same contract as file triggers); the anchor
    survives in _STATE while the process runs and in `last_fired` on disk."""
    secs = _parse_interval((r.get("trigger") or {}).get("value"))
    if secs is None:
        return False
    rid = r.get("id")
    state = _STATE.get(rid, r.get("last_fired"))
    if state is None:
        _STATE[rid] = now            # arm: no fire on the very first tick
        r["last_fired"] = now        # …and persist the anchor (restart-safe)
        with _LOCK:
            _save()
        return False
    if now - float(state) >= secs:
        _STATE[rid] = now
        r["last_fired"] = now        # persisted by the caller's _save path
        with _LOCK:
            _save()
        _LAST_FIRE[rid] = now
        return True
    return False


_DUE = {"time": _due_time, "file": _due_file, "usb": lambda r: False,
        "phrase": _due_phrase, "interval": _due_interval,
        "site": _due_site, "port": _due_port, "proc": _due_proc}
_DUE_ARGS = {"time", "interval", "site", "port", "proc"}
# → time needs (r, now, today); interval/site/port/proc need (r, now)


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
            ok, out = _exec_rule(r, f"phrase '{key}'")
            if _RUNNER is not None:
                _note_health(r, ok, out if ok else out, time.time())
            fired.append(out or r.get("label", "rule"))
    return fired


def tick(now: float | None = None) -> list[str]:
    """Evaluate all triggers + self-heal pending retries. Called every
    ~30s by the app; safe to call from tests with a controlled `now`."""
    _load()
    now = now if now is not None else time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    results: list[str] = []
    with _LOCK:
        rules = [r for r in _RULES if r.get("enabled", True)]
        by_id = {r.get("id"): r for r in rules}

    # ── self-heal pass: retry failures whose backoff has elapsed ────────
    # Rule deleted mid-incident? Drop its health entry regardless of
    # retry_at — cleanup must not depend on a pending retry existing.
    for rid in list(_HEALTH):
        if rid not in by_id:
            _HEALTH.pop(rid, None)
    for rid, h in list(_HEALTH.items()):
        if not h.get("retry_at") or now < h["retry_at"]:
            continue
        r = by_id.get(rid)
        if r is None:
            _HEALTH.pop(rid, None)         # rule deleted mid-incident
            continue
        h["retry_at"] = None               # one attempt per due window
        if h["fails"] > _MAX_HEAL:
            _HEALTH.pop(rid, None)
            continue
        ok, out = _exec_rule(r, f"self-heal {h['fails']}/{_MAX_HEAL}")
        if _RUNNER is None:
            continue                       # nothing ran — don't count
        _note_health(r, ok, out, now)
        results.append(out if out else
                       f"self-heal {'ok' if ok else 'failed'}: "
                       f"{h.get('why', rid)}")

    # ── natural trigger pass ────────────────────────────────────────────
    for r in rules:
        t = str(r.get("trigger", {}).get("type", ""))
        fn = _DUE.get(t)
        if fn is None:
            continue
        try:
            if (fn(r, now, today) if t == "time" else
                    (fn(r, now) if t in
                     ("interval", "site", "port", "proc") else fn(r))):
                ok, out = _exec_rule(r, t)
                if _RUNNER is not None:
                    _note_health(r, ok, out, now)
                if out:
                    results.append(out)
        except Exception as e:
            results.append(f"rule error: {e}")
    return results


# ── self-improvement: mine history + task runs → rule suggestions ───────────
# NEVER installs anything — it prints exact `rules action=add` lines the
# user (or the model, after asking) can copy. Thresholds are deliberately
# conservative: 3+ occurrences across 3+ distinct days / a 1-hour cluster
# of 3+ runs,7-day window for history, 14 days for task runs.

_KEYWORD_TOOL = {
    # token in the recurring question → tool + args (kept tiny + honest;
    # unmapped patterns still get reported, just without copy-paste params)
    "weather": ("weather_report", {}),
    "mausam": ("weather_report", {}),
    "battery": ("scan", {"what": "system"}),
    "cpu": ("scan", {"what": "system"}),
    "ram": ("scan", {"what": "system"}),
    "memory": ("scan", {"what": "system"}),
    "scan": ("scan", {}),
    "health": ("scan", {}),
}


def _map_keyword_tool(key: str) -> tuple[str, dict] | tuple[None, None]:
    tokens = set(key.split())
    for kw, (tool, args) in _KEYWORD_TOOL.items():
        if kw in tokens:
            return tool, dict(args)
    return None, None


def _hhmm(hour_float: float) -> str:
    hh = int(hour_float) % 24
    mm = int(round((hour_float % 1) * 60))
    if mm >= 60:
        hh, mm = (hh + 1) % 24, 0
    return f"{hh:02d}:{mm:02d}"


def _existing_time_rule(tool: str, hhmm: str) -> bool:
    """Skip suggesting something the user already has (within 20 min)."""
    try:
        _load()
    except Exception:
        return False
    try:
        a = int(hhmm[:2]) * 60 + int(hhmm[3:])
    except (ValueError, IndexError):
        return False
    with _LOCK:
        rules = list(_RULES)
    for r in rules:
        trig = r.get("trigger") or {}
        if trig.get("type") != "time" or r.get("tool") != tool:
            continue
        v = str(trig.get("value") or "")
        try:
            b = int(v[:2]) * 60 + int(v[3:])
        except (ValueError, IndexError):
            continue
        if abs(a - b) <= 20:
            return True
    return False


def _suggest(now: float | None = None) -> str:
    import re as _re
    from datetime import datetime as _dt

    now = time.time() if now is None else now
    suggestions: list[str] = []

    # ── Detector B: the same tool keeps appearing at the same hour ──
    try:
        from core import taskstore
        runs = taskstore.list_runs(50)
    except Exception:
        runs = []
    hits: dict[str, list[tuple[float, float, dict]]] = {}
    for r in runs:
        ts = float(r.get("created") or 0)
        if ts <= 0 or now - ts > 14 * 86400:
            continue
        dt = _dt.fromtimestamp(ts)
        hour = dt.hour + dt.minute / 60.0
        seen: set[str] = set()
        for step in (r.get("plan") or []):
            tool = str(step.get("tool") or "")
            if not tool or tool in seen or tool == "task_agent":
                continue
            seen.add(tool)
            hits.setdefault(tool, []).append(
                (hour, ts, dict(step.get("args") or {})))
    for tool in sorted(hits):
        items = sorted(hits[tool])
        if len(items) < 3:
            continue
        best = None
        for anchor, _, _ in items:
            cluster = [x for x in items if abs(x[0] - anchor) <= 1.0]
            if len(cluster) >= 3 and (best is None or len(cluster) > len(best)):
                best = cluster
        if not best:
            continue
        hhmm = _hhmm(sorted(h[0] for h in best)[len(best) // 2])
        if _existing_time_rule(tool, hhmm):
            continue
        args = json.dumps(best[0][2], ensure_ascii=False)
        suggestions.append(
            f"• You kept using '{tool}' around {hhmm} "
            f'({len(best)} runs in 14 days) →\n'
            f'    rules action=add trigger_type=time value={hhmm} '
            f'tool={tool} tool_args=\'{args}\'')

    # ── Detector A: the same question asked across different days ──
    try:
        from actions import history_search as _hs
        turns = _hs.recent_user_turns(now - 7 * 86400)
    except Exception:
        turns = []
    groups: dict[str, list[tuple[float, int]]] = {}
    for ts, text in turns:
        norm = _re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())
        key = " ".join(norm.split()[:8])
        if len(key) < 6:
            continue
        groups.setdefault(key, []).append((ts, int(ts // 86400)))
    for key in sorted(groups, key=lambda k: -len({d for _, d in groups[k]}))[:5]:
        entries = groups[key]
        days = {d for _, d in entries}
        if len(days) < 3:
            continue
        tool, targs = _map_keyword_tool(key)
        hours = sorted(
            _dt.fromtimestamp(ts).hour + _dt.fromtimestamp(ts).minute / 60.0
            for ts, _ in entries)
        hhmm = _hhmm(hours[len(hours) // 2])
        if tool and not _existing_time_rule(tool, hhmm):
            args = json.dumps(targs, ensure_ascii=False)
            suggestions.append(
                f'• You asked about "{key.strip()[:60]}" on {len(days)} '
                f'different days (usually ~{hhmm}) →\n'
                f'    rules action=add trigger_type=time value={hhmm} '
                f'tool={tool} tool_args=\'{args}\'')
        elif not tool:
            suggestions.append(
                f'• You asked "{key.strip()[:60]}" on {len(days)} days — '
                f'I can\'t map that to a tool automatically. Pick one: '
                f'rules action=add trigger_type=phrase value=<keyword> '
                f'tool=<tool>')

    if not suggestions:
        return ("No recurring patterns yet — I look at the last 7 days of "
                "conversation and 14 days of task runs, and need at least "
                "3 repeats before suggesting anything.")
    return (f"{len(suggestions)} automation suggestion(s) — NOTHING "
            f"installed:\n" + "\n".join(suggestions) +
            "\nCopy any line above (or say \'add suggestion 1\') to install.")


# ── tool entry ───────────────────────────────────────────────────────────────

def manage_rules(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()

    if action == "add":
        trigger = params.get("trigger") or {
            "type": params.get("trigger_type", "time"),
            "value": params.get("value", ""),
            "path": params.get("path", ""),
        }
        # site/port/proc extras — only carried when given (keeps JSON clean)
        if isinstance(trigger, dict):
            for key in ("state", "every", "url", "port", "proc"):
                if params.get(key) not in (None, ""):
                    trigger.setdefault(key, params.get(key))
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
    elif action == "health":
        result = health_report()
    elif action == "suggest":
        result = _suggest()
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
        "list (default), add (trigger_type: time HH:MM | interval 30m/2h/1d "
        "| file path + value appears/removed/changed | phrase keyword | "
        "site value=URL state=up/down/changed every=5m | port value=8080 "
        "state=up/down | proc value=<process name> state=running/gone; plus "
        "tool + tool_args — OR steps: JSON list of {tool, args} to run in "
        "order as a scene), remove (by number or match), tick (evaluate "
        "now), health (rules that recently failed — self-heal retries them "
        "automatically with backoff). Use when the user wants 'every "
        "morning at 8 do X', 'when a file appears in Downloads do Y', "
        "'when the server/port/process comes up tell me', 'watch this site "
        "and alert me if it goes down', 'when I say Z do W', or a "
        "multi-action routine like 'movie mode'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | add | remove | tick | health | suggest"},
            "trigger_type": {"type": "STRING",
                             "description": "time | interval | file | "
                                            "phrase | usb | site | port | "
                                            "proc"},
            "value": {"type": "STRING",  # time=HH:MM, interval=30m/2h/1d, phrase=keyword
                      "description": "HH:MM for time, interval like 30m/2h, "
                                     "keyword for phrase, appears/removed "
                                     "for file, URL for site, port number "
                                     "for port, process name for proc"},
            "path": {"type": "STRING", "description": "Folder/file path for file trigger"},
            "state": {"type": "STRING",
                      "description": "site: up (default) | down | changed; "
                                     "port: up (default) | down; "
                                     "proc: running (default) | gone — the "
                                     "edge that fires the rule"},
            "every": {"type": "STRING",
                      "description": "Probe rate limit, e.g. 30s/5m (site "
                                     "default 5m, port default 30s)"},
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
