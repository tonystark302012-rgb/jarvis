# dots/brain.py
"""The Dot conversation brain.

Doc §3.3: context assembly (role + permissions + allowed prefs + page)
→ LLM with tool schemas → permission gate → tool results → final text,
bounded at MAX_ROUNDS tool rounds. Seams:

  * `set_llm(fn)` where fn is (system, history) → str   — legacy one-shot
    (kept for existing integrations/tests);
  * `set_llm(fn)` where fn is (messages, tools)  → str|dict — tool-loop
    seam (doc contract: tests script exact tool-call sequences, zero
    network);
  * no seam → local tool-calling LLM first (core/llm_client, free
    local-first per doc), gemini free tier as one-shot fallback.

Honesty contract (T11): no LLM reachable → the reply stored for the user
SAYS that, it is never a fake answer. Tool refusals are honest strings
(`denied: …`) appended into the loop, never silent.
"""
from __future__ import annotations

import inspect
import json

MAX_ROUNDS = 8

# Honest "the brain did not answer" prefixes — scheduler treats these as
# failed runs; the skill miner treats them as NOT-successful markers.
HONEST_FAILURES = (
    "Brain error",
    "No brain reachable",
    "No brain configured",
    "(the brain returned an empty reply)",
)

# Test/integration seam: legacy fn(system, hist) -> str,
# or tool-loop fn(messages, tools) -> str | {"content", "tool_calls"}
_llm = None


def set_llm(fn) -> None:
    """Inject the LLM callable (tests, or a future provider layer)."""
    global _llm
    _llm = fn


def _seam_style(fn) -> str:
    """'tools' (doc seam messages+tools), 'legacy' (system+hist) or
    'default' (no seam). Callable without a signature → legacy."""
    if fn is None:
        return "default"
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return "legacy"
    return "tools" if "tools" in params else "legacy"


def _page_context(page: dict | None) -> str:
    if not page:
        return ""
    return (f"\n\nYou are looking at page #{page['id']} "
            f"{page['title']!r} (rev {page['rev']}):\n"
            f"{page.get('content_md') or '(empty)'}")


def _skills_context() -> str:
    """Published skills only — drafts/archived never reach a dot (T9)."""
    from . import store
    try:
        pub = store.list_skills("published")[:20]
    except Exception:
        pub = []
    if not pub:
        return "PUBLISHED SKILLS: (none yet)"
    lines = "\n".join(
        f"- [{s['id']}] {s['title']}: "
        f"{(s.get('body_md') or '')[:400]}" for s in pub)
    return f"PUBLISHED SKILLS (follow when relevant):\n{lines}"


def system_prompt(dot: dict, prefs: list[dict], page: dict | None
                  ) -> str:
    from .tools import specs_for
    prefs_txt = "\n".join(f"- {p['key']}: {p['value']}" for p in prefs) \
        or "(none set)"
    role = str(dot.get("role_instructions") or "").strip() \
        or "(no role instructions yet)"
    perms = dot.get("permissions") if isinstance(dot.get("permissions"),
                                                  dict) else {}
    granted = ", ".join(sorted(k for k, v in perms.items() if v)) or "none"
    tools = specs_for(dot)
    tool_names = ", ".join(t["function"]["name"] for t in tools) \
        or "(none — you cannot call tools)"
    return (
        f"You are '{dot.get('name')}', a specialist agent on a self-hosted "
        f"workspace.\n\nROLE:\n{role}\n\n"
        f"PERMISSIONS (owner-granted): {granted}\n"
        f"TOOLS available: {tool_names}\n"
        "A tool result starting 'denied:' means the owner has NOT granted "
        "it — tell the owner that honestly, never pretend you did it.\n\n"
        f"OWNER PREFERENCES you may use:\n{prefs_txt}\n\n"
        + _skills_context() + "\n"
        + _page_context(page)
    )


# ── message shaping ──────────────────────────────────────────────────────────
def _messages(system: str, hist: list, user_text: str) -> list[dict]:
    msgs = [{"role": "system", "content": system}]
    for m in hist or []:
        if not isinstance(m, dict):
            continue
        raw = str(m.get("role") or "")
        role = ("assistant" if raw in ("dot", "assistant", "agent")
                else "user" if raw == "user" else None)
        if role is None:
            continue
        content = str(m.get("content") or "")
        if content:
            msgs.append({"role": role, "content": content})
    # the caller stores the user turn BEFORE calling reply() — don't send
    # the same turn twice when it is already the last history row
    if not (msgs[-1]["role"] == "user"
            and msgs[-1]["content"] == user_text):
        msgs.append({"role": "user", "content": user_text})
    return msgs


def _norm_tcs(raw) -> list[dict]:
    """Normalise tool_calls (Ollama-style dicts, JSON-string arguments,
    missing ids) into one shape the loop can echo back."""
    out = []
    for i, t in enumerate(raw or []):
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) \
            else {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args) or {}
            except ValueError:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_raw": str(args)}
        out.append({"id": str(t.get("id") or f"call_{i}"),
                    "function": {"name": str(name), "arguments": args}})
    return out


def _as_out(out) -> tuple[str, list[dict]]:
    if isinstance(out, str):
        return out, []
    if isinstance(out, dict):
        return (str(out.get("content") or ""),
                _norm_tcs(out.get("tool_calls")))
    raise ValueError(f"LLM returned {type(out).__name__}, expected str/dict")


# ── the bounded tool loop (doc §3.3) ─────────────────────────────────────────
def _run_loop(dot: dict, call, msgs: list[dict], tools: list[dict],
              fallback=None) -> str:
    """call(msgs, tools) → (content, tool_calls). Runs ≤ MAX_ROUNDS tool
    rounds; refusals/errors surface as honest strings inside the loop.
    With `fallback`, a FIRST-call transport failure delegates to it
    (local LLM died between probe and call → gemini one-shot)."""
    from .tools import run_tool
    last_content = ""
    for rnd in range(MAX_ROUNDS):
        try:
            content, tcs = call(msgs, tools)
        except Exception as e:
            if rnd == 0 and fallback is not None:
                return fallback(e)
            return (f"Brain error during tool loop "
                    f"(round {rnd + 1}): {e}")
        if content and content.strip():
            last_content = content
        if not tcs:
            return last_content.strip() or "(the brain returned an empty reply)"
        msgs.append({"role": "assistant",
                     "content": content or "",
                     "tool_calls": tcs})
        for tc in tcs:
            res = run_tool(dot, tc["function"]["name"],
                           tc["function"]["arguments"])
            msgs.append({"role": "tool",
                         "tool_call_id": tc["id"],
                         "name": tc["function"]["name"],
                         "content": res})
    note = (f"(tool limit: {MAX_ROUNDS} rounds reached without a final "
            f"answer — ask for something narrower)")
    tail = last_content.strip()
    return f"{tail}\n{note}" if tail else note


# ── default provider path (local first, gemini fallback — doc §HLD) ─────────
def _local_ready() -> bool:
    """Cheap reachability ping for the local tool-calling LLM.
    Never launches anything (auto-launch belongs to JARVIS startup)."""
    try:
        import requests
        from core import llm_client
        url, _ = llm_client.get_llm_settings()
        probe = (f"{url}/v1/models"
                 if llm_client.get_llm_provider() == "openai"
                 else f"{url}/api/tags")
        return requests.get(probe, timeout=2).status_code == 200
    except Exception:
        return False


def _local_call(messages: list, tools: list) -> tuple[str, list[dict]]:
    from core import llm_client
    return _as_out(llm_client.call_llm(messages, tools=tools or None,
                                       timeout=60))


def _gemini_reply(system: str, hist: list, user_text: str) -> str:
    try:
        from core import gemini
        contents = [system,
                    "\n".join(f"{m.get('role')}: {m.get('content')}"
                              for m in hist[-20:]),
                    f"user: {user_text}\n"
                    "Reply as the specialist dot, in the owner's language."]
        reply_obj = gemini.call(contents, tier=gemini.SMART, timeout_ms=30_000)
        text = getattr(reply_obj, "text", None)
        if text:
            return str(text).strip()
        return ("No brain configured — set a Gemini key in "
                "config/api_keys.json or run a local LLM "
                "(llm_provider=ollama).")
    except Exception as e:
        return f"No brain reachable ({e}) — configure a key or local LLM."


# ── entry point ──────────────────────────────────────────────────────────────
def reply(dot: dict, convo_key: str, user_text: str,
          page: dict | None = None, history: list[dict] | None = None
          ) -> str:
    """Generate the dot's reply for one turn and return it (caller stores
    it). Never raises."""
    from . import store
    from .tools import specs_for
    prefs = store.prefs_for_dot(dot)
    system = system_prompt(dot, prefs, page)
    hist = history if history is not None else \
        store.list_messages(convo_key, limit=30)
    style = _seam_style(_llm)

    if style == "legacy":
        if _llm is not None:
            try:
                out = _llm(system, hist + [{"role": "user",
                                            "content": user_text}])
                if isinstance(out, str) and out.strip():
                    return out.strip()
                return "(the brain returned an empty reply)"
            except Exception as e:
                return f"Brain error: {e}"
        # no seam → falls through to default (local first, gemini fallback)

    msgs = _messages(system, hist, user_text)
    tools = specs_for(dot)

    if style == "tools":
        def _call(m, tl, _fn=_llm):
            return _as_out(_fn(m, tl))
        return _run_loop(dot, _call, msgs, tools)

    # default: local tool-calling LLM first (doc: local-first), else gemini
    def _fallback(_e, _system=system, _hist=hist, _ut=user_text):
        return _gemini_reply(_system, _hist, _ut)

    if _local_ready():
        return _run_loop(dot, _local_call, msgs, tools, fallback=_fallback)
    return _gemini_reply(system, hist, user_text)
