# actions/research.py
"""research — multi-source research → a written markdown report.

WHAT IT DOES
    1. Runs several angle searches on the topic (DDG — free, no key).
    2. Fetches the top unique pages (same requests client discipline as
       scrape: HTTP(S) only, timeout, size cap).
    3. Synthesises a markdown report with numbered sources — through the
       Gemini model ladder when a key exists, and through an extractive
       fallback when it doesn't, so the tool NEVER depends on a key to
       return something useful.
    4. Saves the report to <base>/research/<slug>-<timestamp>.md and
       returns the path plus a preview.

WHY IT'S NOT A DUPLICATE
    web_search answers questions. scrape reads one page. This tool is the
    pipeline: query fan-out → source fetch → citation-bound writing → file.

FREE TOOLS ONLY: DuckDuckGo (search), requests (fetch), Gemini ladder
(optional synthesis — degrades to extractive summary without a key).
"""
from __future__ import annotations

import re
import time
from pathlib import Path


# ── IO seams (monkeypatched in tests) ────────────────────────────────────────

def _search(query: str, max_results: int = 6) -> list[dict]:
    """One DDG search. Returns [{title, snippet, url}, …]. Empty on failure."""
    from actions.web_search import _ddg_search          # shared DDG wrapper
    try:
        return _ddg_search(query, max_results=max_results)
    except Exception as e:
        print(f"[Research] ⚠️ search failed for {query!r}: {e}")
        return []


def _fetch_url(url: str, timeout: int = 20) -> str:
    """GET a page. Raises on non-HTTP schemes, bad status, over-size body."""
    from urllib.parse import urlparse

    from actions.scrape import _fetch                   # same client as scrape
    scheme = (urlparse(url).scheme or "").lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {scheme or '(none)'}")
    return _fetch(url, timeout=timeout)


def _synth_report(topic: str, docs: list[dict]) -> str | None:
    """Gemini-written markdown report, or None when no key / every model down."""
    from core import gemini

    if not gemini.api_key():
        return None
    lines = [
        f"Write a markdown research report on: {topic}",
        "",
        "Rules:",
        "- Use ONLY the sources below; cite them inline as [1], [2], …",
        "- Sections: ## Summary (3 sentences), ## Key findings (bullets),",
        "  ## In depth (2-3 paragraphs), ## Sources (numbered list).",
        "- Plain markdown, no preamble, no code fences.",
        "",
        "SOURCES:",
    ]
    for i, d in enumerate(docs, 1):
        lines.append(f"[{i}] {d['title']} — {d['url']}")
        lines.append(f"    {d['excerpt'][:1200]}")
    try:
        response = gemini.call("\n".join(lines), tier=gemini.SMART,
                               timeout_ms=45_000)
    except Exception as e:
        print(f"[Research] ⚠️ Gemini synthesis failed: {e}")
        return None
    if response is None:
        return None
    try:
        text = "".join(
            part.text for part in response.candidates[0].content.parts
            if getattr(part, "text", None))
    except Exception:
        return None
    return text.strip() or None


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


# ── helpers ──────────────────────────────────────────────────────────────────

def _queries_for(topic: str, depth: str) -> list[str]:
    """Angle fan-out: one plain query plus the angles depth allows."""
    qs = [topic]
    if depth in ("standard", "deep"):
        qs.append(f"{topic} latest developments 2026")
    if depth == "deep":
        qs.append(f"{topic} comparison review analysis")
        qs.append(f"{topic} problems limitations")
    return qs


def _fetch_sources(queries: list[str], limit: int) -> list[dict]:
    """Run searches, dedupe by URL, fetch pages → excerpt docs."""
    seen: set[str] = set()
    hits: list[dict] = []
    for q in queries:
        for h in _search(q):
            url = str(h.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            hits.append({"title": str(h.get("title") or url),
                         "url": url,
                         "snippet": str(h.get("snippet") or "")})
        if len(hits) >= limit * 3:                 # enough candidates
            break

    docs: list[dict] = []
    for h in hits:
        if len(docs) >= limit:
            break
        try:
            html = _fetch_url(h["url"], timeout=15)
        except Exception as e:
            print(f"[Research] skip {h['url']}: {e}")
            continue
        from actions.scrape import clean_html
        title, text = clean_html(html, h["url"], max_chars=6000)
        h["title"] = title or h["title"]
        h["excerpt"] = text or h["snippet"]
        if len(h["excerpt"]) >= 120:               # real content, not a stub
            docs.append(h)
    return docs


def _fallback_report(topic: str, docs: list[dict]) -> str:
    """Extractive report — no model needed. Honest about what it is."""
    lines = [f"# Research report: {topic}", "", "## Summary",
             f"Extractive digest of {len(docs)} sources "
             f"(compiled {time.strftime('%Y-%m-%d %H:%M')} local).",
             "", "## Key findings"]
    for i, d in enumerate(docs, 1):
        first = re.sub(r"\s+", " ", d["excerpt"])[:280]
        lines.append(f"- **{d['title']}** — {first}… [{i}]")
    lines += ["", "## In depth"]
    for i, d in enumerate(docs, 1):
        body = re.sub(r"\s+", " ", d["excerpt"])[:900]
        lines.append(f"### {i}. {d['title']}\n\n{body}…\n")
    lines += ["", "## Sources"]
    lines += [f"{i}. {d['title']} — {d['url']}" for i, d in enumerate(docs, 1)]
    return "\n".join(lines)


def _slug(topic: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return s[:60] or "report"


# ── handler ──────────────────────────────────────────────────────────────────

def research(parameters: dict = None, player=None, session_memory=None) -> str:
    from core import privacy as _privacy
    blocked = _privacy.gate("research")
    if blocked:
        return blocked
    params = parameters or {}
    topic = str(params.get("topic") or params.get("query") or "").strip()
    if not topic:
        return "Give me a topic — research <what you want investigated>."
    depth = str(params.get("depth") or "standard").strip().lower()
    if depth not in ("quick", "standard", "deep"):
        depth = "standard"
    limit = {"quick": 3, "standard": 5, "deep": 7}[depth]

    print(f"[Research] '{topic}' depth={depth} — searching…")
    docs = _fetch_sources(_queries_for(topic, depth), limit)
    if not docs:
        return (f"I found no usable sources for '{topic}'. "
                "Try a broader or differently-worded topic.")

    report = _synth_report(topic, docs) or _fallback_report(topic, docs)

    # save
    d = _base_dir() / "research"
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_slug(topic)}-{int(time.time())}.md"
        path.write_text(report, encoding="utf-8")
    except Exception as e:
        return (f"Report compiled from {len(docs)} sources but saving failed "
                f"({e}).\n\n{report[:1500]}")

    preview = re.sub(r"\s+", " ", report)[:400]
    return (f"Research report saved: {path} "
            f"({len(docs)} sources, depth={depth}).\nPreview: {preview}…")


TOOL = {
    "name": "research",
    "description": (
        "Research a topic across several sources and write a markdown "
        "report with numbered citations, saved to disk. Use for any "
        "'research X', 'find out about Y', 'compare Z' request that needs "
        "more than one page of reading. depth: quick (3 sources), "
        "standard (5), deep (7)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "string",
                "description": "What to research, phrased as a search topic.",
            },
            "depth": {
                "type": "string",
                "enum": ["quick", "standard", "deep"],
                "description": "How deep to go. Default standard.",
            },
        },
        "required": ["topic"],
    },
    "handler": research,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return research(params,
                    player=(ctx or {}).get("player"),
                    session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(run({"topic": "Rajasthan tourism in October", "depth": "standard"}))
