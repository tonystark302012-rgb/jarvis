# dots/brain.py
"""The Dot conversation brain. Batch 6a: context assembly + one-shot reply
through an injectable seam (tests script it, zero network). Batch 6b adds
the tool-call loop on the same entry point.

Honesty contract (T11): no LLM reachable → the reply stored for the user
SAYS that, it is never a fake answer.
"""
from __future__ import annotations


# Test/integration seam: fn(system: str, history: list[dict]) -> str
_llm = None


def set_llm(fn) -> None:
    """Inject the LLM callable (tests, or a future provider layer)."""
    global _llm
    _llm = fn


def _page_context(page: dict | None) -> str:
    if not page:
        return ""
    return (f"\n\nYou are looking at page #{page['id']} "
            f"{page['title']!r} (rev {page['rev']}):\n"
            f"{page.get('content_md') or '(empty)'}")


def system_prompt(dot: dict, prefs: list[dict], page: dict | None
                  ) -> str:
    prefs_txt = "\n".join(f"- {p['key']}: {p['value']}" for p in prefs) \
        or "(none set)"
    role = str(dot.get("role_instructions") or "").strip() \
        or "(no role instructions yet)"
    return (
        f"You are '{dot.get('name')}', a specialist agent on a self-hosted "
        f"workspace.\n\nROLE:\n{role}\n\n"
        f"OWNER PREFERENCES you may use:\n{prefs_txt}\n"
        + _page_context(page)
    )


def reply(dot: dict, convo_key: str, user_text: str,
          page: dict | None = None, history: list[dict] | None = None
          ) -> str:
    """Generate the dot's reply for one turn and return it (caller stores
    it). Never raises."""
    from . import store
    prefs = store.prefs_for_dot(dot)
    system = system_prompt(dot, prefs, page)
    hist = history if history is not None else \
        store.list_messages(convo_key, limit=30)
    if _llm is not None:
        try:
            out = _llm(system, hist + [{"role": "user",
                                        "content": user_text}])
            if isinstance(out, str) and out.strip():
                return out.strip()
            return "(the brain returned an empty reply)"
        except Exception as e:
            return f"Brain error: {e}"
    # default: cloud free tier via the existing gemini client
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
