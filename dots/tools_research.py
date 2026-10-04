# dots/tools_research.py
"""Web-research tools for the Dot brain (doc §3.5).

Wired to the EXACT code paths JARVIS already ships (duplicate-check
recorded in docs/DOT_PLATFORM.md): DDG fan-out via actions/web_search,
page fetching/extraction via actions/scrape — nothing rebuilt.

Gates:
  * dot must have the `research` permission (enforced by the router in
    dots/tools.py BEFORE this module runs);
  * `research_mode` config (config/dots.json): parallel (default) |
    browser (page reader only) | disabled (both tools refuse honestly).

All network entry points are module-level seams (_ddg/_fetch/_extract/
_links) so tests run with zero network.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

PRIV = "research"

_MD_CAP = 6_000
_HTML_CAP = 500_000
_LINK_CAP = 8

SPECS = [
    {"type": "function",
     "function": {
         "name": "search_web",
         "description": ("Free web search — 1 to 3 queries in parallel "
                         "(DDG, keyless). Returns title/url/snippet per "
                         "hit, deduped by URL. Give the exact queries."),
         "parameters": {"type": "object",
                        "properties": {
                            "queries": {"type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 1, "maxItems": 3},
                        },
                        "required": ["queries"]}}},
    {"type": "function",
     "function": {
         "name": "read_public_page",
         "description": ("Fetch one public http(s) page and return its "
                         "main text plus top links. Use after "
                         "search_web to read a source."),
         "parameters": {"type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"]}}},
]


def allowed(dot: dict) -> bool:
    p = dot.get("permissions")
    return bool(isinstance(p, dict) and p.get("research"))


# ── seams (tests patch these; production hits the network) ───────────────────
def _ddg(query: str) -> list[dict]:
    from actions.web_search import _ddg_search
    return _ddg_search(query, max_results=6)


def _fetch(url: str) -> str:
    from actions.scrape import _fetch as fetch
    return fetch(url, timeout=15)


def _extract(html: str, url: str) -> tuple[str, str]:
    from actions.scrape import clean_html
    return clean_html(html, url, max_chars=_MD_CAP)


def _links(html: str, url: str) -> list[tuple[str, str]]:
    from actions.scrape import _top_links
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    try:
        return list(_top_links(html, host, limit=_LINK_CAP))
    except Exception:
        return []


def _research_mode() -> str:
    import json
    from pathlib import Path
    try:
        from config import get_base_dir
        raw = json.loads(
            (Path(get_base_dir()) / "config" / "dots.json")
            .read_text(encoding="utf-8"))
        mode = str(raw.get("research_mode", "parallel")).lower()
    except Exception:
        mode = "parallel"
    return mode if mode in ("parallel", "browser", "disabled") else "parallel"


def _mode_refusal(tool: str) -> str | None:
    mode = _research_mode()
    if mode == "disabled":
        return "denied: research_mode=disabled in config/dots.json"
    if mode == "browser" and tool == "search_web":
        return ("denied: research_mode=browser allows read_public_page "
                "only (no search fan-out)")
    return None


# ── tools ────────────────────────────────────────────────────────────────────
def run(dot: dict, name: str, args: dict) -> str:
    if name == "search_web":
        return _search(args)
    if name == "read_public_page":
        return _read(args)
    return f"unknown research tool: {name}"


def _search(args: dict) -> str:
    refuse = _mode_refusal("search_web")
    if refuse:
        return refuse
    raw = args.get("queries")
    if isinstance(raw, str):
        raw = [raw]
    queries = [str(q).strip() for q in (raw or []) if str(q).strip()]
    if not queries:
        return "search_web needs 1-3 queries"
    truncated = ""
    if len(queries) > 3:
        queries = queries[:3]
        truncated = "\n[note: 3 queries max — extra queries dropped]"
    if len(queries) == 1:
        batches = [_ddg(queries[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            batches = list(pool.map(_ddg, queries))
    seen: set[str] = set()
    lines: list[str] = []
    errors: list[str] = []
    for q, res in zip(queries, batches):
        if isinstance(res, dict) and "error" in res:
            errors.append(f"{q}: {res['error']}")
            continue
        for r in res or []:
            url = str(r.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            title = str(r.get("title") or "").strip()
            snippet = str(r.get("snippet") or "").strip()
            lines.append(f"- {title}\n  {url}\n  {snippet}")
    if lines:
        out = f"{len(lines)} results:\n" + "\n".join(lines)
        if errors:
            out += "\n[warnings] " + "; ".join(errors)
        return out + truncated
    if errors:
        return "search failed: " + "; ".join(errors)
    return "no results — rephrase the queries or read a known URL directly."


def _read(args: dict) -> str:
    refuse = _mode_refusal("read_public_page")
    if refuse:
        return refuse
    url = str(args.get("url") or "").strip()
    if not url:
        return "read_public_page needs a url"
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        got = scheme or "no scheme"
        return f"denied: only http(s) urls are allowed (got {got})"
    try:
        html = _fetch(url)
    except Exception as e:
        return f"error: could not fetch {url} ({e})"
    if not isinstance(html, str) or not html.strip():
        return f"error: {url} returned an empty body"
    if len(html) > _HTML_CAP:
        html = html[:_HTML_CAP]
    title, text = _extract(html, url)
    if not text.strip():
        return f"error: no readable text at {url}"
    links = _links(html, url)
    link_txt = ("\n" + "\n".join(f"- {t} → {u}" for t, u in links)
                if links else "")
    return (f"title: {title or '(untitled)'}\nurl: {url}{link_txt}\n"
            f"---\n{text}")
