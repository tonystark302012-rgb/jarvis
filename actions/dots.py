# actions/dots.py
"""dots — JARVIS's specialist agents (Dots) BY VOICE/TOOL.

The Dots ENGINE lives in dots/ (storage, approvals, brain — shared with
the dashboard's workspace routes). This action is the JARVIS-native
surface: create specialists, talk to each one (every Dot keeps its OWN
conversation), review their proposed page edits, and manage the
preferences (memory) they are allowed to read.

    dots action=dot_create name=Researcher role="cite every source" \
         perms="research,memory,space"
    dots action=chat dot=Researcher message="compare these two papers"
    dots action=pending_list
    dots action=approve which=3
    dots action=memory_set key=language value=Hinglish

Duplicate-check: multi_agent (one-shot dry run) and task_agent (goal
planner) are UNCHANGED — Dots are persistent personas with separate
conversations, permissions and review rights; nothing here replaces
them.
"""
from __future__ import annotations

import json
import time
from datetime import datetime


def _parse_at(raw) -> float | None:
    """at=/run_at= accepts epoch seconds or a local 'YYYY-MM-DD HH:MM(:SS)'.
    None/'' → None (no one-shot). Raises ValueError with a usable message."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if s.isdigit():
        return float(s)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        raise ValueError(
            f"at= must be 'YYYY-MM-DD HH:MM' or epoch seconds, got {raw!r}")


def _sched_label(t: dict) -> str:
    """Human schedule for one task row (task_list + messages)."""
    kind = t.get("schedule_kind") or "every"
    if kind == "cron":
        return f"cron {t.get('cron_expr')}"
    if kind == "at":
        return time.strftime("at %Y-%m-%d %H:%M",
                             time.localtime(t.get("run_at") or 0))
    return f"every {t['every_seconds']}s"


def _resolve_dot(params: dict) -> dict | None:
    raw = str(params.get("dot") or params.get("name") or "").strip()
    if not raw:
        return None
    from dots import store
    if raw.isdigit():
        return store.get_dot(int(raw))
    return store.find_dot_by_name(raw)


def _parse_perms(raw) -> dict | None:
    """dot_create perms: 'research,memory,space' | 'all' | JSON object |
    already-a-dict → permissions dict (doc: per-Dot permissions)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (list, tuple)):
        return {str(k).strip(): True for k in raw if str(k).strip()}
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("{"):
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    if s.lower() == "all":
        return {"research": True, "memory": True, "space": True}
    return {k.strip().lower(): True for k in s.split(",") if k.strip()}


def _require_dot(params: dict) -> tuple[dict | None, str | None]:
    d = _resolve_dot(params)
    if d is None:
        from dots import store
        known = ", ".join(x["name"] for x in store.list_dots()) or "(none)"
        return None, f"No Dot named {params.get('dot')!r}. Known: {known}."
    return d, None


def dots(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "chat").lower().strip()
    from dots import store

    # ── specialist agents ──────────────────────────────────────────────
    if action in ("dot_create", "create"):
        try:
            d = store.create_dot(
                params.get("dot") or params.get("name"),
                params.get("role") or "",
                permissions=_parse_perms(params.get("perms")
                                         or params.get("permissions")))
        except ValueError as e:
            return str(e)
        granted = ", ".join(sorted(k for k, v in d["permissions"].items()
                                   if v)) or "none"
        return (f"Dot '{d['name']}' created (#{d['id']}) "
                f"[perms: {granted}]. "
                f"Talk to it: dots action=chat dot={d['name']} "
                f"message=...")

    if action in ("dot_list", "list"):
        ds = store.list_dots()
        if not ds:
            return ("No Dots yet — create one: "
                    "dots action=dot_create name=Researcher role=...")
        return "\n".join(
            f"#{x['id']} {x['name']} — "
            f"{(x['role_instructions'] or '(no role)')[:80]}" for x in ds)

    if action in ("dot_show", "show"):
        d, err = _require_dot(params)
        if err:
            return err
        return (f"Dot #{d['id']} '{d['name']}'\n"
                f"Role: {d['role_instructions'] or '(none)'}\n"
                f"Permissions: {json.dumps(d['permissions'])}")

    if action in ("dot_delete", "delete"):
        d, err = _require_dot(params)
        if err:
            return err
        store.delete_dot(d["id"])
        return f"Dot '{d['name']}' deleted."

    # ── chat (separate conversation per Dot; optional page scope) ─────
    if action == "chat":
        message = str(params.get("message") or "").strip()
        if not message:
            return "Give a message — dots action=chat dot=… message=…"
        d, err = _require_dot(params)
        if err:
            return err
        page = None
        convo = f"dot:{d['id']}"
        page_ref = params.get("page")
        if page_ref not in (None, ""):
            page = _resolve_page(params)
            if page is None:
                return f"No page {page_ref!r}."
            convo = f"page:{page['id']}"
        from dots import brain
        store.add_message(convo, "user", message)
        answer = brain.reply(d, convo, message, page=page)
        store.add_message(convo, "dot", answer)
        try:
            from dots import learning
            learning.mine()          # drafts only — never auto-publish
        except Exception:
            pass
        return answer

    # ── human-in-the-loop review (owner side; Dots cannot self-approve)
    # ──────────────────────────────────────────────────────────────────
    if action in ("pending_list", "pending", "review"):
        rows = store.list_pending("pending")
        if not rows:
            return "No proposals waiting for review."
        out = []
        for p in rows:
            tgt = (f"page #{p['page_id']}" if p["kind"] == "edit"
                   else f"new page in space #{p['space_id']}")
            dot_name = (store.get_dot(p["dot_id"]) or {}).get("name",
                                                              "?")
            out.append(f"#{p['id']} [{p['kind']}] by {dot_name} → {tgt} "
                       f"(base rev {p['base_rev']}): "
                       f"{p['title']!r} — {p['reason'] or 'no reason'}")
        return ("Pending proposals:\n" + "\n".join(out) +
                "\nApprove: dots action=approve which=<id> · "
                "Decline: dots action=decline which=<id>")

    if action == "approve":
        which = str(params.get("which") or "").strip()
        if not which.isdigit():
            return "Give the proposal number — dots action=approve which=3"
        out = store.approve_pending(int(which))
        if "error" in out:
            return out["error"]
        if out.get("conflict"):
            return (f"NOT saved — {out['reason']}. Nothing was overwritten; "
                    f"tell the Dot to re-propose on the current revision.")
        return (f"Approved — page saved as rev "
                f"{out['page']['rev']}: {out['page']['title']!r}")

    if action == "decline":
        which = str(params.get("which") or "").strip()
        if not which.isdigit():
            return "Give the proposal number — dots action=decline which=3"
        out = store.decline_pending(int(which))
        return out.get("error") or "Declined — nothing was saved."

    # ── memory (preferences the permitted Dots may read) ──────────────
    if action in ("memory_list", "memory"):
        prefs = store.list_prefs()
        if not prefs:
            return "No preferences saved yet."
        return "\n".join(
            f"#{p['id']} {p['key']} = {p['value']} "
            f"(allowed: {p['allowed']})" for p in prefs)

    if action == "memory_set":
        key = str(params.get("key") or "").strip()
        value = str(params.get("value") or "").strip()
        if not key or not value:
            return "Give key and value — dots action=memory_set key=… value=…"
        allowed = params.get("allowed") or "*"
        if isinstance(allowed, str) and allowed.strip() != "*":
            allowed = [x.strip() for x in allowed.split(",") if x.strip()]
        try:
            p = store.set_pref(key, value, allowed)
        except ValueError as e:
            return str(e)
        return f"Saved: {p['key']} = {p['value']} (allowed: {p['allowed']})"

    if action == "memory_delete":
        which = str(params.get("which") or params.get("key") or "").strip()
        if which.isdigit():
            ok = store.delete_pref(int(which))
            return "Deleted." if ok else f"No preference #{which}."
        for p in store.list_prefs():
            if p["key"].lower() == which.lower():
                store.delete_pref(p["id"])
                return f"Deleted {p['key']}."
        return f"No preference {which!r}."

    # ── background/scheduled tasks (recurring, 90 s cap per run) ─────
    if action in ("task_create", "task"):
        instruction = str(params.get("instruction") or
                          params.get("message") or "").strip()
        if not instruction:
            return ("Give the recurring command — dots action=task_create "
                    "instruction=\"…\" every=3600 (or cron=\"0 7 * * *\" "
                    "or at=\"2026-10-06 07:00\") dot=…")
        raw_dot = str(params.get("dot") or "").strip()
        if raw_dot:
            d, err = _require_dot(params)
            if err:
                return err
        else:
            ds = store.list_dots()
            if len(ds) > 1:
                return ("Which Dot should run it? Add dot=… Known: "
                        + ", ".join(x["name"] for x in ds))
            d = ds[0] if ds else None      # no dots → main brain (dot-less)
        every = params.get("every") or params.get("every_seconds") or 3600
        try:
            run_at = _parse_at(params.get("at")
                               if params.get("at") is not None
                               else params.get("run_at"))
            t = store.create_task(params.get("task") or instruction[:40],
                                  instruction, every,
                                  d["id"] if d else None,
                                  cron=str(params.get("cron") or "").strip()
                                  or None,
                                  run_at=run_at)
        except (ValueError, KeyError) as e:
            return str(e)
        from dots import scheduler
        scheduler.ensure_started()
        where = f"on Dot '{d['name']}'" if d else "on the main brain"
        return (f"Task '{t['name']}' scheduled {_sched_label(t)} "
                f"{where} (first run on the next tick, "
                f"hard cap {scheduler.DEADLINE_SECONDS}s per run). "
                f"List: dots action=task_list")

    if action in ("task_list", "tasks"):
        from dots import scheduler
        scheduler.ensure_started()
        rows = store.list_tasks()
        if not rows:
            return ("No scheduled tasks — dots action=task_create "
                    "instruction=… every=…")
        lines = []
        for t in rows:
            if t["dot_id"] is None:
                dn = "main brain"
            else:
                dn = (store.get_dot(t["dot_id"]) or {}).get("name", "?")
            lines.append(f"#{t['id']} [{t['status']}] {_sched_label(t)} "
                         f"on {dn}: {t['name']}")
        return "\n".join(lines)

    if action in ("task_pause", "task_resume", "task_cancel",
                  "task_retry"):
        tid = str(params.get("which") or params.get("task") or "").strip()
        if not tid.isdigit():
            for t in store.list_tasks():
                if t["name"].lower() == tid.lower():
                    tid = str(t["id"])
                    break
            else:
                return (f"No task {tid!r} — list: dots action=task_list "
                        f"(or pass which=#id)")
        from dots import scheduler
        try:
            tid_i = int(tid)
            if action == "task_pause":
                t = scheduler.pause(tid_i)
                return (f"Task #{tid} paused — state kept; resume: "
                        f"dots action=task_resume which={tid}")
            if action == "task_resume":
                t = scheduler.resume(tid_i)
                return f"Task #{tid} active again."
            if action == "task_cancel":
                t = scheduler.cancel(tid_i)
                extra = (" (live run aborted)" if t.get("aborted_live_run")
                         else "")
                return f"Task #{tid} cancelled permanently{extra}."
            r = scheduler.retry(tid_i)
            return (f"Retrying task #{tid} now (previous run "
                    f"#{r['retried_run']} failed).")
        except KeyError as e:
            return str(e)
        except ValueError as e:
            return str(e)

    if action in ("task_runs", "runs"):
        which = str(params.get("which") or params.get("task") or "")
        if not which.isdigit():
            return "task_runs needs which=#task-id (dots action=task_list)"
        rows = store.list_runs(int(which))
        if not rows:
            return f"No runs yet for task #{which}."
        icon = {"ok": "✓", "failed": "✗", "timeout": "⏱", "cancelled": "⊘"}
        return "\n".join(
            f"#{r['id']} {icon.get(r['status'], '?')} {r['status']}: "
            f"{(r['output'] or '')[:160]}" for r in rows)

    # ── voice calls (captions, timer, receipt, background agent) ──────
    if action in ("call_start", "call"):
        d, err = _require_dot(params)
        if err:
            return err
        page = None
        if params.get("page") not in (None, ""):
            page = _resolve_page(params)
            if page is None:
                return f"No page {params.get('page')!r}."
        from dots import voice
        try:
            c = voice.start(d["id"], page["id"] if page else None)
        except (ValueError, KeyError) as e:
            return str(e)
        return (f"Call #{c['id']} live with Dot '{d['name']}' "
                f"({voice.provider_name()}) — timer running. "
                f"Captions: dots action=call_caption which={c['id']} "
                f"message=… · end: dots action=call_end which={c['id']}")

    if action in ("call_list", "calls"):
        from dots import voice
        rows = voice.list_calls()
        if not rows:
            return "No calls yet — dots action=call_start dot=…"
        return "\n".join(
            f"#{c['id']} [{c['status']}] {round(c['elapsed_seconds'])}s "
            f"dot #{c['dot_id']}" for c in rows)

    if action in ("call_end", "call_caption", "call_transcript",
                  "call_background"):
        cid = str(params.get("which") or params.get("call") or "").strip()
        if not cid.isdigit():
            d, err = _require_dot(params)
            if err:
                return (err + " (or pass which=#call-id)")
            from dots import voice
            live = [c for c in voice.list_calls("active")
                    if c["dot_id"] == d["id"]]
            if not live:
                return f"No active call for '{d['name']}'."
            cid = str(live[0]["id"])
        from dots import voice
        cid_i = int(cid)
        try:
            if action == "call_end":
                c = voice.end(cid_i)
                return (f"Call #{cid} ended ({round(c['elapsed_seconds'])}s, "
                        f"{c['transcript_messages']} transcript lines). "
                        f"Receipt: dots action=call_transcript "
                        f"which={cid}")
            if action == "call_caption":
                text = str(params.get("message") or "").strip()
                if not text:
                    return ("call_caption needs message=… (what the "
                            "caller said)")
                res = voice.caption(cid_i,
                                    str(params.get("speaker") or "user"),
                                    text)
                if "error" in res:
                    return res["error"]
                if res.get("dot_caption"):
                    return f"{d_name_for(cid_i, params)}: {res['dot_caption']}"
                return f"[caption stored] {res['text'][:200]}"
            if action == "call_transcript":
                return voice.transcript(cid_i, format="text")
            # call_background
            instruction = str(params.get("message") or
                              params.get("instruction") or "").strip()
            r = voice.background(cid_i, instruction)
            return (f"Background agent scheduled on the call's Dot: "
                    f"task #{r['task']['id']} every "
                    f"{r['task']['every_seconds']}s — {r['note']}")
        except KeyError as e:
            return str(e)
        except ValueError as e:
            return str(e)

    # ── skills (miner drafts → OWNER publishes; T9) ───────────────────
    if action in ("skill_list", "skills"):
        status = str(params.get("status") or "").strip().lower()
        rows = store.list_skills(status if status in (
            "draft", "published", "archived") else None)
        if not rows:
            return ("No skills yet — the miner creates DRAFTS from "
                    "repeated conversations (dots action=skill_mine).")
        return "\n".join(
            f"#{s['id']} [{s['status']}] {s['title']}" for s in rows)

    if action == "skill_mine":
        from dots import learning
        drafts = learning.mine()
        if not drafts:
            return ("No new skill drafts — I look for a task shape "
                    "repeated 3+ times with completed answers.")
        return ("New DRAFT skill(s): "
                + "; ".join(f"#{d['id']} {d['title']!r}"
                            for d in drafts)
                + " — publish them yourself (dots action=skill_publish "
                  "which=…); nothing is ever auto-published.")

    if action in ("skill_publish", "skill_archive"):
        which = str(params.get("which") or params.get("skill") or "").strip()
        if not which.isdigit():
            return "skill action needs which=#skill-id (skill_list shows ids)"
        try:
            if action == "skill_publish":
                s = store.publish_skill(int(which))
                return (f"Skill #{s['id']} PUBLISHED — every Dot now "
                        f"sees it: {s['title']!r}")
            s = store.archive_skill(int(which))
            return f"Skill #{s['id']} archived (kept for the miner, not shown)."
        except KeyError as e:
            return str(e)
        except ValueError as e:
            return str(e)

    return ("Unknown dots action — use: dot_create | dot_list | dot_show | "
            "dot_delete | chat | pending_list | approve | decline | "
            "memory_list | memory_set | memory_delete | task_create | "
            "task_list | task_pause | task_resume | task_cancel | "
            "task_retry | task_runs | skill_list | skill_mine | "
            "skill_publish | skill_archive | call_start | call_list | "
            "call_end | call_caption | call_transcript | "
            "call_background")


def d_name_for(call_id: int, params: dict) -> str:
    from dots import store, voice
    c = voice.get(call_id)
    return (store.get_dot(c["dot_id"]) or {}).get("name", "Dot")


def _resolve_page(params: dict):
    from dots import store
    raw = str(params.get("page") or "").strip()
    if raw.isdigit():
        return store.get_page(int(raw))
    # voice fallback: unique title match
    want = raw.lower()
    for s in store.list_spaces():
        for p in store.list_pages(s["id"]):
            if p["title"].lower() == want:
                return p
    return None


TOOL = {
    "name": "dots",
    "description": (
        "Specialist agents (Dots): persistent personas, each with its own "
        "role, permissions and SEPARATE conversation. Use to create a "
        "specialist (dot_create), ask one something (chat, optionally "
        "scoped to a page), review the page edits Dots have proposed "
        "(pending_list → approve/decline — only the owner approves, and a "
        "stale proposal is refused, never overwritten), and manage the "
        "preferences Dots may read (memory_*). Example: 'Researcher se "
        "pucho, ye topic 3 sources ke saath summary bana do'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "dot_create | dot_list | dot_show | "
                                      "dot_delete | chat | pending_list | "
                                      "approve | decline | memory_list | "
                                      "memory_set | memory_delete | "
                                      "task_create | task_list | "
                                      "task_pause | task_resume | "
                                      "task_cancel | task_retry | "
                                      "task_runs | skill_list | "
                                      "skill_mine | skill_publish | "
                                      "skill_archive | call_start | "
                                      "call_list | call_end | "
                                      "call_caption | call_transcript | "
                                      "call_background"},
            "dot": {"type": "STRING",
                    "description": "Dot name or #id (chat/show/delete)"},
            "name": {"type": "STRING", "description": "New Dot's name"},
            "role": {"type": "STRING",
                     "description": "Role instructions for dot_create"},
            "perms": {"type": "STRING",
                      "description": "dot_create permissions: comma list "
                                     "(research,memory,space), 'all', or "
                                     "a JSON object; default none"},
            "message": {"type": "STRING",
                        "description": "Message for chat"},
            "page": {"type": "STRING",
                     "description": "Optional page id/title to scope chat"},
            "which": {"type": "STRING",
                      "description": "Proposal # (approve/decline) or "
                                     "preference # (memory_delete)"},
            "key": {"type": "STRING", "description": "Preference key"},
            "value": {"type": "STRING", "description": "Preference value"},
            "allowed": {"type": "STRING",
                        "description": "memory_set: '*' (default) or comma "
                                       "list of Dot names"},
            "instruction": {"type": "STRING",
                            "description": "task_create: the recurring "
                                           "command to run"},
            "every": {"type": "STRING",
                      "description": "task_create: seconds between runs "
                                     "(e.g. 3600) — ignored when cron=/at= "
                                     "is given"},
            "cron": {"type": "STRING",
                     "description": "task_create: cron schedule, 5 fields "
                                    "local time — e.g. '0 7 * * *' (daily "
                                    "07:00), '*/15 * * * *' (every 15 min). "
                                    "Pick ONE of every/cron/at."},
            "at": {"type": "STRING",
                   "description": "task_create: one-shot at 'YYYY-MM-DD "
                                  "HH:MM' (local) or epoch seconds — runs "
                                  "once then status=done. Pick ONE of "
                                  "every/cron/at."},
            "task": {"type": "STRING",
                     "description": "task name or #id for task_* actions"},
            "status": {"type": "STRING",
                       "description": "skill_list filter: draft | "
                                      "published | archived"},
            "call": {"type": "STRING",
                     "description": "call # for call_* actions"},
            "speaker": {"type": "STRING",
                        "description": "call_caption: user (default, "
                                       "triggers the Dot's reply) or "
                                       "dot (audio path line)"},
        },
        "required": [],
    },
    "handler": dots,
}
