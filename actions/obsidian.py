"""
obsidian — full vault CRUD over the Local REST API (Report #1 Tier-A #2).

Talks to the official `obsidian-local-rest-api` plugin (MIT fork with the
built-in MCP server) running INSIDE Obsidian:

    status / list / read / write / append / search / open / tags

Connection settings come from config/api_keys.json (`obsidian_api_key`)
or the OBSIDIAN_API_KEY / OBSIDIAN_URL env vars; base URL defaults to
https://127.0.0.1:27124 (the plugin's HTTPS port). TLS is verified
normally — the plugin's self-signed cert is accepted ONLY for loopback
hosts, which is where it lives.

REUSE FIRST: the same plugin exposes an MCP endpoint — if you prefer the
protocol route, this connects with the EXISTING mcp client instead:

    mcp action=add name=obsidian url=https://127.0.0.1:27124/mcp/ \
        headers='{"Authorization": "Bearer <key>"}'

WHY NOT A DUPLICATE: spaces/pages are JARVIS's own store; this is the
bridge into the user's EXISTING Obsidian vault (their notes, their
plugins, their phone sync).
"""
from __future__ import annotations

import json
from urllib.parse import quote

_TIMEOUT = 8.0
_OFFLINE_HINT = (
    "Obsidian's Local REST API is not reachable. Install/enable the "
    "'Local REST API' community plugin in Obsidian (Settings → Community "
    "plugins), copy its API key into config/api_keys.json as "
    "`obsidian_api_key` (or OBSIDIAN_API_KEY env), and keep Obsidian "
    "running. Then: obsidian action=status.")


def _settings() -> tuple[str, str]:
    """(base_url, api_key) — config file first, environment second."""
    url, key = "https://127.0.0.1:27124", ""
    try:
        from config import get_base_dir
        data = json.loads(
            (get_base_dir() / "config" / "api_keys.json")
            .read_text(encoding="utf-8"))
        url = str(data.get("obsidian_url") or url)
        key = str(data.get("obsidian_api_key") or key)
    except Exception:
        pass
    import os
    url = os.environ.get("OBSIDIAN_URL", url) or url
    key = os.environ.get("OBSIDIAN_API_KEY", key) or key
    return url.rstrip("/"), key


def _verify(base: str) -> bool:
    """The plugin serves a self-signed cert on loopback — accept it there
    ONLY; any remote host keeps full verification."""
    host = base.split("://", 1)[-1].split("/", 1)[0].split(":")[0]
    return host not in ("127.0.0.1", "localhost", "::1")


def _req(method: str, path: str, *, body: str | None = None,
         content_type: str = "text/markdown", params: dict | None = None):
    """One HTTP call. Returns (status, text) or raises ConnectionError-ish
    mapped to None so callers give the honest offline hint."""
    import requests
    base, key = _settings()
    headers = {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if body is not None:
        headers["Content-Type"] = content_type
    try:
        r = requests.request(method, f"{base}{path}", data=body,
                             headers=headers, timeout=_TIMEOUT,
                             params=params, verify=_verify(base))
    except Exception:
        return None, None
    return r.status_code, (r.text or "")


def _offline(code, text) -> str:
    if code is None:
        return _OFFLINE_HINT
    if code == 401:
        return ("Obsidian rejected the API key (401) — set "
                "`obsidian_api_key` in config/api_keys.json to the key "
                "shown in the plugin settings.")
    if code == 404:
        return "Not found in the vault (404)."
    return f"Obsidian API error {code}: {str(text)[:200]}"


def obsidian(parameters: dict = None, player=None,
             session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()
    path = str(params.get("path", "") or "").strip().lstrip("/")
    enc = quote(path, safe="/")

    if action == "status":
        code, text = _req("GET", "/")
        if code is None:
            return _OFFLINE_HINT
        if code in (200, 204):
            base, key = _settings()
            return (f"Obsidian REST API is up at {base}"
                    + (" (key set)." if key else
                       " — but no API key configured "
                       "(config/api_keys.json `obsidian_api_key`)."))
        return _offline(code, text)

    if not path and action not in ("list", "search", "tags"):
        return f"Give a `path` for action={action} (e.g. notes/daily.md)."

    if action == "list":
        code, text = _req("GET", f"/vault/{enc}")
        if code is None:
            return _OFFLINE_HINT
        if code != 200:
            return _offline(code, text)
        try:
            entries = json.loads(text)
        except ValueError:
            return f"Unexpected list payload: {text[:200]}"
        if isinstance(entries, dict):
            entries = entries.get("files") or entries.get("entries") or []
        lines = []
        for e in entries[:80]:
            if isinstance(e, dict):
                lines.append(f"• {e.get('path', e)}")
            else:
                lines.append(f"• {e}")
        return (f"Vault root ({len(lines)} entries):\n" + "\n".join(lines)
                if lines else "Vault listing is empty.")

    if action == "read":
        code, text = _req("GET", f"/vault/{enc}")
        if code is None:
            return _OFFLINE_HINT
        if code != 200:
            return _offline(code, text)
        try:
            data = json.loads(text)
            body = data.get("content") if isinstance(data, dict) else None
            if body is None:
                body = text
        except ValueError:
            body = text
        return str(body)[:20000]

    if action in ("write", "put"):
        content = str(params.get("content", "") or "")
        if not content:
            return "Give `content` to write."
        code, text = _req("PUT", f"/vault/{enc}", body=content)
        if code is None:
            return _OFFLINE_HINT
        if code not in (200, 204):
            return _offline(code, text)
        return f"Wrote {len(content)} chars to vault:{path}."

    if action == "append":
        content = str(params.get("content", "") or "")
        if not content:
            return "Give `content` to append."
        code, text = _req("POST", f"/vault/{enc}", body=content)
        if code is None:
            return _OFFLINE_HINT
        if code in (200, 201, 204):
            return f"Appended {len(content)} chars to vault:{path}."
        if code in (404, 405):
            # some plugin versions only support append at section targets
            # — honest read-modify-write fallback for plain files
            c2, cur = _req("GET", f"/vault/{enc}")
            if c2 == 200:
                try:
                    body = json.loads(cur).get("content", cur)
                except (ValueError, AttributeError):
                    body = cur
                merged = (str(body).rstrip("\n") + "\n" + content)
                c3, t3 = _req("PUT", f"/vault/{enc}", body=merged)
                if c3 in (200, 204):
                    return (f"Appended {len(content)} chars to "
                            f"vault:{path} (read-modify-write).")
                return _offline(c3, t3)
            return _offline(code, text)
        return _offline(code, text)

    if action == "search":
        query = str(params.get("query", "") or params.get("q", "") or "")
        if not query:
            return "Give `query` to search the vault."
        code, text = _req("POST", "/search/simple/", params={"query": query})
        if code is None:
            return _OFFLINE_HINT
        if code != 200:
            return _offline(code, text)
        try:
            data = json.loads(text)
        except ValueError:
            return str(text)[:4000]
        matches = (data.get("matches") if isinstance(data, dict)
                   else data) or []
        if not matches:
            return f"No vault notes match {query!r}."
        lines = []
        for m in matches[:15]:
            if isinstance(m, dict):
                fn = m.get("filename") or m.get("path") or "?"
                ctx = m.get("context") or m.get("snippet") or ""
                lines.append(f"• {fn}: {str(ctx)[:120]}")
            else:
                lines.append(f"• {m}")
        return f"Search {query!r} — {len(matches)} match(es):\n" + \
            "\n".join(lines)

    if action == "open":
        code, text = _req("POST", f"/open/{enc}")
        if code is None:
            return _OFFLINE_HINT
        if code not in (200, 204):
            return _offline(code, text)
        return f"Opened vault:{path} in Obsidian."

    if action == "tags":
        code, text = _req("GET", "/tags/")
        if code is None:
            return _OFFLINE_HINT
        if code != 200:
            return _offline(code, text)
        try:
            data = json.loads(text)
        except ValueError:
            return str(text)[:2000]
        tags = data.get("tags", data) if isinstance(data, dict) else data
        if isinstance(tags, list) and tags and isinstance(tags[0], dict):
            lines = [f"• {t.get('tag', t)} ({t.get('usage', '?')})"
                     for t in tags[:60]]
        else:
            lines = [f"• {t}" for t in list(tags)[:60]]
        return (f"Vault tags ({len(lines)}):\n" + "\n".join(lines)
                if lines else "No tags in the vault.")

    return (f"Unknown obsidian action {action!r} — status | list | read "
            "| write | append | search | open | tags.")


TOOL = {
    "name": "obsidian",
    "description": (
        "Full CRUD over the user's existing Obsidian vault via the "
        "official Local REST API plugin (HTTPS, bearer key). Actions: "
        "status (is it up / key configured), list, read, write (content), "
        "append (content), search (query), open (show in Obsidian), tags. "
        "Settings: config/api_keys.json obsidian_api_key + optional "
        "obsidian_url (default https://127.0.0.1:27124), or "
        "OBSIDIAN_API_KEY/OBSIDIAN_URL env. Use when the user mentions "
        "'my vault', 'my Obsidian notes', 'append to daily note'. MCP "
        "route also works: mcp action=add … url=…/mcp/."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status | list | read | write | "
                                      "append | search | open | tags."},
            "path": {"type": "STRING",
                     "description": "Vault-relative path, e.g. notes/a.md"},
            "content": {"type": "STRING",
                        "description": "Markdown body for write/append."},
            "query": {"type": "STRING",
                      "description": "Search terms for search."},
        },
        "required": [],
    },
    "handler": obsidian,
}
