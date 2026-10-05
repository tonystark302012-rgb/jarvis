# dots/slack.py
"""Slack → Dot threads (doc §3.9).

`POST /api/slack/events` receives the official Slack events API:
  * `url_verification` handshake → echo `challenge` (route);
  * message events are GATED FIRST (T10): workspace ∈ allowlist AND
    user ∈ allowlist — an empty allowlist denies everything (secure
    default); a rejected event is acknowledged with 200 and NOTHING is
    executed, it is only logged;
  * `@DotName` mention → the message lands in convo
    `slack:<channel>:<thread_ts>` (thread continuity: `thread_ts` is
    the thread the reply goes back into) → brain → reply posted to the
    SAME thread via `chat.postMessage`.

Config: `config/dots_slack.json` (gitignored):
    {"bot_token": "xoxb-…", "workspaces": ["T…"], "users": ["U…"]}

Side effects (brain + Slack post) only ever run through `run()`, so
tests can exercise `evaluate()` (pure decision) with zero network.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from . import store

# "@Researcher" / "@research-bot" — first char must be a letter so we
# never eat emails or raw user ids ("<@U123>")
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_-]{1,40})")


def _config_path() -> Path:
    from config import get_base_dir
    return Path(get_base_dir()) / "config" / "dots_slack.json"


def _config() -> dict:
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _logged(reason: str) -> None:
    print(f"[Dots/Slack] ignored event: {reason}")


# ── pure decision (no side effects, no network) ──────────────────────────────
def evaluate(payload: dict) -> tuple[bool, str, dict | None]:
    """→ (process?, reason, context). reason is set exactly when the
    event must be acknowledged and dropped (T10: nothing executed)."""
    if not isinstance(payload, dict):
        return False, "not an object", None
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        return False, f"not a message event ({event!r:.60})", None
    if event.get("bot_id") or event.get("subtype"):
        return False, "bot/edited message", None
    text = str(event.get("text") or "")
    if not text.strip():
        return False, "empty message", None

    cfg = _config()
    workspaces = [str(w) for w in (cfg.get("workspaces") or [])]
    users = [str(u) for u in (cfg.get("users") or [])]
    workspace = str(payload.get("team_id")
                    or event.get("team") or payload.get("team") or "")
    user = str(event.get("user") or "")
    if not workspaces or workspace not in workspaces:
        return False, (f"workspace {workspace or '(none)'} not in the "
                       "allowlist"), None
    if not users or user not in users:
        return False, f"user {user or '(none)'} not in the allowlist", None

    m = _MENTION.search(text)
    if not m:
        return False, "no @mention", None
    dot = store.find_dot_by_name(m.group(1))
    if dot is None:
        return False, f"no Dot named {m.group(1)!r}", None

    channel = str(event.get("channel") or "")
    thread_ts = str(event.get("thread_ts") or event.get("ts") or "")
    if not channel or not thread_ts:
        return False, "missing channel/ts", None
    return True, "", {"text": text, "user": user, "dot": dot,
                      "channel": channel, "thread_ts": thread_ts,
                      "convo": f"slack:{channel}:{thread_ts}"}


# ── side effects (messages + brain + Slack reply) ────────────────────────────
def _post_slack(method: str, payload: dict) -> dict:
    """Slack Web API call (free tier). Seam for tests. Never raises."""
    cfg = _config()
    token = str(cfg.get("bot_token") or "")
    if not token:
        return {"ok": False, "error": "no bot_token configured"}
    try:
        import requests
        r = requests.post(f"https://slack.com/api/{method}",
                          json=payload,
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=10)
        data = r.json() if r.content else {}
        return {"ok": bool(data.get("ok")), "error": data.get("error")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run(ctx: dict) -> dict:
    """Execute an already-allowed event: store → brain → thread reply."""
    from . import brain
    convo = ctx["convo"]
    store.add_message(convo, "user", ctx["text"],
                      meta={"slack": {"user": ctx["user"],
                                      "channel": ctx["channel"],
                                      "ts": ctx["thread_ts"]}})
    store.upsert_slack_thread(convo, ctx["channel"], ctx["thread_ts"])
    reply = brain.reply(ctx["dot"], convo, ctx["text"])
    store.add_message(convo, "dot", reply,
                      meta={"slack": {"channel": ctx["channel"],
                                      "ts": ctx["thread_ts"]}})
    posted = _post_slack("chat.postMessage",
                         {"channel": ctx["channel"],
                          "thread_ts": ctx["thread_ts"],
                          "text": reply})
    return {"ok": True, "reply": reply, "convo": convo,
            "dot": ctx["dot"]["name"], "posted": bool(posted.get("ok")),
            "post_error": posted.get("error")}


def handle_event(payload: dict, background: bool = False) -> dict:
    """Ack-shaped result. Rejected events (T10) → ok=True + reason,
    NOTHING executed. Allowed ones run now, or in a daemon thread when
    `background=True` (the events API needs its 200 fast)."""
    process, reason, ctx = evaluate(payload)
    if not process:
        _logged(reason)
        return {"ok": True, "ignored": reason}
    if background:
        threading.Thread(target=run, args=(ctx,), daemon=True,
                         name="dots-slack").start()
        return {"ok": True, "processing": True, "convo": ctx["convo"]}
    return run(ctx)
