"""
tool_tiers — declare the tools the model needs every turn, and let it fetch the
rest on demand.

THE PROBLEM
    Every `actions/*.py` that declares a TOOL dict is sent to Gemini on every
    connection, whether or not the user will ever touch it. That is the price of
    the plugin architecture and it had grown to 76 declarations — 72,263
    characters of JSON, ~18,000 tokens — plus a second copy of the same 76
    descriptions rendered into the system prompt by `_describe_tools`.
    Measured end to end, about **25,000 tokens** of static payload go out before
    a word is spoken, on every session.

    It is not only cost. 76 tools is a lot of surface to choose from, and a
    model picking between `video_player` and `youtube_video` and `screen_mirror`
    is a model that sometimes picks wrong.

WHY NOT INJECT TOOLS MID-SESSION
    The Live API session exposes `send_client_content`, `send_realtime_input`
    and `send_tool_response` — and nothing that updates the declared tool set.
    Tool declarations are fixed when the socket opens, so "declare more later"
    is not available and the model can never call a tool it was not told about.

    So the model is always told about *one* extra tool, `toolbox`, which can
    look up any deferred tool's full schema and run it.

WHAT STAYS CORE
    `CORE_TOOL_NAMES` below is a deliberate, auditable list — the things a
    voice assistant is asked for constantly (open an app, search, weather, a
    reminder, a file, the volume, play a video, remember this, undo that).
    Everything specialised — CAD, Manim, the A/B evaluator, the MCP bridge,
    Telegram, the smart-home bus — is deferred, because reaching it costs one
    extra round trip that a rarely-used tool can afford and "open Chrome"
    cannot.

    Deferred does NOT mean unreachable or unsandboxed. `ActionRegistry.run` is
    the single dispatch choke point, and the router re-enters `JarvisLive.
    _execute_tool`, so the autonomy gate, the confirm gate, the undo stack, the
    Mission Control timeline and the audit chain all apply to a routed call
    exactly as they do to a direct one. Tiering changes what the model is
    *told*; it changes nothing about what is *allowed*.

TURNING IT OFF
    `config/api_keys.json` → `"tool_tiering": false` restores the old
    behaviour: every tool declared every session.
"""
from __future__ import annotations

import json
import re
from typing import Iterable

# ── the router ────────────────────────────────────────────────────────────────
ROUTER_NAME = "toolbox"

ROUTER_DESCRIPTION = (
    "The rest of your abilities, on demand. JARVIS ships FAR more tools than are "
    "declared up front — specialist ones (CAD, diagrams, code agents, messaging "
    "platforms, smart home, the memory graph, MCP servers and more). Two actions:\n"
    "  action='search' query='...'  — list the tools that match, with the exact "
    "parameters each one takes. Do this FIRST whenever the request sounds like "
    "something specialised and none of your named tools fit.\n"
    "  action='run' tool='<name>' parameters_json='{\"action\": ...}'  — run one "
    "of them. `parameters_json` is that tool's parameters as a JSON object.\n"
    "Example: search query='3d model of a bracket' → run tool='make_3d' "
    "parameters_json='{\"spec\":\"box 40x20x8\"}'. "
    "Never tell the user you cannot do something before searching here — the "
    "ability is usually one lookup away."
)

# ── what a voice assistant is asked for constantly ────────────────────────────
# Chosen for frequency, not for size. The four heaviest declarations here
# (file_processor, browser_control, computer_settings, video_player) are kept
# core on purpose: they are exactly the ones whose extra round trip a user would
# feel, and a router hop in front of "read this file" is worse than the tokens
# it saves.
CORE_TOOL_NAMES: frozenset[str] = frozenset({
    # applications, machine and screen
    "open_app", "computer_settings", "computer_control", "terminal",
    # looking things up
    "web_search", "weather_report", "browser_control",
    # files
    "file_processor", "file_controller",
    # keeping in touch and keeping time
    "send_message", "reminder",
    # media
    "youtube_video", "video_player",
    # clipboard, which is pasted into constantly
    "clip_history",
    # The user's own levers, not tasks. "privacy on" and "autonomy observe"
    # are how someone takes control back from the assistant; putting a router
    # hop in front of a safety switch means the one time it matters is the one
    # time the model has to go looking for it. Both are small (~1.3 KB for the
    # pair), which is a cheap price for never hiding them.
    "privacy", "autonomy",
    # live-session tools declared inline in main.py: memory, undo, vision,
    # monitors and shutdown. These are conversational basics; routing them
    # would add a hop to "remember this" and "take that back".
    "save_memory", "recall_memory", "undo", "screen_process",
    "system_status", "manage_monitor", "shutdown_jarvis", "close_camera",
})

# ── how many tools a single search returns ────────────────────────────────────
_SEARCH_LIMIT = 4

_WORD = re.compile(r"[a-z0-9]+")

# Words that appear in almost every description and so carry no signal.
_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "for", "in", "on", "is", "it",
    "this", "that", "with", "use", "used", "using", "you", "your", "user",
    "tool", "action", "actions", "when", "from", "by", "as", "at", "be", "can",
    "will", "any", "all", "if", "not", "no", "do", "does", "into", "out",
})


def router_declaration() -> dict:
    """The one always-declared gateway to everything deferred."""
    return {
        "name": ROUTER_NAME,
        "description": ROUTER_DESCRIPTION,
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": "'search' to find a tool, 'run' to execute one.",
                },
                "query": {
                    "type": "STRING",
                    "description": "What you want to do, in a few words (action=search).",
                },
                "tool": {
                    "type": "STRING",
                    "description": "Exact tool name from a previous search (action=run).",
                },
                "parameters_json": {
                    "type": "STRING",
                    "description": (
                        "The tool's parameters as a JSON object string, e.g. "
                        "'{\"action\": \"play\", \"query\": \"dune trailer\"}' "
                        "(action=run)."
                    ),
                },
            },
            "required": ["action"],
        },
        # Never batched with read-only tools: it can run anything, including
        # something that mutates the machine.
        "behavior": "BLOCKING",
    }


def split_declarations(
    declarations: Iterable[dict],
    core: frozenset[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split into (declared now, deferred behind the router).

    Order within each group is preserved, so the model still sees the core set
    in the order the registries produced it.
    """
    core_names = CORE_TOOL_NAMES if core is None else core
    kept: list[dict] = []
    deferred: list[dict] = []
    for d in declarations or ():
        name = d.get("name") if isinstance(d, dict) else getattr(d, "name", "")
        (kept if name in core_names else deferred).append(d)
    return kept, deferred


def _stem(word: str) -> str:
    """Crude singulariser, enough to stop "documents" missing "document".

    Deliberately not a real stemmer: the corpus is 60 short tool descriptions,
    and the failure being fixed is a plural (or a trailing 'e') keeping two
    obviously-related words apart. Anything cleverer would need a dependency
    and would risk merging words that should stay distinct.
    """
    for suffix in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(str(text or "").lower())
            if w not in _STOP]


def search(deferred: list[dict], query: str, limit: int = _SEARCH_LIMIT) -> list[dict]:
    """Rank deferred declarations against a plain-language query.

    Lexical rather than embedding-based on purpose: it is instant, it costs no
    API call, it works offline, and the corpus is 60 short descriptions where a
    name match is almost always decisive. Ties fall back to the original order,
    which keeps results stable between identical queries.
    """
    q = _tokens(query)
    if not q:
        return deferred[:limit]

    scored: list[tuple[int, int, dict]] = []
    for i, d in enumerate(deferred):
        name = str(d.get("name", ""))
        # Name matches are worth far more than a mention in the prose: a user
        # asking about "telegram" should get the telegram tool, not every tool
        # whose description happens to contain the word.
        name_tokens = set(_tokens(name.replace("_", " ")))
        desc_tokens = set(_tokens(d.get("description", "")))
        score = 0
        for w in q:
            if w in name_tokens:
                score += 10
            elif any(w in nt for nt in name_tokens):
                # "video" should reach `video_player` and `video_qa`.
                score += 6
            if w in desc_tokens:
                score += 2
            elif any(w in dt or dt in w for dt in desc_tokens):
                score += 1
        if score:
            scored.append((-score, i, d))

    if not scored:
        # Nothing matched on vocabulary. Returning an arbitrary handful is
        # still better than nothing: the model asked, and a wrong-but-related
        # list lets it correct its own query, where "no results" just makes it
        # give up and tell the user it cannot help.
        return deferred[:limit]

    scored.sort(key=lambda t: (t[0], t[1]))
    return [d for _s, _i, d in scored[:limit]]


def plan_run(args: dict, deferred: list[dict]) -> tuple[str, dict, str]:
    """Validate an `action=run` request → (target, parameters, error).

    Split out of the router in main.py so the decision is testable without Qt:
    `_execute_tool` needs a live JarvisLive, this needs a list of dicts. A
    non-empty `error` means nothing should be executed and the string is what
    to hand back to the model.

    The error strings are written for a model, not a human — each one says what
    to do next, because the model is the only one who can act on it.
    """
    target = str(args.get("tool") or "").strip()
    if not target:
        return "", {}, ("action=run needs tool=<name>. Search first "
                        "(action=search query=...) to get an exact name.")
    if target == ROUTER_NAME:
        return "", {}, ("toolbox cannot run itself. Search for the tool you "
                        "actually want (action=search query=...).")

    raw = args.get("parameters_json")
    if raw in (None, ""):
        return target, {}, ""
    if isinstance(raw, dict):
        return target, dict(raw), ""          # the model inlined an object

    try:
        params = json.loads(raw)
    except Exception as e:
        # Hand back the schema rather than just the parse error: the usual
        # cause is a guessed parameter name, and "invalid JSON" alone does not
        # let the model fix that.
        hint = format_schemas(search(deferred, target, limit=1))
        return "", {}, (f"parameters_json was not valid JSON ({e}). Pass a JSON "
                        f"object string. Expected shape:\n{hint}")

    if not isinstance(params, dict):
        return "", {}, ("parameters_json must be a JSON OBJECT, e.g. "
                        "'{\"action\": \"play\"}'.")
    return target, params, ""


def unknown_tool_hint(target: str, deferred: list[dict], limit: int = 3) -> str:
    """Appended to an `Unknown tool:` result so the model can correct itself.

    Fuzzy-matches the NAME first via difflib, because a wrong tool name is
    almost always a near-miss typo (`telegrm`, `manim_anim` for `manim`) rather
    than a vocabulary problem — and the lexical search used for finding tools
    by intent is bad at exactly that. Only if nothing is close enough does it
    fall back to asking the search, which at least retries the intent.
    """
    import difflib

    names = [str(d.get("name", "")) for d in deferred]
    close = difflib.get_close_matches(target, names, n=limit, cutoff=0.55)
    if close:
        return (f" Did you mean: {', '.join(close)}? "
                f"Use the exact name, or action=search to find it.")
    near = search(deferred, target, limit=limit)
    if not near:
        return ""
    return (f" Closest matches: "
            f"{', '.join(str(d.get('name', '?')) for d in near)}. "
            f"Search with action=search to get the exact name.")


def format_schemas(decls: list[dict]) -> str:
    """The result the model reads before calling a tool: everything needed to
    call it correctly, which means the FULL parameter schema — a summary here
    just produces malformed calls."""
    blocks: list[str] = []
    for d in decls:
        name = d.get("name", "?")
        desc = " ".join(str(d.get("description", "")).split())
        params = d.get("parameters") or {"type": "OBJECT", "properties": {}}
        blocks.append(
            f"### {name}\n{desc}\nparameters: "
            f"{json.dumps(params, ensure_ascii=False)}"
        )
    return "\n\n".join(blocks)


def hint_for_prompt(deferred: list[dict]) -> str:
    """One prompt line listing deferred tool NAMES.

    Names only — about 700 characters for all of them — because the model has to
    know a capability exists before it will think to search for it, but the full
    schemas are exactly what we are trying not to send.
    """
    if not deferred:
        return ""
    names = ", ".join(sorted(str(d.get("name", "")) for d in deferred))
    return (
        f"- toolbox: SEARCH AND RUN {len(deferred)} FURTHER TOOLS NOT LISTED ABOVE. "
        f"Use `toolbox action=search query=...` to find one and `toolbox "
        f"action=run tool=... parameters_json=...` to run it. "
        f"This is not a fallback — it is how you reach most of what you can do, "
        f"so search it before saying something is beyond you. Available: {names}."
    )
