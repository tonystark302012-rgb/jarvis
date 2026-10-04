# dots/voice.py
"""Voice calls — phase 1 core (doc §3.10).

Session + REST lifecycle: start/end with a LIVE timer, per-speaker
captions (user | dot) stored as transcript messages under convo
`call:<id>` (user captions can generate the dot's reply through the
normal brain), a transcript receipt (text/json download), and the
background compute agent = a regular scheduler task bound to the
call's Dot (machinery reused from dots/scheduler.py).

Mute/minimize are UI-side (dashboard batch). The audio path sits
behind a provider seam: `set_provider(obj)` — phase 2 wires Gemini
Live (free tier) / OpenAI Realtime (only when configured) into the
same interface; without a provider the call honestly reports
`captions-only` mode and everything except audio still works.
"""
from __future__ import annotations

import time

from . import store

_provider = None


def set_provider(provider) -> None:
    """Phase-2 audio provider seam: needs .describe() -> str and
    optionally .start(call) / .stop(call). None = captions-only."""
    global _provider
    _provider = provider


def provider_name() -> str:
    if _provider is None:
        return "captions-only"
    return str(getattr(_provider, "name", None)
               or getattr(_provider, "describe", lambda: "custom")())


def elapsed(call: dict) -> float:
    end = call.get("ended_at") or time.time()
    return max(0.0, float(end) - float(call["started_at"]))


def _payload(call: dict) -> dict:
    out = dict(call)
    out["elapsed_seconds"] = round(elapsed(call), 1)
    out["provider"] = provider_name()
    return out


# ── lifecycle ────────────────────────────────────────────────────────────────
def start(dot_id: int, page_id: int | None = None) -> dict:
    call = store.start_call(dot_id, page_id, provider=provider_name())
    if _provider is not None:
        hook = getattr(_provider, "start", None)
        if callable(hook):
            try:
                hook(call)
            except Exception as e:
                print(f"[Dots/voice] provider start failed: {e}")
    return _payload(call)


def end(cid: int) -> dict:
    call = store.get_call(cid)
    if call is None:
        raise KeyError(f"no call #{cid}")
    call = store.end_call(cid)
    if _provider is not None:
        hook = getattr(_provider, "stop", None)
        if callable(hook):
            try:
                hook(call)
            except Exception as e:
                print(f"[Dots/voice] provider stop failed: {e}")
    out = _payload(call)
    out["transcript_messages"] = len(transcript(cid, format="json"))
    return out


def get(cid: int) -> dict:
    call = store.get_call(cid)
    if call is None:
        raise KeyError(f"no call #{cid}")
    return _payload(call)


def list_calls(status: str | None = None) -> list[dict]:
    return [_payload(c) for c in store.list_calls(status)]


# ── captions (the live transcript) ───────────────────────────────────────────
def caption(cid: int, speaker: str, text: str,
            generate_reply: bool = True) -> dict:
    """One caption line. speaker='user' stores the ASR line and (by
    default) generates the Dot's reply through the brain — that reply
    is stored as the dot's caption. speaker='dot' stores a line the
    audio path (or the owner) produced. Never raises: honest error
    dicts for ended calls / bad input."""
    call = store.get_call(cid)
    if call is None:
        return {"error": f"no call #{cid}"}
    if call["status"] != "active":
        return {"error": f"call #{cid} has ended — start a new one"}
    speaker = str(speaker or "user").lower().strip()
    if speaker not in ("user", "dot"):
        return {"error": "speaker must be 'user' or 'dot'"}
    text = str(text or "").strip()
    if not text:
        return {"error": "empty caption"}
    convo = f"call:{cid}"
    dot = store.get_dot(call["dot_id"])
    if speaker == "user":
        store.add_message(convo, "user", text,
                          meta={"speaker": "user"})
        reply = None
        if generate_reply:
            from . import brain
            page = (store.get_page(call["page_id"])
                    if call.get("page_id") else None)
            reply = brain.reply(dot, convo, text, page=page)
            store.add_message(convo, "dot", reply,
                              meta={"speaker": "dot"})
        out = {"ok": True, "speaker": "user", "text": text,
               "convo": convo}
        out["dot_caption"] = reply
        return out
    store.add_message(convo, "dot", text, meta={"speaker": "dot"})
    return {"ok": True, "speaker": "dot", "text": text, "convo": convo}


# ── transcript receipt ───────────────────────────────────────────────────────
def transcript(cid: int, format: str = "json"):
    call = store.get_call(cid)
    if call is None:
        raise KeyError(f"no call #{cid}")
    msgs = store.list_messages(f"call:{cid}", limit=500)
    if format == "json":
        return [{"role": m["role"],
                 "speaker": (m.get("meta") or {}).get("speaker",
                                                      m["role"]),
                 "content": m["content"],
                 "at": m.get("created_at")} for m in msgs]
    lines = [f"Call #{cid} — Dot "
             f"#{call['dot_id']} "
             f"({'live' if call['status'] == 'active' else 'ended'}, "
             f"{round(elapsed(call))}s)", "-" * 40]
    for m in msgs:
        spk = (m.get("meta") or {}).get("speaker", m["role"])
        lines.append(f"[{spk}] {m['content']}")
    return "\n".join(lines)


# ── background compute agent during the call (scheduler reuse) ───────────────
def background(cid: int, instruction: str,
               every_seconds: int = 120) -> dict:
    call = store.get_call(cid)
    if call is None:
        raise KeyError(f"no call #{cid}")
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ValueError("background agent needs an instruction")
    task = store.create_task(
        f"call-{cid} agent", instruction, every_seconds, call["dot_id"])
    from . import scheduler
    scheduler.ensure_started()
    return {"ok": True, "task": task,
            "note": ("running on the call's Dot — watch it live: "
                     f"dots action=task_runs which={task['id']}")}
