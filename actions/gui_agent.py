"""gui_agent — desktop computer-use loop: perceive → reason → act → verify.

The model gets a GOAL ("open Settings and turn on dark mode"), not a list
of clicks. Each step:

  1. PERCEIVE  real screenshot via actions.screen_processor._capture_screen
  2. REASON    one Gemini SMART call with the image → strict JSON decision
               ({"say": …, "act": {"type": click|key|hotkey|type|scroll|
               move|done|fail, …}}) — the PROVIDER seam is `_decide`, so a
               local VLM can replace it later without touching the loop.
  3. ACT       pyautogui primitives imported from actions.computer_control
               (_click/_press/_type/_scroll/_hotkey/_move)
  4. VERIFY    next step re-perceives from scratch — no blind chains;
               `done` must carry on-screen proof text; two identical
               consecutive decisions = stuck → honest failure.

Safety layers (all tested):
  * privacy gate FIRST — screenshots are user content; refusal happens
    BEFORE the capture (gui_agent ∈ CLOUD_TOOLS).
  * autonomy: observe → PREVIEW only (one frame, plan, zero acts);
    ask → needs confirm=yes (auto mode is interface-enhanced in
    main._execute_tool); act steps are MUTATING for observe anyway.
  * step budget (default 8, cap 15), cooperative cancel between steps
    (core/agent_runtime key "gui"), every act logged to a report on the
    DISPLAY surface.
"""
from __future__ import annotations

import base64
import json
import time

from core import agent_runtime as _rt
from core import autonomy as _autonomy
from core import privacy as _privacy

_KEY = "gui"
_MAX_STEPS_CAP = 15
_LAST_STATUS: dict = {}


# ── seams (tests inject fakes here) ─────────────────────────────────────

def _capture() -> tuple[bytes, str]:
    """Real screenshot → (jpeg bytes, mime). May raise (no display)."""
    from actions.screen_processor import _capture_screen
    return _capture_screen()


_LAST_DECIDE_ERR = ""      # honest failure reason from the last _decide


def _provider() -> str:
    """'gemini' (default) or 'ollama' — config key `gui_provider`.
    Ollama runs fully local (free, no key, nothing leaves the machine)."""
    try:
        from config import get_config
        p = str(get_config().get("gui_provider") or "gemini").lower().strip()
        return "ollama" if p in ("ollama", "local") else "gemini"
    except Exception:
        return "gemini"


def _ollama_cfg() -> tuple[str, str]:
    try:
        from config import get_config
        cfg = get_config()
    except Exception:
        cfg = {}
    url = str(cfg.get("ollama_url") or "http://127.0.0.1:11434").strip()
    model = str(cfg.get("gui_vlm_model") or "qwen2.5vl").strip()
    return url, model


def _prompt_text(goal: str, history: list[dict], plan_only: bool) -> str:
    """The decision protocol — shared by every provider verbatim."""
    hist_txt = "\n".join(
        f"- {h.get('say', '')} → {json.dumps(h.get('act') or {})}"
        for h in history[-5:]) or "- (first step)"
    mode_line = ("PLAN ONLY: reply with the first 3 actions you WOULD take, "
                 "under key 'plan' (array), do NOT act."
                 if plan_only else
                 "Choose the NEXT single action.")
    return (
        "You are a desktop GUI agent controlling this computer. The attached "
        "image is the CURRENT full screen (screenshot).\n\n"
        f"GOAL: {goal}\n\n"
        f"Steps so far:\n{hist_txt}\n\n"
        f"{mode_line}\n\n"
        "Reply with ONLY JSON (no prose, no fences):\n"
        '{"say": "one short sentence", '
        '"act": {"type": "click", "x": 123, "y": 456}}\n'
        "Allowed act types (pixels are absolute, top-left = 0,0):\n"
        '- {"type":"click","x":N,"y":N}\n'
        '- {"type":"key","key":"enter"}        (single key)\n'
        '- {"type":"hotkey","keys":"ctrl+s"}   (combo, + separated)\n'
        '- {"type":"type","text":"..."}        (literal text)\n'
        '- {"type":"scroll","direction":"down","amount":N}\n'
        '- {"type":"move","x":N,"y":N}\n'
        '- {"type":"done","proof":"text visible on screen proving success"}\n'
        '- {"type":"fail","reason":"why the goal is unreachable"}\n'
        "If the goal is already achieved, reply done with visible proof. "
        "Never invent pixels that are not in the image."
    )


def _decide_ollama(prompt: str, image: bytes, plan_only: bool = False
                   ) -> dict | None:
    """Local vision model via Ollama's HTTP API (stdlib urllib — no new
    deps, nothing leaves the machine). Returns parsed decision or None;
    _LAST_DECIDE_ERR carries the honest reason."""
    global _LAST_DECIDE_ERR
    import urllib.error
    import urllib.request
    url, model = _ollama_cfg()
    endpoint = url.rstrip("/") + "/api/generate"
    body = json.dumps({
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image).decode("ascii")],
        "stream": False,
        "options": {"temperature": 0},
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        _LAST_DECIDE_ERR = (
            f"ollama not reachable at {endpoint} ({e}) — install from "
            f"ollama.com, then: ollama serve && ollama pull {model}")
        return None
    text = str(data.get("response") or "")
    decision = _parse_decision(text, plan_only=plan_only)
    if decision is None:
        _LAST_DECIDE_ERR = (f"ollama ({model}) replied but not with usable "
                            f"JSON: {text[:160]!r}")
        return None
    _LAST_DECIDE_ERR = ""
    return decision


def _decide(goal: str, image: bytes, history: list[dict],
            plan_only: bool = False) -> dict | None:
    """Provider seam: routes by config `gui_provider` —
    gemini (SMART, cloud) or ollama (local VLM, free/offline).
    Never raises; _LAST_DECIDE_ERR explains any failure."""
    global _LAST_DECIDE_ERR
    prompt = _prompt_text(goal, history, plan_only)
    if _provider() == "ollama":
        return _decide_ollama(prompt, image, plan_only)
    from core import gemini
    contents = [
        prompt,
        {"inline_data": {"mime_type": "image/jpeg",
                         "data": base64.b64encode(image).decode("ascii")}},
    ]
    try:
        reply = gemini.call(contents, tier=gemini.SMART, timeout_ms=30_000)
    except Exception as e:
        _LAST_DECIDE_ERR = f"gemini: {e}"
        return None
    if reply is None or not getattr(reply, "text", None):
        _LAST_DECIDE_ERR = "gemini: empty reply"
        return None
    decision = _parse_decision(reply.text, plan_only=plan_only)
    if decision is None:
        _LAST_DECIDE_ERR = (f"gemini replied but not with usable JSON: "
                            f"{reply.text[:160]!r}")
        return None
    _LAST_DECIDE_ERR = ""
    return decision


def _parse_decision(text: str, plan_only: bool = False) -> dict | None:
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        s = s.rsplit("```")[0]
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if plan_only:
        return obj if ("plan" in obj or "act" in obj) else None
    act = obj.get("act")
    if not isinstance(act, dict) or not str(act.get("type", "")):
        return None
    obj["say"] = str(obj.get("say") or "")[:300]
    return obj


def _perform(act: dict) -> str:
    """Map a decision onto real input primitives (actions.computer_control).
    Raises on missing backend — caller counts strikes."""
    from actions import computer_control as cc
    kind = str(act.get("type") or "").lower()
    if kind == "click":
        return cc._click(x=int(act["x"]), y=int(act["y"]))
    if kind == "key":
        return cc._press(str(act.get("key") or "enter"))
    if kind == "hotkey":
        keys = [k.strip() for k in str(act.get("keys") or "").split("+")
                if k.strip()]
        if not keys:
            raise ValueError("hotkey needs keys")
        return cc._hotkey(*keys)
    if kind == "type":
        return cc._type(str(act.get("text") or ""))
    if kind == "scroll":
        return cc._scroll(str(act.get("direction") or "down"),
                          int(act.get("amount") or 3))
    if kind == "move":
        return cc._move(int(act["x"]), int(act["y"]))
    raise ValueError(f"unknown act type {kind!r}")


# ── the loop ────────────────────────────────────────────────────────────

def _run(goal: str, max_steps: int, player) -> str:
    rt = _rt
    rt.begin(_KEY)          # also clears any stale cancel flag
    history: list[dict] = []
    strikes = 0
    last_sig = ""
    outcome = "budget"
    started = time.time()
    try:
        for step in range(1, max_steps + 1):
            if rt.is_cancelled(_KEY):
                outcome = "cancelled"
                break
            # 1. perceive
            try:
                image, _mime = _capture()
            except Exception as e:
                outcome = f"capture failed: {e}"
                break
            # 2. reason (1 retry on garbage)
            decision = _decide(goal, image, history) or \
                _decide(goal, image, history)
            if not isinstance(decision, dict):
                outcome = "decision unavailable (model returned no usable JSON)"
                if _LAST_DECIDE_ERR:
                    outcome += f" — {_LAST_DECIDE_ERR}"
                break
            act = decision.get("act") or {}
            kind = str(act.get("type") or "").lower()
            history.append({"say": decision.get("say", ""), "act": act,
                            "step": step})
            if kind == "done":
                outcome = f"done — {str(act.get('proof') or '')[:200]}"
                break
            if kind == "fail":
                outcome = f"failed — {str(act.get('reason') or '')[:200]}"
                break
            # 3. act (with stuck + strike guards)
            sig = json.dumps(act, sort_keys=True)
            if sig == last_sig:
                outcome = f"stuck — same decision twice ({sig[:120]})"
                break
            last_sig = sig
            try:
                _perform(act)
                strikes = 0
            except Exception as e:
                strikes += 1
                history.append({"say": f"act error: {e}", "act": act,
                                "step": step})
                if strikes >= 2:
                    outcome = f"act backend unavailable: {e}"
                    break
            time.sleep(0.15)            # settle frame before re-perceive
        else:
            outcome = f"budget exhausted after {max_steps} steps"
    finally:
        elapsed = time.time() - started
        _rt.finish(_KEY)
        _LAST_STATUS.update({
            "goal": goal, "outcome": outcome, "steps": len(history),
            "seconds": round(elapsed, 1),
            "log": [f"L{h['step']} {h['say'][:80]} → "
                    f"{json.dumps(h['act'])[:120]}" for h in history],
        })
    return _report(goal, outcome, history, player)


def _report(goal: str, outcome: str, history: list[dict], player) -> str:
    head = f"GUI agent [{'OK' if outcome.startswith('done') else 'STOP'}] " \
           f"— {goal[:60]}\nOutcome: {outcome}"
    if history:
        lines = [head, ""]
        for h in history:
            lines.append(f"  L{h['step']} · {h['say'][:110]}")
        body = "\n".join(lines)
    else:
        body = head
    if player is not None:
        try:
            player.show_content("GUI AGENT — " + goal[:40], body)
        except Exception:
            pass
    return body


def gui_agent(parameters: dict, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()

    if action == "status":
        if not _LAST_STATUS:
            return "No gui_agent run yet."
        st = _LAST_STATUS
        return (f"Last GUI run: {st['goal'][:60]}\nOutcome: {st['outcome']}"
                f"\nSteps: {st['steps']} in {st['seconds']}s"
                + ("\n" + "\n".join(st["log"][:10]) if st.get("log") else ""))

    if action == "cancel":
        if _rt.cancel(_KEY):
            return "Cancelling the GUI agent — it will stop before the next act."
        return "No GUI agent run to cancel."

    goal = str(params.get("goal") or params.get("description") or "").strip()
    if not goal:
        return ("gui_agent needs a goal, e.g. goal='open Settings and turn "
                "on dark mode'. Add confirm=yes to execute; action=preview "
                "plans one step without touching the screen.")

    # 1. privacy BEFORE any capture — screenshots are user content
    blocked = _privacy.gate("gui_agent")
    if blocked:
        return blocked

    mode = _autonomy.get_mode()
    confirm = str(params.get("confirm") or "").lower() in ("yes", "true", "1")
    try:
        max_steps = max(1, min(_MAX_STEPS_CAP,
                               int(params.get("max_steps", 8))))
    except (TypeError, ValueError):
        max_steps = 8

    preview = action == "preview" or mode == "observe" or \
        (not confirm and mode != "auto")
    if preview:
        # one frame, one decide, ZERO acts
        try:
            image, _mime = _capture()
        except Exception as e:
            return f"Preview capture failed: {e}"
        decision = _decide(goal, image, [], plan_only=True) or \
            _decide(goal, image, [], plan_only=False)
        if not isinstance(decision, dict):
            note = f" — {_LAST_DECIDE_ERR}" if _LAST_DECIDE_ERR else ""
            return ("Preview unavailable: the decision model returned "
                    f"no usable JSON.{note}")
        plan = decision.get("plan")
        if isinstance(plan, list) and plan:
            steps_txt = "\n".join(f"  {i + 1}. {json.dumps(s)[:140]}"
                                  for i, s in enumerate(plan[:5]))
        else:
            act = decision.get("act") or {}
            steps_txt = f"  1. {json.dumps(act)[:160]}"
        say = str(decision.get("say") or "")[:200]
        tail = ("" if mode == "observe" or action == "preview" else
                "\n(Re-run with confirm=yes to execute.)")
        if mode == "observe":
            tail = "\n(Autonomy OBSERVE — nothing will execute until you " \
                   "switch to ask/auto.)"
        return (f"GUI preview for: {goal}\nPlan:\n{steps_txt}\n"
                f"Note: {say}{tail}")

    if _KEY in _rt.active():
        return "A GUI agent run is already active — cancel it first."
    return _run(goal, max_steps, player)


TOOL = {
    "name": "gui_agent",
    "description": (
        "Agentic desktop control: given a GOAL, repeatedly screenshots the "
        "real screen, decides the next action (click/key/type/scroll), does "
        "it, and re-checks until done (with on-screen proof) or the step "
        "budget runs out. Args: goal (e.g. 'open Settings and enable dark "
        "mode'), confirm=yes to execute (auto mode needs none), max_steps "
        "1-15 (default 8), action=preview to see the plan without touching "
        "the screen, action=cancel to stop between steps, action=status for "
        "the last run. Screenshots go to the vision model — blocked under "
        "privacy mode. Use for multi-step GUI tasks; single clicks are "
        "better served by computer_control."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {"type": "STRING",
                     "description": "What to achieve on screen"},
            "action": {"type": "STRING",
                       "description": "run (default w/ confirm) | preview | "
                                      "cancel | status"},
            "confirm": {"type": "STRING",
                        "description": "yes to execute acts (required in "
                                       "ask mode)"},
            "max_steps": {"type": "INTEGER",
                          "description": "1-15, default 8"},
        },
        "required": ["goal"],
    },
    "handler": gui_agent,
}
