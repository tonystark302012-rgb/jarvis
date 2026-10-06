# actions/mcp.py
"""mcp — Model Context Protocol client. Plug into 100+ free servers.

WHAT IT DOES
    Talks JSON-RPC 2.0 over the two standard MCP transports (spec
    2025-11-25): stdio for local servers, Streamable HTTP for remote
    ones — no code per server, just config/mcp.json:

        {"servers": {"fs": {"command": ["npx", "-y",
          "@modelcontextprotocol/server-filesystem", "/home/u/notes"]},
          "remote": {"url": "https://host/mcp",
                     "headers": {"Authorization": "Bearer …"}}}}

    actions:
      list    — configured servers + their tools (cached per process)
      call    — run a server tool with JSON arguments
      add     — save a server entry (command as a string, shlex-split —
                never a shell, so no injection surface) — or preset=<name>
                to install straight from the OFFICIAL catalog below
      presets — the official modelcontextprotocol/servers catalog with
                one-line usage (zero-code setup)
      remove  — delete a server entry

SAFETY
    * command is an argv LIST (shlex on add) — runs without a shell.
    * every RPC has a timeout; a wedged server dies, the tool says so.
    * server config lives in config/mcp.json (gitignored via config/*).
    * stderr is drained to a pipe so a chatty server can't block.

FREE: the MCP spec is open, the servers are open source, this client is
stdlib only (subprocess + json + threading).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import threading
import time
from pathlib import Path

_DEFAULT_TIMEOUT = 20.0

# ── Official catalog — modelcontextprotocol/servers (MIT) ───────────────────
# `mcp action=add preset=filesystem path=~/notes` resolves to the server's
# real command. {path} is substituted from the `path` param; `env` names a
# variable the server expects (we note it instead of silently failing).
CATALOG: dict[str, dict] = {
    "filesystem": {
        "cmd": "npx -y @modelcontextprotocol/server-filesystem {path}",
        "path": "~",
        "desc": "Read/write/append files inside one allowlisted folder"},
    "fetch": {
        "cmd": "uvx mcp-server-fetch",
        "desc": "Fetch and read web pages (server-side HTTP client)"},
    "github": {
        "cmd": "npx -y @modelcontextprotocol/server-github",
        "env": "GITHUB_PERSONAL_ACCESS_TOKEN",
        "desc": "Repos, issues, pull requests (needs a token)"},
    "memory": {
        "cmd": "npx -y @modelcontextprotocol/server-memory",
        "desc": "Knowledge-graph memory store (entities + relations)"},
    "sequential-thinking": {
        "cmd": "npx -y @modelcontextprotocol/server-sequential-thinking",
        "desc": "Structured multi-step thinking tool"},
    "git": {
        "cmd": "uvx mcp-server-git",
        "path": ".",
        "desc": "Status, log, diff, commit in a local repo"},
    "sqlite": {
        "cmd": "uvx mcp-server-sqlite --db-path {path}",
        "path": "./mcp-data.db",
        "desc": "SQL queries over a local sqlite file"},
    "puppeteer": {
        "cmd": "npx -y @modelcontextprotocol/server-puppeteer",
        "desc": "Headless browser automation (screenshots, clicks)"},
    "google-maps": {
        "cmd": "npx -y @modelcontextprotocol/server-google-maps",
        "env": "GOOGLE_MAPS_API_KEY",
        "desc": "Directions, places, geocoding (needs a key)"},
    "time": {
        "cmd": "uvx mcp-server-time",
        "desc": "Timezone clocks and time queries"},
    "everything": {
        "cmd": "npx -y @modelcontextprotocol/server-everything",
        "desc": "Official demo server (tools, resources, prompts)"},
}


def _cfg_path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "mcp.json"


def _load_cfg() -> dict:
    try:
        data = json.loads(_cfg_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("servers"), dict):
            return data
    except Exception:
        pass
    return {"servers": {}}


def _save_cfg(cfg: dict) -> None:
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                 encoding="utf-8")


# ── JSON-RPC over stdio ──────────────────────────────────────────────────────

class _StdioClient:
    """One-shot-ish MCP session: spawn, initialize, then any number of
    request/response rounds. Callers use `close()` (or the context manager)."""

    def __init__(self, argv: list[str], env: dict | None = None,
                 timeout: float = _DEFAULT_TIMEOUT):
        import os
        self._timeout = timeout
        self._id = 0
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
            env={**os.environ, **(env or {})},
        )
        self._write_lock = threading.Lock()

    # -- plumbing -------------------------------------------------------
    def _send(self, payload: dict) -> None:
        stdin = self._proc.stdin
        if stdin is None:                    # we always pass stdin=PIPE
            raise RuntimeError("MCP server has no stdin")
        with self._write_lock:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None) -> dict:
        """Send a request, block until its id answers. Raises on timeout
        or a dead server — never hangs past `timeout`."""
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}})
        deadline = time.monotonic() + (timeout or self._timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP {method} timed out")
            line = _read_line(self._proc.stdout, remaining)
            if line is None:
                raise ConnectionError("MCP server closed its pipe")
            try:
                msg = json.loads(line)
            except ValueError:
                continue                       # non-JSON noise — ignore
            if msg.get("id") == rid:
                if "error" in msg:
                    err = msg["error"] or {}
                    raise RuntimeError(
                        f"MCP error {err.get('code')}: "
                        f"{err.get('message', 'unknown')}")
                return msg.get("result") or {}
            # notifications / other ids — keep reading

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": {}})

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _read_line(stream, timeout: float) -> str | None:
    """Blocking readline with a deadline — reads aren't natively
    interruptible, so the proc is watched: if it dies, readline returns
    '' immediately and we surface None."""
    result: list = [None]

    def _reader():
        try:
            result[0] = stream.readline()
        except Exception:
            result[0] = None

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("MCP read timed out")
    line = result[0]
    if not line:
        return None
    return line.rstrip("\n")


def _session(argv: list[str], timeout: float = _DEFAULT_TIMEOUT) -> _StdioClient:
    """Spawn + MCP handshake (initialize → initialized)."""
    c = _StdioClient(argv, timeout=timeout)
    try:
        c.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "jarvis", "version": "1.0"},
        })
        c.notify("notifications/initialized")
    except Exception:
        c.close()
        raise
    return c


# ── JSON-RPC over Streamable HTTP (spec 2025-11-25) ─────────────────────────
# POST one JSON-RPC message per request; the server answers with either a
# single JSON body or an SSE stream (both mandated by the spec), and hands
# us a `Mcp-Session-Id` we must echo on every follow-up. Stale sessions
# (404) self-heal by re-initializing once; close() DELETEs the session.

_HTTP_PROTOCOL = "2025-06-18"
_ACCEPT = "application/json, text/event-stream"


class _HttpClient:
    """Same interface as _StdioClient (request/notify/close/context mgr)
    so every caller works over either transport."""

    def __init__(self, url: str, headers: dict | None = None,
                 timeout: float = _DEFAULT_TIMEOUT):
        self._url = str(url)
        self._headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._timeout = timeout
        self._id = 0
        self._session_id: str | None = None
        self._ready = False

    # -- plumbing -------------------------------------------------------
    def _post(self, payload: dict, timeout: float | None = None):
        """POST one message → (status, content_type, raw_bytes, headers).
        Never raises for 4xx/5xx — the status is part of the protocol."""
        import urllib.error
        import urllib.request
        hdrs = {"Content-Type": "application/json",
                "Accept": _ACCEPT, **self._headers}
        if self._session_id:
            hdrs["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self._url, data=json.dumps(payload).encode("utf-8"),
            headers=hdrs, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout or self._timeout)
        except urllib.error.HTTPError as e:         # 4xx/5xx with a body
            raw = b""
            try:
                raw = e.read()
            except Exception:
                pass
            return (e.code, (e.headers.get("Content-Type") or ""),
                    raw, e.headers)
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._session_id = sid
            return (getattr(resp, "status", 200) or 200,
                    resp.headers.get("Content-Type") or "",
                    resp.read(), resp.headers)

    @staticmethod
    def _messages(ctype: str, raw: bytes) -> list[dict]:
        """JSON body → [msg]; SSE body → every `data:` event parsed.
        Non-protocol garbage raises with a snippet (honest, never silent)."""
        if not raw:
            return []
        if "text/event-stream" in ctype:
            msgs: list[dict] = []
            buf: list[str] = []
            for line in raw.decode("utf-8", "replace").splitlines():
                if line.startswith("data:"):
                    buf.append(line[5:].lstrip())
                    continue
                if not line and buf:               # blank line = event end
                    try:
                        msgs.append(json.loads("\n".join(buf)))
                    except ValueError:
                        pass
                    buf = []
            if buf:                                # stream cut mid-event
                try:
                    msgs.append(json.loads("\n".join(buf)))
                except ValueError:
                    pass
            return msgs
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError(
                f"MCP HTTP: response is not JSON/SSE: {raw[:120]!r}")
        return [obj] if isinstance(obj, dict) else []

    @staticmethod
    def _raise_for(msg: dict) -> None:
        if "error" in msg:
            err = msg["error"] or {}
            raise RuntimeError(
                f"MCP error {err.get('code')}: "
                f"{err.get('message', 'unknown')}")

    # -- protocol -------------------------------------------------------
    def handshake(self) -> None:
        """initialize → notifications/initialized (captures session id)."""
        self._id += 1
        status, ctype, raw, _h = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "initialize",
            "params": {"protocolVersion": _HTTP_PROTOCOL,
                       "capabilities": {},
                       "clientInfo": {"name": "jarvis", "version": "1.0"}},
        })
        if status == 404:
            raise ConnectionError("MCP HTTP endpoint not found (404)")
        if status >= 400:
            raise ConnectionError(
                f"MCP HTTP initialize failed: {status} {raw[:160]!r}")
        msgs = self._messages(ctype, raw)
        reply = next((m for m in msgs if m.get("id") == self._id), None)
        if reply is None:
            raise ConnectionError(
                f"MCP HTTP initialize got no reply ({status}, {raw[:120]!r})")
        self._raise_for(reply)
        self._ready = True
        self.notify("notifications/initialized")

    def request(self, method: str, params: dict | None = None,
                timeout: float | None = None) -> dict:
        """One request → its result. A 404 session (server restarted) is
        recovered by one transparent re-handshake, then retried once."""
        if not self._ready:
            self.handshake()
        self._id += 1
        rid = self._id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params or {}}
        status, ctype, raw, _h = self._post(payload, timeout)
        if status == 404 and self._session_id:
            # server lost our session → re-initialize and retry once
            self._session_id = None
            self._ready = False
            self.handshake()
            self._id += 1
            rid = self._id
            payload["id"] = rid
            status, ctype, raw, _h = self._post(payload, timeout)
        if status >= 400:
            raise ConnectionError(
                f"MCP HTTP {method} failed: {status} {raw[:160]!r}")
        msgs = self._messages(ctype, raw)
        reply = next((m for m in msgs if m.get("id") == rid), None)
        if reply is None:
            raise ConnectionError(
                f"MCP HTTP {method} got no matching reply "
                f"(status {status}, {raw[:120]!r})")
        self._raise_for(reply)
        return reply.get("result") or {}

    def notify(self, method: str) -> None:
        status, _ctype, raw, _h = self._post(
            {"jsonrpc": "2.0", "method": method, "params": {}})
        if status >= 400 and status != 404:
            raise ConnectionError(
                f"MCP HTTP notify {method} failed: {status} {raw[:120]!r}")

    def close(self) -> None:
        """Spec: terminate the session with HTTP DELETE (best-effort)."""
        if not self._session_id:
            return
        import urllib.error
        import urllib.request
        hdrs = {"Accept": _ACCEPT, "Mcp-Session-Id": self._session_id,
                **self._headers}
        try:
            req = urllib.request.Request(self._url, headers=hdrs,
                                         method="DELETE")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except (urllib.error.URLError, OSError):
            pass
        self._session_id = None
        self._ready = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ── tool cache ───────────────────────────────────────────────────────────────

_TOOLS: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300.0


def _server_entry(name: str) -> dict:
    servers = _load_cfg().get("servers", {})
    entry = servers.get(name)
    if not isinstance(entry, dict):
        raise KeyError(f"no MCP server named {name!r}")
    return entry


def _server_argv(name: str) -> list[str]:
    servers = _load_cfg().get("servers", {})
    entry = servers.get(name)
    if not isinstance(entry, dict):
        raise KeyError(f"no MCP server named {name!r}")
    argv = entry.get("command")
    if not isinstance(argv, list) or not argv or \
            not all(isinstance(a, str) for a in argv):
        raise ValueError(f"server {name!r} has no argv list")
    return argv


def _connect(name: str, timeout: float = _DEFAULT_TIMEOUT):
    """Configured entry → live MCP session on its transport:
    `url` → Streamable HTTP (handshake inside), `command` → stdio."""
    entry = _server_entry(name)
    url = entry.get("url")
    if url:
        scheme = str(url).split("://", 1)[0].lower()
        if scheme not in ("http", "https"):
            raise ValueError(f"server {name!r} url must be http(s): {url!r}")
        c = _HttpClient(url, entry.get("headers") or {}, timeout=timeout)
        try:
            c.handshake()
        except Exception:
            c.close()
            raise
        return c
    argv = entry.get("command")
    if not isinstance(argv, list) or not argv or \
            not all(isinstance(a, str) for a in argv):
        raise ValueError(f"server {name!r} needs an argv list or a url")
    return _session(argv, timeout=timeout)


def _list_tools(name: str, force: bool = False) -> list[dict]:
    cached = _TOOLS.get(name)
    if cached and not force and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]
    with _connect(name) as c:
        result = c.request("tools/list")
    tools = result.get("tools") or []
    _TOOLS[name] = (time.monotonic(), tools)
    return tools


# ── handler ──────────────────────────────────────────────────────────────────

def mcp(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "list").lower().strip()
    cfg = _load_cfg()

    if action == "list":
        servers = cfg.get("servers", {})
        if not servers:
            return ("No MCP servers configured. Add one: "
                    "mcp action=add name=filesystem command="
                    "'npx -y @modelcontextprotocol/server-filesystem /tmp'.")
        out = []
        for name in sorted(servers):
            try:
                tools = _list_tools(name)
                names = ", ".join(t.get("name", "?") for t in tools[:8])
                more = f" (+{len(tools) - 8})" if len(tools) > 8 else ""
                out.append(f"• {name}: {len(tools)} tools — {names}{more}")
            except Exception as e:
                out.append(f"• {name}: unreachable ({str(e)[:80]})")
        return "MCP servers:\n" + "\n".join(out)

    if action in ("presets", "catalog"):
        lines = ["Official MCP catalog (modelcontextprotocol/servers, MIT):"]
        for key in sorted(CATALOG):
            spec = CATALOG[key]
            env = f"  [needs ${spec['env']}]" if spec.get("env") else ""
            lines.append(f"• preset={key} — {spec['desc']}{env}")
        lines.append("Install: mcp action=add preset=<name>"
                     " [path=…] [name=…]  ·  requires node (npx) or "
                     "uv/uvx on PATH.")
        return "\n".join(lines)

    if action == "add":
        preset = str(params.get("preset") or "").strip().lower()
        if preset:
            entry = CATALOG.get(preset)
            if entry is None:
                return (f"No preset {preset!r} — see "
                        f"mcp action=presets ({', '.join(sorted(CATALOG))}).")
            raw = entry["cmd"]
            path_v = str(params.get("path") or entry.get("path") or "")
            if "{path}" in raw:
                if not path_v:
                    return (f"Preset {preset} needs a path: "
                            f"mcp action=add preset={preset} path=/some/dir")
                raw = raw.replace("{path}", path_v)
            if not str(params.get("command") or "").strip():
                params = dict(params, command=raw)
            if not str(params.get("name") or "").strip():
                params = dict(params, name=preset)
        name = str(params.get("name") or "").strip()
        command = str(params.get("command") or "").strip()
        url = str(params.get("url") or "").strip()
        if not name:
            return "Give a server name (mcp action=add name=…)."
        if not name.replace("_", "").replace("-", "").isalnum():
            return "Server name must be letters/digits/underscore/dash."
        if command and url:
            return "Give EITHER command (stdio) or url (Streamable HTTP), not both."
        if not command and not url:
            return ("Need a command or a url — e.g. "
                    "mcp action=add name=fs command='npx -y server-filesystem /tmp' "
                    "OR mcp action=add name=remote url=https://host/mcp.")
        new_entry: dict
        if url:
            scheme = url.split("://", 1)[0].lower()
            if scheme not in ("http", "https"):
                return "url must be http:// or https://."
            new_entry = {"url": url}
            headers = params.get("headers")
            if isinstance(headers, str) and headers.strip():
                try:
                    headers = json.loads(headers)
                except ValueError:
                    return "headers must be a JSON object string."
            if isinstance(headers, dict) and headers:
                new_entry["headers"] = {str(k): str(v)
                                        for k, v in headers.items()}
        else:
            try:
                argv = shlex.split(command)
            except ValueError as e:
                return f"Couldn't parse the command: {e}"
            if not argv:
                return "Empty command."
            new_entry = {"command": argv}
        cfg.setdefault("servers", {})[name] = new_entry
        _save_cfg(cfg)
        _TOOLS.pop(name, None)
        # verify it actually speaks MCP before claiming success
        try:
            tools = _list_tools(name)
            note = ""
            if preset:
                envk = CATALOG.get(preset, {}).get("env")
                if envk:
                    import os as _os
                    have = bool(_os.environ.get(envk))
                    note = (f" NOTE: set ${envk} before calling it"
                            + (" (found in env)." if have else
                               " (not in env yet)."))
            return (f"MCP server '{name}' saved and verified — "
                    f"{len(tools)} tool(s) available.{note}")
        except Exception as e:
            hint = ("Check the url answers MCP POSTs."
                    if url else "Check the command runs standalone.")
            return (f"Saved '{name}' but the handshake failed: {e}. {hint}")

    if action == "remove":
        name = str(params.get("name") or "").strip()
        if name not in cfg.get("servers", {}):
            return f"No MCP server named {name!r}."
        del cfg["servers"][name]
        _save_cfg(cfg)
        _TOOLS.pop(name, None)
        return f"Removed MCP server '{name}'."

    if action == "call":
        name = str(params.get("name") or params.get("server") or "").strip()
        tool = str(params.get("tool") or "").strip()
        if not name or not tool:
            return "Give the server name and tool name."
        args = params.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return "arguments must be a JSON object."
        if not isinstance(args, dict):
            return "arguments must be a JSON object."
        try:
            conn = _connect(name, timeout=float(
                params.get("timeout") or _DEFAULT_TIMEOUT))
        except KeyError:
            return f"No MCP server named {name!r}."
        except ValueError as e:
            return str(e)
        try:
            with conn as c:
                result = c.request("tools/call",
                                   {"name": tool, "arguments": args},
                                   timeout=float(
                                       params.get("timeout") or _DEFAULT_TIMEOUT))
        except Exception as e:
            return f"MCP '{name}.{tool}' failed: {e}"
        return _format_call_result(result, f"{name}.{tool}")

    return f"Unknown mcp action {action!r} — use list|call|add|remove."


TOOL = {
    "name": "mcp",
    "description": (
        "Model Context Protocol client — call tools from any configured "
        "MCP server (filesystem, GitHub, Slack, Notion, Spotify, …) over "
        "either standard transport: stdio (command) or Streamable HTTP "
        "(url). Actions: list (servers + tools), call (name, tool, "
        "arguments as JSON), add (name + command OR name + url [+ "
        "headers] OR preset=filesystem|fetch|github|memory|"
        "sequential-thinking|git|sqlite|puppeteer|google-maps|time|"
        "everything from the official catalog), presets (list that "
        "catalog), remove (name). Use when the "
        "user mentions an MCP server or wants external app tools without "
        "new code. For Gmail use the gmail plugin; for local files the "
        "file tools may be simpler."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "list | call | add | presets | remove"},
            "preset": {"type": "STRING",
                       "description": "Official catalog key for add "
                                      "(filesystem, fetch, github, memory, "
                                      "git, sqlite, puppeteer, time, …)"},
            "path": {"type": "STRING",
                     "description": "Folder/DB path for presets that need one"},
            "name": {"type": "STRING", "description": "Server name"},
            "tool": {"type": "STRING", "description": "Tool name for call"},
            "command": {"type": "STRING",
                        "description": "argv string for add (stdio), e.g. 'npx -y server-x'"},
            "url": {"type": "STRING",
                    "description": "Streamable HTTP endpoint for add, e.g. https://host/mcp"},
            "headers": {"type": "STRING",
                        "description": "JSON object of extra HTTP headers for a url server"},
            "arguments": {"type": "STRING",
                          "description": "JSON object of tool arguments"},
            "timeout": {"type": "NUMBER", "description": "Seconds — default 20"},
        },
        "required": [],
    },
    "handler": mcp,
}


# ── shared result formatting ─────────────────────────────────────────────

def _format_call_result(result: dict, label: str) -> str:
    """result.content = [{type: text|image|…, text: …}] → speakable text."""
    content = (result or {}).get("content") or []
    texts = [b.get("text", "") for b in content
             if isinstance(b, dict) and b.get("type") == "text"]
    body = "\n".join(t for t in texts if t).strip()
    if (result or {}).get("isError"):
        return f"MCP '{label}' returned an error: {body or 'no detail'}"
    if not body:
        return f"MCP '{label}' ok (no text content)."
    return body if len(body) <= 4000 else body[:4000] + "\n…(truncated)"


# ── NATIVE flatten: every MCP tool as a first-class session tool ─────────
# The model calls mcp__<server>__<tool> directly (one hop instead of the
# two-hop `mcp action=call` JSON dance). Declarations are built at session
# config time (main._build_config) and refreshed on a TTL; dispatch happens
# in main._execute_tool's `mcp__` branch. Servers that are down are simply
# absent from the tool list — `mcp list` still shows them as unreachable.

_NATIVE_TTL = 300.0
_NATIVE_INDEX: dict[str, tuple[str, str]] = {}   # full name → (server, tool)
_NATIVE_DECLS: list[dict] = []
_NATIVE_BUILT_AT = 0.0


def _sanitize(name: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name))[:48]
    if not s:
        s = "tool"
    if s[0].isdigit():
        s = "t_" + s
    return s


def native_declarations(force: bool = False) -> list[dict]:
    """Flatten configured servers' tools into function declarations.
    Unreachable servers are skipped (never break session start)."""
    global _NATIVE_INDEX, _NATIVE_DECLS, _NATIVE_BUILT_AT
    now = time.monotonic()
    if (not force and _NATIVE_DECLS and
            now - _NATIVE_BUILT_AT < _NATIVE_TTL):
        return list(_NATIVE_DECLS)
    decls: list[dict] = []
    index: dict[str, tuple[str, str]] = {}
    cfg = _load_cfg()
    for server in sorted(cfg.get("servers", {})):
        try:
            tools = _list_tools(server, force=force)
        except Exception:
            continue                              # down → absent, honestly
        for t in tools if isinstance(tools, list) else []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            base = f"mcp__{_sanitize(server)}__{_sanitize(t['name'])}"
            name, n = base, 2
            while name in index:
                name, n = f"{base}_{n}", n + 1
            index[name] = (server, str(t["name"]))
            params = t.get("inputSchema") or {
                "type": "object", "properties": {}}
            desc = str(t.get("description") or
                       f"MCP tool '{t['name']}' on server '{server}'")
            decls.append({"name": name, "description": desc[:1024],
                          "parameters": params})
    _NATIVE_INDEX, _NATIVE_DECLS = index, decls
    _NATIVE_BUILT_AT = now
    return list(decls)


def call_native(full_name: str, args: dict | None = None,
                timeout: float | None = None) -> str:
    """Dispatch mcp__srv__tool → tools/call. Re-spawns the server per call
    (sessions are short-lived by design), so a crash since declaration time
    self-heals on the next call."""
    args = dict(args or {})
    pair = _NATIVE_INDEX.get(full_name)
    if pair is None:
        native_declarations(force=True)           # stale index → rebuild once
        pair = _NATIVE_INDEX.get(full_name)
    if pair is None:
        return (f"Unknown MCP tool '{full_name}'. Say 'mcp list' to see "
                f"configured servers and their tools.")
    server, tool = pair
    try:
        conn = _connect(server)
    except KeyError:
        return f"MCP server '{server}' was removed from config."
    except ValueError as e:
        return str(e)
    try:
        with conn as c:
            result = c.request(
                "tools/call", {"name": tool, "arguments": args},
                timeout=float(timeout or _DEFAULT_TIMEOUT))
    except Exception as e:
        return f"MCP '{server}.{tool}' failed: {e}"
    return _format_call_result(result, f"{server}.{tool}")


def run(params: dict, ctx: dict | None = None) -> str:
    return mcp(params,
               player=(ctx or {}).get("player"),
               session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(mcp({"action": "list"}))
