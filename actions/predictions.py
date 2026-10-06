"""
predictions — make a claim, get it ANNOTATED with ground truth, push
the verdict as a HUD chip + to Telegram (Report M: "chips/
annotation→telegram").

Why this exists: monitors watch TOPICS (news). Nothing tracks a CLAIM
JARVIS or the user makes ("it'll ship Friday", "rain by 6", "CPU will
drop after restart") and later records whether it came true. This is
that loop:

  predict  → pending claim stored (text + optional expected value)
  resolve  → observed value annotated in → confirmed/refuted verdict
             (numeric tolerance or substring rule; custom verdict
             allowed) + HUD CHIP via the content panel
  announce → the annotated verdict pushed to Telegram (allowlisted
             chat, bot API — reuse telegram_rx token/settings)
  list     → pending/resolved with their annotations

Ground truth is IMMUTABLE: a resolved prediction refuses overwrites
(append-only timeline keeps the audit trail honest).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _path() -> Path:
    return _base_dir() / "memory" / "predictions.json"


def _load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows[-100:], indent=1), encoding="utf-8")


# ── verdict rules (pure — tested) ───────────────────────────────────────────

def _numbers(text: str) -> list[float]:
    out = []
    for m in re.findall(r"-?\d+(?:[.,]\d+)?", str(text or "")):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def verdict_for(expected: str, observed: str, tol: float = 0.05) -> str:
    """confirmed | refuted from expected vs observed. Numeric when both
    sides parse (relative tolerance 5%), else casefold substring."""
    exp, obs = str(expected or "").strip(), str(observed or "").strip()
    if not exp or not obs:
        return ""
    e_nums, o_nums = _numbers(exp), _numbers(obs)
    if e_nums and o_nums:
        e, o = e_nums[0], o_nums[0]
        if e == o:
            return "confirmed"
        ref = max(abs(e), abs(o), 1e-9)
        return "confirmed" if abs(e - o) / ref <= tol else "refuted"
    if exp.casefold() in obs.casefold() or obs.casefold() in exp.casefold():
        return "confirmed"
    return "refuted"


# ── actions ─────────────────────────────────────────────────────────────────

def _find(rows: list[dict], token: str) -> dict | None:
    token = str(token or "").strip()
    if not token:
        return None
    for r in reversed(rows):
        if str(r.get("id")) == token or \
                token.casefold() in str(r.get("text", "")).casefold():
            return r
    return None


def predictions(parameters: dict | None = None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()
    rows = _load()

    if action in ("predict", "add", "new"):
        text = str(params.get("text") or params.get("claim") or "").strip()
        if not text:
            return "Give me the claim — predictions action=predict text=…"
        expected = str(params.get("expected") or "").strip()
        row = {"id": str(int(time.time() * 1000))[-8:],
               "text": text[:300], "expected": expected[:200],
               "created_at": time.time(), "status": "pending",
               "annotations": []}
        rows.append(row)
        _save(rows)
        exp = f" (expect: {expected})" if expected else ""
        return (f"Prediction #{row['id']} recorded{exp}: {text} — "
                "resolve it later with action=resolve to annotate ground "
                "truth.")

    if action in ("resolve", "annotate"):
        token = str(params.get("id") or params.get("query") or "").strip()
        observed = str(params.get("observed") or "").strip()
        if not token:
            return "resolve needs id= (or id text) — see action=list."
        if not observed:
            return "resolve needs observed=… (what actually happened)."
        row = _find(rows, token)
        if row is None:
            return f"No prediction matches {token!r} — action=list first."
        if row.get("status") == "resolved":
            return (f"Already resolved {row['id']} at "
                    f"{row.get('resolved_at')} — ground truth is "
                    "append-only; record a NEW prediction instead.")
        verdict = str(params.get("verdict") or "").strip().lower()
        if verdict not in ("confirmed", "refuted"):
            verdict = verdict_for(row.get("expected", ""), observed)
            if not verdict:
                verdict = "confirmed" if str(params.get("ok", "")).strip() \
                    in ("1", "true", "yes") else ""
            if not verdict:
                return ("No expected value to compare — pass "
                        "verdict=confirmed|refuted or ok=true, or give "
                        "expected= when you predict.")
        row["status"] = "resolved"
        row["resolved_at"] = time.time()
        row["annotations"].append({"at": time.time(),
                                   "observed": observed[:300],
                                   "verdict": verdict})
        _save(rows)
        chip = (f"{verdict.upper()}: {row['text']} → observed: "
                f"{observed[:160]}")
        if player is not None:
            try:
                player.show_content("PREDICTION", chip)
            except Exception:
                pass
        return (f"Annotated: #{row['id']} {verdict} — {row['text']} "
                f"(observed: {observed[:200]}). HUD chip shown"
                + ("." if player is None else " and delivered.")
                + f" Announce: predictions action=announce id={row['id']}")

    if action in ("announce", "push", "telegram"):
        token = str(params.get("id") or params.get("query") or "").strip()
        row = _find(rows, token)
        if row is None:
            return f"No prediction matches {token!r} — action=list first."
        if row.get("status") != "resolved":
            return ("Still pending — resolve it first so there is a "
                    "ground-truth annotation to announce.")
        ann = (row.get("annotations") or [{}])[-1]
        line = (f"JARVIS prediction #{row['id']} [{ann.get('verdict', '?')}] "
                f"{row['text']} — observed: {ann.get('observed', '?')}")
        from actions.telegram_tx import send
        return send(line)

    if action in ("pending", "resolved", "list"):
        want = {"pending": "pending", "resolved": "resolved"}.get(action)
        picked = [r for r in reversed(rows)
                  if want is None or r.get("status") == want]
        if not picked:
            return f"No {want or ''} predictions — start with " \
                   "action=predict text=…"
        out = []
        for r in picked[:12]:
            line = f"- #{r['id']} [{r['status']}] {r['text']}"
            for a in (r.get("annotations") or [])[-1:]:
                line += f" → {a['verdict']}: {a['observed'][:120]}"
            out.append(line)
        return f"{len(picked)} prediction(s):\n" + "\n".join(out)

    return "action must be predict | resolve | announce | list | pending | resolved."


TOOL = {
    "name": "predictions",
    "description": (
        "Ground-truth loop for claims: action=predict text=… "
        "[expected=…] records a prediction; action=resolve id=… "
        "observed=… annotates it confirmed/refuted (numeric 5% "
        "tolerance or substring rule; verdict= override) and pops a "
        "HUD chip; action=announce id=… pushes the annotation to "
        "Telegram (allowlisted chat); list/pending/resolved show the "
        "audit trail. Ground truth is append-only (no overwrites). "
        "Use for 'track this prediction', 'did X come true', 'tell me "
        "on telegram when it happens'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "predict | resolve | announce | "
                                      "list | pending | resolved."},
            "text": {"type": "STRING", "description": "The claim (predict)."},
            "expected": {"type": "STRING",
                         "description": "Expected value for auto-verdict."},
            "id": {"type": "STRING",
                   "description": "Prediction id or text fragment."},
            "observed": {"type": "STRING",
                         "description": "What actually happened (resolve)."},
            "verdict": {"type": "STRING",
                        "description": "confirmed | refuted (override)."},
        },
        "required": [],
    },
    "handler": predictions,
}
