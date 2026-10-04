# actions/mcp.py
"""mcp — Model Context Protocol client. Plug into 100+ free servers.

WHAT IT DOES
    Talks JSON-RPC 2.0 over stdio to any MCP server the user configures
    (GitHub, Slack, Notion, Spotify, filesystem, …) — no code per server,
    just a command line in config/mcp.json:

        {"servers": {"fs": {"command": ["npx", "-y",
          "@modelcontextprotocol/server-filesystem", "/home/u/notes"]}}}

    actions:
      list    — configured servers + their tools (cached per process)
      call    — run a server tool with JSON arguments
      add     — save a server entry (command as a string, shlex-split —
                never a shell, so no injection surface)
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
        with self._write_lock:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()

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


# ── tool cache ───────────────────────────────────────────────────────────────

_TOOLS: dict[str, tuple[float, list[dict]]] = {}
_CACHE_TTL = 300.0


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


def _list_tools(name: str, force: bool = False) -> list[dict]:
    cached = _TOOLS.get(name)
    if cached and not force and (time.monotonic() - cached[0]) < _CACHE_TTL:
        return cached[1]
    with _session(_server_argv(name)) as c:
        result = c.request("tools/list")
    tools = result.get("tools") or []
    _TOOLS[name] = (time.monotonic(), tools)
    return tools


# ── handler ──────────────────────────────────────────────────────────────────

def mcp(parameters: dict = None, player=None, session_memory=None) -> str:
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

    if action == "add":
        name = str(params.get("name") or "").strip()
        command = str(params.get("command") or "").strip()
        if not name or not command:
            return "Need both name and command (mcp action=add name=… command=…)."
        if not name.replace("_", "").replace("-", "").isalnum():
            return "Server name must be letters/digits/underscore/dash."
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"Couldn't parse the command: {e}"
        if not argv:
            return "Empty command."
        cfg.setdefault("servers", {})[name] = {"command": argv}
        _save_cfg(cfg)
        _TOOLS.pop(name, None)
        # verify it actually speaks MCP before claiming success
        try:
            tools = _list_tools(name)
            return (f"MCP server '{name}' saved and verified — "
                    f"{len(tools)} tool(s) available.")
        except Exception as e:
            return (f"Saved '{name}' but the handshake failed: {e}. "
                    "Check the command runs standalone.")

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
            argv = _server_argv(name)
        except KeyError:
            return f"No MCP server named {name!r}."
        except ValueError as e:
            return str(e)
        try:
            with _session(argv) as c:
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
        "MCP server (filesystem, GitHub, Slack, Notion, Spotify, …). "
        "Actions: list (servers + tools), call (name, tool, arguments as "
        "JSON), add (name + command string), remove (name). Use when the "
        "user mentions an MCP server or wants external app tools without "
        "new code. For Gmail use the gmail plugin; for local files the "
        "file tools may be simpler."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "list | call | add | remove"},
            "name": {"type": "STRING", "description": "Server name"},
            "tool": {"type": "STRING", "description": "Tool name for call"},
            "command": {"type": "STRING",
                        "description": "argv string for add, e.g. 'npx -y server-x'"},
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
        argv = _server_argv(server)
    except KeyError:
        return f"MCP server '{server}' was removed from config."
    except ValueError as e:
        return str(e)
    try:
        with _session(argv) as c:
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
