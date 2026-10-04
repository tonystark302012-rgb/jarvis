# dots/learning.py
"""Automatic learning → skill DRAFTS, owner publishes (doc §3.8, T9).

Deterministic v1 miner (no LLM needed, fully testable):
  * scan conversation messages (`dot:`/`page:`/`task:` convos);
  * every USER message whose next dot reply in the SAME convo is a
    completed answer (not an honest brain failure) = one "ok" occurrence;
  * normalize to a task SHAPE (lowercase, urls/digits/punctuation
    stripped) → group; ≥ MIN_COUNT occurrences of the same shape with a
    long-enough shape → one `skills(status='draft')` row;
  * fingerprint lives in `source_note` (`fp:<16 hex>`) so re-mining —
    even after publish/archive — NEVER creates a duplicate draft;
  * drafts are NEVER auto-published: only POST /api/skills/{id}/publish
    (owner) moves them to `published`, and only published skills reach
    a Dot's prompt/tools (T9).

Optional seam: `set_summariser(fn(shape, examples) -> str)` lets an LLM
write the body; without it the body is the honest deterministic
template listing the observed examples.
"""
from __future__ import annotations

import hashlib
import re

from . import brain, store

MIN_COUNT = 3
_SHAPE_MIN = 10                # chars after normalization — "hi" x3 ignored
_TITLE_CAP = 80

_summariser = None


def set_summariser(fn) -> None:
    """Optional LLM summariser seam (tests inject; production may set
    a local-LLM writer). None → deterministic template body."""
    global _summariser
    _summariser = fn


def shape(text: str) -> str:
    """Task shape: lowercase, urls/digits/punct/space collapsed."""
    t = str(text or "").lower()
    t = re.sub(r"https?://\S+|www\.\S+", "<url>", t)
    t = re.sub(r"\d+", "<n>", t)
    t = re.sub(r"[^\w\s<>]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:160]


def fingerprint(shape_text: str) -> str:
    return hashlib.sha1(shape_text.encode("utf-8")).hexdigest()[:16]


def _ok_reply(text: str) -> bool:
    return not any(str(text or "").startswith(p)
                   for p in brain.HONEST_FAILURES)


def _collect() -> dict:
    """shape → {shape, occs: [(convo, text), ...]} with ok markers."""
    rows = store.scan_for_mining()
    by_convo: dict[str, list[dict]] = {}
    for r in rows:
        by_convo.setdefault(r["convo_key"], []).append(r)
    groups: dict[str, dict] = {}
    for convo, msgs in by_convo.items():
        for i, m in enumerate(msgs):
            if m["role"] != "user":
                continue
            nxt = next((x for x in msgs[i + 1:] if x["role"] == "dot"),
                       None)
            if nxt is None or not _ok_reply(nxt["content"]):
                continue                  # no completed answer yet
            sh = shape(m["content"])
            if len(sh) < _SHAPE_MIN:
                continue                  # trivial chit-chat is no skill
            g = groups.setdefault(sh, {"shape": sh, "occs": []})
            g["occs"].append((convo, str(m["content"])))
    return groups


def _template_body(sh: str, occs: list) -> str:
    lines = [
        f"# {sh}",
        "",
        f"Task pattern (seen {len(occs)} times): {sh}",
        "",
        "Observed requests:",
    ]
    for convo, text in occs[:5]:
        lines.append(f"- [{convo}] {text[:160]}")
    lines += [
        "",
        "How to apply: when the owner asks for this shape, follow the",
        "same approach that worked in the examples above.",
        "",
        "_Draft mined automatically — the owner publishes or archives "
        "it; it is never active until published._",
    ]
    return "\n".join(lines)


def mine() -> list[dict]:
    """Scan conversations → new draft skills (idempotent by fingerprint).
    Returns the drafts created THIS call."""
    created: list[dict] = []
    for sh, g in _collect().items():
        if len(g["occs"]) < MIN_COUNT:
            continue
        fp = fingerprint(sh)
        if store.find_skill_by_fp(fp) is not None:
            continue                     # mined/published/archived before
        examples = [t for _, t in g["occs"]]
        if _summariser is not None:
            try:
                body = str(_summariser(sh, examples))
            except Exception:
                body = _template_body(sh, g["occs"])
        else:
            body = _template_body(sh, g["occs"])
        title = examples[-1][:_TITLE_CAP]
        note = f"fp:{fp} mined x{len(g['occs'])}"
        try:
            created.append(store.create_skill_draft(title, body, note))
        except Exception as e:
            print(f"[Dots/learning] draft insert failed: {e}")
    return created
