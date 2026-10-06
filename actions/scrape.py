"""
scrape — fetch a URL and return clean, readable text.

The deep-research building block: pull a page, strip the chrome, hand the
meat to the model (or save it). BeautifulSoup when present, stdlib
HTMLParser fallback otherwise — one of them is always there.

Read-only: GET requests only, no form submissions, no cookies kept.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe",
              "nav", "footer", "header"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4",
               "h5", "h6", "section", "article", "blockquote", "pre"}
_TITLE_TAG = "title"


class _TextExtractor(HTMLParser):
    """Stdlib fallback: keep text, drop chrome, mark block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._in_title = False
        self.parts: list[str] = []
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == _TITLE_TAG:
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag == _TITLE_TAG:
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def _extract_bs4(html: str, url: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string or "").strip() if soup.title else ""
    for tag in soup(_SKIP_TAGS):
        tag.decompose()
    # Prefer the main content region when the page marks it.
    main = soup.find("main") or soup.find("article") \
        or soup.find(attrs={"role": "main"}) or soup.body or soup
    text = main.get_text(separator="\n")
    return title, text


def _extract_std(html: str, url: str) -> tuple[str, str]:
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.title.strip(), "".join(p.parts)


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln:
            blank = 0
            out.append(ln)
        else:
            blank += 1
            if blank == 1:
                out.append("")
    return "\n".join(out).strip()


def clean_html(html: str, url: str = "", max_chars: int = 8000) -> tuple[str, str]:
    """(title, clean_text) from raw HTML. bs4 when available, else stdlib."""
    title, text = "", ""
    try:
        title, text = _extract_bs4(html, url)
    except Exception:
        title, text = _extract_std(html, url)
    if not text.strip():
        title, text = _extract_std(html, url)
    text = _tidy(text)
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n… [truncated]"
    return title, text


def _fetch(url: str, timeout: int = 20) -> str:
    import requests
    resp = requests.get(
        url, timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (compatible; JarvisScrape/1.0)"},
    )
    resp.raise_for_status()
    # requests' guessed encoding beats ISO-8859-1 defaults on most sites.
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def scrape(parameters: dict | None = None, player=None, session_memory=None) -> str:
    from core import privacy as _privacy
    blocked = _privacy.gate("scrape")
    if blocked:
        return blocked
    params = parameters or {}
    url = str(params.get("url", "")).strip()
    if not url:
        return "No URL given."
    # Any explicit non-HTTP scheme (ftp:, file:, javascript:) is rejected
    # BEFORE the bare-host defaulting below, so 'ftp://x' never becomes a
    # fetch attempt.
    if re.match(r"^[a-z][a-z0-9+.\-]*:", url, re.I) and not re.match(
            r"^https?://", url, re.I):
        return f"Unsupported URL: {url}"
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return f"Unsupported URL: {url}"

    mode = str(params.get("mode", "text")).lower().strip()
    try:
        max_chars = max(500, min(40_000, int(params.get("max_chars", 8000))))
    except (TypeError, ValueError):
        max_chars = 8000

    try:
        html = _fetch(url)
    except Exception as e:
        return f"Could not fetch {parsed.netloc}: {e}"

    if mode == "structured":
        # Roadmap: structured web extraction — JSON-LD (schema.org),
        # OpenGraph, Twitter cards + trafilatura main-content when the
        # lib is installed. Falls back to raw text if nothing parses.
        result = _structured_extract(html, url, max_chars=max_chars)
        if player is not None:
            try:
                player.show_content(f"STRUCTURED — {parsed.netloc}"[:48],
                                    result[:4000])
            except Exception:
                pass
        return result

    title, text = clean_html(html, url, max_chars=max_chars)

    if mode == "links":
        links = _top_links(html, parsed.netloc, limit=40)
        if not links:
            return f"No links found on {parsed.netloc}."
        out = "\n".join(f"- {t}  <{u}>" for t, u in links)
        result = f"Links on {title or parsed.netloc}:\n{out}"
    elif mode == "title":
        result = title or "(no title)"
    else:
        head = f"# {title}\n({url})\n\n" if title else f"({url})\n\n"
        result = head + (text or "(page had no readable text)")

    if player is not None:
        try:
            player.show_content(f"SCRAPE — {title or parsed.netloc}"[:48], result[:4000])
        except Exception:
            pass
    return result


def _structured_extract(html: str, url: str, max_chars: int = 8000) -> str:
    """JSON-LD + OpenGraph + Twitter-card extraction (free, stdlib) with
    an optional trafilatura main-content pass when that lib is present.

    Returns a speakable markdown block: metadata table, then the clean
    article body. Never raises — partial metadata still beats none."""
    import json as _json
    from html.parser import HTMLParser

    meta: dict[str, str] = {}
    ld_objects: list[dict] = []

    class _HeadParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._in_ld = False
            self._ld_buf: list[str] = []
            self._is_title = False
            self._title_buf: list[str] = []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "meta":
                key = (a.get("property") or a.get("name") or "").lower()
                val = (a.get("content") or "").strip()
                if key.startswith(("og:", "twitter:", "article:",
                                   "description", "author",
                                   "keywords", "date")) and val:
                    # first writer wins — og: beats bare description only
                    # when the bare key isn't set yet
                    meta.setdefault(key, val)
            elif tag == "script" and \
                    (a.get("type") or "").lower() == "application/ld+json":
                self._in_ld = True
                self._ld_buf = []
            elif tag == "title":
                self._is_title = True
                self._title_buf = []

        def handle_endtag(self, tag):
            if tag == "script" and self._in_ld:
                self._in_ld = False
                raw = "".join(self._ld_buf)
                try:
                    data = _json.loads(raw)
                    if isinstance(data, dict):
                        ld_objects.append(data)
                    elif isinstance(data, list):
                        ld_objects.extend(x for x in data
                                          if isinstance(x, dict))
                except ValueError:
                    pass          # malformed LD is common — skip, don't die
            elif tag == "title":
                self._is_title = False
                title_txt = "".join(self._title_buf).strip()
                if title_txt:
                    meta.setdefault("title", title_txt)

        def handle_data(self, data):
            if self._in_ld:
                self._ld_buf.append(data)
            elif self._is_title:
                self._title_buf.append(data)

    try:
        p = _HeadParser()
        p.feed(html)
    except Exception:
        pass

    # Flatten JSON-LD: prefer schema.org Article/Product/Event types.
    ld_best: dict = {}
    for obj in ld_objects:
        t = obj.get("@type") or obj.get("type") or ""
        if isinstance(t, list):
            t = " ".join(str(x) for x in t)
        t_low = str(t).lower()
        if any(k in t_low for k in ("article", "news", "blog", "product",
                                    "event", "recipe", "howto", "video")):
            ld_best = obj
            break
    if not ld_best and ld_objects:
        ld_best = ld_objects[0]

    def _ld_get(*keys):
        for k in keys:
            v = ld_best.get(k)
            if v:
                if isinstance(v, list):
                    v = v[0] if v else ""
                if isinstance(v, dict):
                    v = v.get("name") or v.get("@id") or ""
                return str(v).strip()
        return ""

    merged = {
        "title": meta.get("og:title") or _ld_get("headline", "name")
                 or meta.get("title", ""),
        "description": meta.get("og:description")
                       or meta.get("twitter:description")
                       or _ld_get("description")
                       or meta.get("description", ""),
        "author": _ld_get("author", "creator") or meta.get("author", ""),
        "site": meta.get("og:site_name", ""),
        "type": meta.get("og:type") or _ld_get("@type"),
        "published": meta.get("article:published_time")
                     or meta.get("date") or _ld_get("datePublished"),
        "image": meta.get("og:image", ""),
        "canonical": meta.get("og:url", ""),
    }

    # Optional trafilatura pass — the roadmap's "structured extraction"
    # lib; only used for the BODY, metadata above stays stdlib.
    body = ""
    engine = "stdlib json-ld/og"
    try:
        import trafilatura
        got = trafilatura.extract(html, include_comments=False,
                                  include_tables=True,
                                  favor_precision=True)
        if got and len(got.strip()) > 80:
            body = got.strip()
            engine = "trafilatura"
    except ImportError:
        pass
    except Exception:
        pass
    if not body:
        _, body = clean_html(html, url, max_chars=max_chars)

    lines = [f"# {merged['title'] or '(untitled)'}",
             f"({url}) — extracted via {engine}"]
    facts = [(k, v) for k, v in (("Type", merged["type"]),
                                 ("Author", merged["author"]),
                                 ("Site", merged["site"]),
                                 ("Published", merged["published"]),
                                 ("Description", merged["description"]),
                                 ("Image", merged["image"])) if v]
    if facts:
        lines.append("")
        lines.extend(f"- **{k}**: {v[:300]}" for k, v in facts)
    lines.append("")
    lines.append(body[:max_chars] or "(no readable body found)")
    return "\n".join(lines)


def _top_links(html: str, base_host: str, limit: int = 40) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.startswith(("http://", "https://")):
                continue
            txt = a.get_text(" ", strip=True)[:80]
            if not txt or href in seen:
                continue
            seen.add(href)
            out.append((txt, href))
            if len(out) >= limit:
                break
    except Exception:
        for m in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]{2,80})<', html):
            href, txt = m.group(1), m.group(2).strip()
            if href not in seen and txt:
                seen.add(href)
                out.append((txt, href))
                if len(out) >= limit:
                    break
    return out


TOOL = {
    "name": "scrape",
    "description": (
        "Fetches a web page and returns its clean readable text (chrome, "
        "scripts and menus stripped). Use for: 'read this page', 'what does "
        "this article say', gathering source material for research. Modes: "
        "text (default, article body), links (list of page links), title, "
        "structured (JSON-LD/OpenGraph metadata + main content — products, "
        "articles, events with author/date). "
        "Read-only GET; never submits forms."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {"type": "STRING", "description": "Page URL to fetch."},
            "mode": {"type": "STRING",
                     "description": "text | links | title | structured. Default text."},
            "max_chars": {"type": "STRING",
                          "description": "Truncate text at N chars (500-40000). Default 8000."},
        },
        "required": ["url"],
    },
    "handler": scrape,
}
