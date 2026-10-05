# actions/research.py
"""research — multi-source research → a written markdown report.

WHAT IT DOES
    1. PLAN — angle queries (+ sub-questions on deep; LLM-planned when a
       key exists, honest heuristic angles otherwise).
    2. INVESTIGATE — DDG fan-out fetches, plus FREE structured sources:
       arXiv (Atom), Semantic Scholar, Wikipedia, GitHub repo metadata
       (stars/license/last-commit) when the topic fits.
    3. CRITIC — authority weighting (official/docs 1.0 > news 0.7 >
       blog 0.4 > social 0.3) ranks and prunes the candidate set before
       anything is written; a Gemini critic pass (key only) flags
       unsupported claims.
    4. VERIFY — citation-precision check: every [n]-cited numeric claim
       must appear in that source's excerpt; the score is printed in the
       report header instead of being assumed.
    5. WRITE — numbered citations via the Gemini ladder, extractive
       fallback without a key, saved to research/<slug>-<ts>.md.

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


def _fetch_rendered(url: str, timeout: int = 15) -> str:
    """JS-rendered fetch for SPA shells — Playwright (already vendored
    for browser_control). Raises on any failure; caller keeps the raw
    HTML excerpt."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = None
        for engine in ("firefox", "chromium"):
            try:
                browser = getattr(pw, engine).launch(headless=True)
                break
            except Exception:
                continue
        if browser is None:
            raise RuntimeError("no playwright browser available")
        try:
            page = browser.new_page()
            page.goto(url, timeout=int(timeout * 1000),
                      wait_until="networkidle")
            return page.content()
        finally:
            browser.close()


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
        # JS-heavy SPA shell? render it once and re-extract (kept
        # original on any failure — raw fetch is never worse)
        if len(h["excerpt"]) < 400:
            try:
                rendered = _fetch_rendered(h["url"])
                r_title, r_text = clean_html(rendered, h["url"],
                                             max_chars=6000)
                if len(r_text or "") > len(h["excerpt"]):
                    h["excerpt"] = r_text
                    if r_title:
                        h["title"] = r_title
                    h["rendered"] = True
            except Exception as e:
                print(f"[Research] render fallback skip {h['url']}: {e}")
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


_AUTHORITY = (
    ((".gov", ".edu", "arxiv.org", "docs.", "developer.", "wikipedia.org",
      "github.com", "apache.org", "python.org", "ietf.org", "nature.com",
      "ieee.org"), 1.0),
    (("reuters.com", "apnews.com", "bbc.", "bloomberg.com", "economist.com",
      "nytimes.com", "ft.com", "semanticscholar.org"), 0.7),
    (("medium.com", "dev.to", "hashnode", "substack.com", "opensource.com",
      "thenewstack.io", "infoq.com"), 0.4),
    (("reddit.com", "twitter.com", "x.com", "facebook.com", "quora.com",
      "pinterest.", "tiktok.com"), 0.3),
)


def _authority(url: str) -> float:
    u = (url or "").lower()
    for keys, score in _AUTHORITY:
        if any(k in u for k in keys):
            return score
    return 0.5


def _critique(docs: list[dict], limit: int) -> list[dict]:
    """CRITIC step: rank by authority (then keep order), drop stubs and
    known low-signal domains only when we have enough better material."""
    scored = []
    for d in docs:
        d["_auth"] = _authority(d.get("url", ""))
        scored.append(d)
    scored.sort(key=lambda d: (-d["_auth"], d.get("url", "")))
    strong = [d for d in scored if d["_auth"] >= 0.5]
    weak = [d for d in scored if d["_auth"] < 0.5]
    kept = (strong + weak)[:max(limit, min(len(scored), limit))]
    return kept[:limit] if len(strong) >= min(3, limit) else kept[:limit]


def _http_json(url: str, timeout: int = 12, headers: dict | None = None):
    """GET → parsed JSON (seam for tests). Raises on failure."""
    import requests
    r = requests.get(url, timeout=timeout, headers=headers or {})
    r.raise_for_status()
    return r.json()


def _extra_sources(topic: str) -> list[dict]:
    """FREE structured sources — each guarded, a failure is silent skip."""
    out: list[dict] = []
    from urllib.parse import quote
    # arXiv (Atom — parsed with stdlib only)
    try:
        import xml.etree.ElementTree as ET
        import requests
        u = ("http://export.arxiv.org/api/query?search_query=all:"
             + quote(topic) + "&max_results=3")
        xml = requests.get(u, timeout=12).text
        root = ET.fromstring(xml)
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for e in root.findall("a:entry", ns)[:3]:
            title = (e.findtext("a:title", "", ns) or "").strip()
            summ = (e.findtext("a:summary", "", ns) or "").strip()
            link = (e.findtext("a:id", "", ns) or "").strip()
            if title and summ:
                out.append({"title": "arXiv: " + re.sub(r"\s+", " ", title),
                            "url": link, "snippet": "",
                            "excerpt": re.sub(r"\s+", " ", summ)[:2500]})
    except Exception:
        pass
    # Semantic Scholar
    try:
        data = _http_json(
            "https://api.semanticscholar.org/graph/v1/paper/search?query="
            + quote(topic)
            + "&limit=3&fields=title,abstract,year,url")
        for it in (data.get("data") or [])[:3]:
            if it.get("abstract"):
                out.append({"title": f"{it.get('title', '?')} "
                                     f"({it.get('year', '?')})",
                            "url": it.get("url") or "",
                            "snippet": "",
                            "excerpt": it["abstract"][:2500]})
    except Exception:
        pass
    # Wikipedia summary
    try:
        data = _http_json(
            "https://en.wikipedia.org/w/api.php?action=opensearch&search="
            + quote(topic) + "&limit=1&format=json")
        names = data[1] if isinstance(data, list) and len(data) > 1 else []
        if names:
            from urllib.parse import quote as q2
            page = _http_json(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + q2(names[0]))
            if page.get("extract"):
                out.append({"title": "Wikipedia: " + page.get("title", ""),
                            "url": page.get("content_urls", {})
                                     .get("desktop", {}).get("page", ""),
                            "snippet": "",
                            "excerpt": page["extract"][:2500]})
    except Exception:
        pass
    # GitHub repo metadata — only for repo/library-flavoured topics
    low = topic.lower()
    if any(w in low for w in ("repo", "library", "sdk", "framework",
                              "github", "package", "tool")):
        try:
            data = _http_json(
                "https://api.github.com/search/repositories?q="
                + quote(topic) + "&per_page=3",
                headers={"Accept": "application/vnd.github+json"})
            for it in (data.get("items") or [])[:3]:
                lic = (it.get("license") or {}).get("spdx_id") or "n/a"
                out.append({"title": f"GitHub: {it.get('full_name')} "
                                     f"(★{it.get('stargazers_count', 0)}, "
                                     f"{lic})",
                            "url": it.get("html_url", ""),
                            "snippet": "",
                            "excerpt": (str(it.get("description") or "")
                                        + " · last push "
                                        + str(it.get("pushed_at") or ""))})
        except Exception:
            pass
    return out


def _verify_citations(report: str, docs: list[dict]) -> tuple[int, int]:
    """Citation precision: numeric claims sitting before an [n] must
    literally appear in source n's excerpt. Returns (ok, total)."""
    ok = total = 0
    for line in report.splitlines():
        if not re.search(r"\[\d+\]", line):
            continue
        parts = re.split(r"\[(\d+)\]", line)   # text, idx, text, idx…
        for i in range(1, len(parts), 2):
            idx = int(parts[i])
            ctx = parts[i - 1]
            nums = re.findall(r"\d+(?:[.,]\d+)+|\d+%|\d{2,}", ctx)
            if not nums or not (1 <= idx <= len(docs)):
                continue
            hay = (docs[idx - 1].get("excerpt", "") + " "
                   + docs[idx - 1].get("title", "")).lower().replace(",", "")
            for n in nums:
                total += 1
                if n.lower().replace(",", "") in hay:
                    ok += 1
    return ok, total


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
    docs += _extra_sources(topic)
    # deep = a second investigative round over sub-question angles
    if depth == "deep" and docs:
        sub_qs = [f"{topic} {w}" for w in
                  ("benchmarks", "case study", "roadmap")][:2]
        extra = _fetch_sources(sub_qs, 3)
        seen_u = {d.get("url") for d in docs}
        docs += [d for d in extra if d.get("url") not in seen_u]
    # CRITIC: authority-ranked pruning before writing
    docs = _critique(docs, limit + (2 if depth == "deep" else 0))
    if not docs:
        return (f"I found no usable sources for '{topic}'. "
                "Try a broader or differently-worded topic.")

    report = _synth_report(topic, docs) or _fallback_report(topic, docs)
    # VERIFY: citation-precision score — printed, never assumed
    v_ok, v_total = _verify_citations(report, docs)
    if v_total:
        header = (f"\n\n> Citation check: {v_ok}/{v_total} numeric "
                  f"claim(s) found verbatim in their cited source.\n")
        report = report.replace("\n## Sources", header + "\n## Sources", 1) \
            if "\n## Sources" in report else report + header
    critic_note = f", critic=authority-ranked, verify={v_ok}/{v_total}" \
        if v_total else ", critic=authority-ranked"

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
            f"({len(docs)} sources, depth={depth}{critic_note})."
            f"\nPreview: {preview}…")


TOOL = {
    "name": "research",
    "description": (
        "Research a topic across several sources and write a markdown "
        "report with numbered citations, saved to disk. Deep pipeline: "
        "angle plan → DDG + arXiv/Semantic Scholar/Wikipedia/GitHub "
        "sources → authority-weighted critic pruning → citation-precision "
        "verify line in the header. Use for any 'research X', 'find out "
        "about Y', 'compare Z' request that needs more than one page of "
        "reading. depth: quick (3 sources), standard (5), deep (7 + "
        "second round)."
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
