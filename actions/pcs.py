# actions/pcs.py
"""pc — JARVIS owner's voice surface for the Dots' computers (§3.4).

Every call runs with actor='owner' and lands in computer_audit, so
human takeover is distinguishable from agent activity (T8):

    pc action=create dot=Researcher perms="files,shell"
    pc action=start dot=Researcher
    pc action=exec dot=Researcher argv="ls -la"        (shlex → argv list)
    pc action=files_read dot=Researcher path=notes.md
    pc action=browser dot=Researcher op=navigate url=https://example.com
    pc action=audit dot=Researcher

Duplicate-check: `terminal` (owner's own machine, allowlisted) and
`computer_control` (owner's desktop/windows) are UNCHANGED — a Dot
computer is an isolated per-Dot workspace (dir jail + 30/60 s exec +
toggles), not the host machine.
"""
from __future__ import annotations

import shlex

_ACTIONS = ("create", "show", "list", "start", "stop", "perms", "exec",
            "files_list", "files_read", "files_write", "browser",
            "audit", "delete")


def _resolve_comp(params: dict):
    """pc=<id> or dot=<name|#id> → computer row (or honest error)."""
    from dots import computer, store
    raw_pc = str(params.get("pc") or params.get("computer") or "").strip()
    if raw_pc.isdigit():
        return computer.get(int(raw_pc)), None
    raw_dot = str(params.get("dot") or params.get("name") or "").strip()
    if not raw_dot:
        known = computer.list_computers()
        if not known:
            return None, ("No Dot computers yet — create one: "
                          "pc action=create dot=…")
        return known[0], None
    if raw_dot.isdigit():
        d = store.get_dot(int(raw_dot))
    else:
        d = store.find_dot_by_name(raw_dot)
    if d is None:
        return None, f"No Dot named {raw_dot!r}."
    comp = computer.for_dot(d["id"])
    if comp is None:
        return None, (f"Dot '{d['name']}' has no computer — "
                      f"pc action=create dot={d['name']}")
    return comp, None


def _parse_perms(raw) -> dict:
    if raw is None:
        return {}
    out = {}
    for part in str(raw).replace(",", " ").split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip().lower() in (
                "1", "true", "on", "yes")
        else:
            out[part.strip().lower()] = True
    return out


def pc(parameters: dict = None, player=None, session_memory=None) -> str:
    import json
    params = parameters or {}
    action = str(params.get("action") or "list").lower().strip()
    from dots import computer

    if action not in _ACTIONS:
        return (f"Unknown pc action {action!r}. Known: "
                f"{', '.join(_ACTIONS)}.")

    if action == "create":
        from dots import store
        raw_dot = str(params.get("dot") or params.get("name") or "").strip()
        if not raw_dot:
            return "pc action=create needs dot=… (which Dot gets it?)"
        d = (store.get_dot(int(raw_dot)) if raw_dot.isdigit()
             else store.find_dot_by_name(raw_dot))
        if d is None:
            return f"No Dot named {raw_dot!r}."
        try:
            comp = computer.create_for_dot(d["id"],
                                           _parse_perms(params.get("perms")))
        except (ValueError, KeyError) as e:
            return str(e)
        return (f"Computer #{comp['id']} ready for Dot '{d['name']}' "
                f"(work: {comp['work']}). Start it: pc action=start "
                f"dot={d['name']}")

    if action == "list":
        comps = computer.list_computers()
        if not comps:
            return ("No Dot computers yet — pc action=create dot=…")
        lines = []
        for c in comps:
            from dots import store
            name = (store.get_dot(c["dot_id"]) or {}).get("name", "?")
            on = ", ".join(k for k, v in sorted(c["perms"].items()) if v) \
                or "none"
            lines.append(f"#{c['id']} for {name} — {c['status']} "
                         f"[perms: {on}]")
        return "\n".join(lines)

    comp, err = _resolve_comp(params)
    if err:
        return err

    if action == "show":
        from dots import store
        name = (store.get_dot(comp["dot_id"]) or {}).get("name", "?")
        return (f"Computer #{comp['id']} for Dot '{name}'\n"
                f"status: {comp['status']}\n"
                f"perms: {json.dumps(comp['perms'])}\n"
                f"work: {comp['work']}\nprofile: {comp['profile']}")

    if action == "start":
        c = computer.start(comp["id"], actor="owner")
        return f"Computer #{c['id']} started (running)."

    if action == "stop":
        c = computer.stop(comp["id"], actor="owner")
        return f"Computer #{c['id']} stopped — files and profile persist."

    if action == "delete":
        if computer.delete(comp["id"], actor="owner"):
            return f"Computer #{comp['id']} deleted (workspace removed)."
        return f"No computer #{comp['id']}."

    if action == "perms":
        p = _parse_perms(params.get("perms") or params.get("value"))
        if not p:
            return ('pc action=perms needs e.g. perms="browser=true,'
                    'shell=false"')
        try:
            c = computer.set_perms(comp["id"], p, actor="owner")
        except (KeyError, ValueError) as e:
            return str(e)
        return f"Computer #{c['id']} perms: {json.dumps(c['perms'])}"

    if action == "exec":
        raw = str(params.get("argv") or params.get("message") or "").strip()
        if not raw:
            return "pc action=exec needs argv=\"…\" (the command to run)"
        argv = shlex.split(raw)          # quoting only — NEVER a shell
        res = computer.shell(comp, argv, params.get("timeout"),
                             actor="owner")
        if "error" in res:
            return f"exec: {res['error']}"
        out = (res.get("stdout") or "").strip() or "(no output)"
        return (f"[rc {res['returncode']}, {res['timeout']}s cap]\n"
                f"{out[:4000]}")

    if action == "files_list":
        res = computer.files_list(comp,
                                  str(params.get("path") or ""),
                                  actor="owner")
        if "error" in res:
            return res["error"]
        return "\n".join(res["entries"]) or "(empty directory)"

    if action == "files_read":
        path = str(params.get("path") or "").strip()
        if not path:
            return "pc action=files_read needs path=…"
        res = computer.files_read(comp, path, actor="owner")
        if "error" in res:
            return res["error"]
        note = "… [truncated]" if res.get("truncated") else ""
        return res["content"][:4000] + note

    if action == "files_write":
        path = str(params.get("path") or "").strip()
        if not path:
            return "pc action=files_write needs path=…"
        content = str(params.get("content") or "")
        res = computer.files_write(comp, path, content,
                                   append=bool(params.get("append")),
                                   actor="owner")
        if "error" in res:
            return res["error"]
        return f"Wrote {res['bytes']} bytes to {path}."

    if action == "browser":
        op = str(params.get("op") or "").strip().lower()
        if not op:
            return ("pc action=browser needs op= (navigate | read | "
                    "snapshot | screenshot | click | type | key | scroll)")
        kw = {k: v for k, v in params.items()
              if k in ("url", "target", "text", "key", "direction",
                       "amount")}
        res = computer.browser(comp, op, actor="owner", **kw)
        if "error" in res:
            return f"browser: {res['error']}"
        if op == "snapshot":
            els = "\n".join(
                f"[{i}] {e.get('sel')} — {e.get('text')}"
                for i, e in enumerate(res.get("elements") or []))
            return (f"{res.get('title')} — {res.get('url')}\n"
                    f"{(res.get('text') or '')[:1500]}\n"
                    f"Elements:\n{els}")
        if op in ("navigate", "read"):
            txt = res.get("text") or ""
            return (f"{res.get('title')} — {res.get('url')}"
                    + (f"\n{txt[:2000]}" if txt else ""))
        if op == "screenshot":
            return f"Screenshot saved: {res.get('path')}"
        return json.dumps(res) if not isinstance(res, str) else res

    # action == "audit"
    rows = computer.audit_rows(comp["id"],
                               int(params.get("which") or 15))
    if not rows:
        return f"Computer #{comp['id']}: no audit entries yet."
    return "\n".join(
        f"#{r['id']} [{r['actor']}] {r['action']}: "
        f"{r['detail'][:120]}{' ✓' if r['ok'] else ' ✗'}"
        for r in rows)


TOOL = {
    "name": "pc",
    "description": (
        "Owner takeover of a Dot's isolated computer: create/start/stop "
        "it, run a command (argv, jailed, 30/60 s), read/write files "
        "inside its workspace, drive its browser, toggle "
        "browser/files/shell permissions, and read the audit log "
        "(owner vs agent). Example: 'Researcher ke computer pe ls "
        "chalao'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": " | ".join(_ACTIONS)},
            "dot": {"type": "STRING",
                    "description": "Dot name or #id (or pc=#computer)"},
            "pc": {"type": "STRING", "description": "Computer #id"},
            "perms": {"type": "STRING",
                      "description": "create/perms: e.g. "
                                     "'files,shell' or "
                                     "'browser=true,shell=false'"},
            "argv": {"type": "STRING",
                     "description": "exec: the command line (quoted "
                                    "strings, no shell operators)"},
            "timeout": {"type": "INTEGER",
                        "description": "exec: seconds (cap 60)"},
            "path": {"type": "STRING",
                     "description": "files_*: path inside the workspace"},
            "content": {"type": "STRING",
                        "description": "files_write: text to write"},
            "append": {"type": "BOOLEAN",
                       "description": "files_write: append instead of "
                                      "overwrite"},
            "op": {"type": "STRING",
                   "description": "browser: navigate | read | snapshot |"
                                  " screenshot | click | type | key | "
                                  "scroll"},
            "url": {"type": "STRING", "description": "browser navigate"},
            "target": {"type": "STRING",
                       "description": "browser click/type selector"},
            "text": {"type": "STRING", "description": "browser type"},
            "key": {"type": "STRING", "description": "browser key"},
            "direction": {"type": "STRING",
                          "description": "browser scroll: up|down"},
            "message": {"type": "STRING",
                        "description": "fallback for exec command"},
        },
        "required": [],
    },
    "handler": pc,
}
