"""
eval_ab — honest A/B evaluation of reply variants (Report R2).

Run two candidate answers (yours, or gemini-generated) through a judge
and get a persisted scorecard:

  * WITH a Gemini key  — an LLM judge scores both on relevance,
    clarity and actionability (JSON verdict, ladder-routed, free tier).
  * WITHOUT a key      — an offline heuristic judge (prompt-keyword
    coverage, structure, hedging penalties) labelled as heuristic.
    Never dressed up as an LLM opinion.

Artifacts: <base>/evals/eval-<ts>.json per run + `action=report`
aggregates winner tallies across runs. FREE ONLY: gemini ladder or
pure-local heuristics, zero paid APIs.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _evals_dir() -> Path:
    d = _base_dir() / "evals"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── judges ──────────────────────────────────────────────────────────────────

def _heuristic_score(prompt: str, reply: str) -> dict:
    """Offline scoring — transparent weights, no magic, no API."""
    p_words = {w for w in re.findall(r"[a-z0-9']{3,}",
                                     str(prompt).lower())}
    r = str(reply or "")
    r_low = r.lower()
    coverage = 0.0
    if p_words:
        coverage = sum(1 for w in p_words if w in r_low) / len(p_words)
    length = len(r.strip())
    length_score = (0.2 if length < 40 else
                    1.0 if 120 <= length <= 2400 else
                    0.7 if length < 120 else 0.5)
    structure = 0.4
    if re.search(r"^\s*([-*] |\d+\. )", r, re.M):
        structure += 0.3
    if re.search(r"\*\*[^*]+\*\*", r):
        structure += 0.15
    hedges = len(re.findall(r"\b(maybe|perhaps|kind of|sort of|i guess|"
                            r"not sure)\b", r_low))
    hedge_penalty = min(0.45, 0.15 * hedges)
    total = max(0.0, min(1.0, 0.45 * coverage + 0.3 * length_score +
                         0.25 * structure - hedge_penalty))
    return {"score": round(total, 3), "coverage": round(coverage, 3),
            "structure": round(structure, 3), "hedges": hedges}


def _llm_judge(prompt: str, a: str, b: str, criteria: str) -> dict | None:
    """Gemini judges → {'winner': 'a'|'b'|'tie', 'why': …}. None on any
    failure — caller falls back to the heuristic, honestly."""
    from core import gemini
    if not gemini.api_key():
        return None
    try:
        out = gemini.text([
            {"role": "user", "content":
                "You are an impartial evaluator. Judge the two candidate "
                f"answers to the prompt.\nPROMPT: {prompt}\n\n"
                f"CRITERIA: {criteria or 'relevance, clarity, actionability'}"
                f"\n\nANSWER A:\n{a[:3000]}\n\nANSWER B:\n{b[:3000]}\n\n"
                "Reply with ONLY one line: WINNER=a|b|tie REASON=<short>"},
        ])
        m = re.search(r"WINNER\s*=\s*(a|b|tie)\b.*?REASON\s*=\s*(.+)",
                      str(out or ""), re.I | re.S)
        if not m:
            return None
        return {"winner": m.group(1).lower(),
                "why": m.group(2).strip()[:300]}
    except Exception:
        return None


def _judge(prompt: str, a: str, b: str, criteria: str) -> dict:
    llm = _llm_judge(prompt, a, b, criteria)
    if llm:
        return {"judge": "gemini", **llm,
                "scores": {"a": None, "b": None}}
    sa, sb = _heuristic_score(prompt, a), _heuristic_score(prompt, b)
    if abs(sa["score"] - sb["score"]) < 0.05:
        winner, why = "tie", "heuristic scores within 0.05"
    else:
        winner = "a" if sa["score"] > sb["score"] else "b"
        why = (f"heuristic: coverage/structure/length — "
               f"a={sa['score']} vs b={sb['score']}")
    return {"judge": "heuristic", "winner": winner, "why": why,
            "scores": {"a": sa, "b": sb}}


# ── handler ─────────────────────────────────────────────────────────────────

def eval_ab(parameters: dict = None, player=None,
            session_memory=None) -> str:
    from core import privacy as _privacy
    blocked = _privacy.gate("eval_ab")
    if blocked:
        return blocked
    params = parameters or {}
    action = str(params.get("action", "run")).lower().strip()

    if action == "report":
        files = sorted(_evals_dir().glob("eval-*.json"))
        if not files:
            return "No eval runs yet — eval_ab prompt=… a=… b=… first."
        wins = {"a": 0, "b": 0, "tie": 0}
        judges = {"gemini": 0, "heuristic": 0}
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            wins[data.get("result", {}).get("winner", "tie")] = \
                wins.get(data.get("result", {}).get("winner", "tie"), 0) + 1
            judges[data.get("result", {}).get("judge", "?")] = \
                judges.get(data.get("result", {}).get("judge", "?"), 0) + 1
        return (f"Eval report: {len(files)} run(s) — "
                f"A wins {wins.get('a', 0)}, B wins {wins.get('b', 0)}, "
                f"ties {wins.get('tie', 0)} "
                f"(judges: {judges.get('gemini', 0)} gemini / "
                f"{judges.get('heuristic', 0)} heuristic).")

    prompt = str(params.get("prompt") or "").strip()
    a = str(params.get("a") or "").strip()
    b = str(params.get("b") or "").strip()
    if not prompt:
        return ("Give me a prompt to evaluate against — eval_ab "
                "prompt=\"…\" a=\"reply one\" b=\"reply two\".")
    if not a or not b:
        return ("Need BOTH candidates (a= and b=). Generate them first, "
                "then hand both here for judging.")
    criteria = str(params.get("criteria") or "").strip()

    result = _judge(prompt, a, b, criteria)
    record = {"ts": time.time(), "prompt": prompt[:600],
              "criteria": criteria, "a": a[:3000], "b": b[:3000],
              "result": result}
    path = _evals_dir() / f"eval-{int(record['ts'])}.json"
    try:
        path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    except Exception:
        path = None

    scores = result.get("scores") or {}
    score_line = ""
    if scores.get("a"):
        score_line = (f" Scores: a={scores['a']['score']} "
                      f"b={scores['b']['score']} (heuristic).")
    who = {"a": "A", "b": "B", "tie": "Tie"}[result["winner"]]
    return (f"Winner: {who} (judge={result['judge']}) — "
            f"{result['why']}.{score_line}"
            + (f" Saved: {path}" if path else ""))


TOOL = {
    "name": "eval_ab",
    "description": (
        "A/B-evaluate two candidate replies to a prompt. LLM judge via "
        "the free Gemini ladder when a key exists, transparent offline "
        "heuristic otherwise (labelled honestly). Saves a JSON scorecard "
        "per run; action=report aggregates win tallies. Use for 'compare "
        "these answers', 'which reply is better', prompt-tuning checks."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "run (default) | report."},
            "prompt": {"type": "STRING",
                       "description": "The user prompt both replies answer."},
            "a": {"type": "STRING", "description": "Candidate A text."},
            "b": {"type": "STRING", "description": "Candidate B text."},
            "criteria": {"type": "STRING",
                         "description": "Optional judging criteria."},
        },
        "required": [],
    },
    "handler": eval_ab,
}
