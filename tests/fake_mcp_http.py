# tests/fake_mcp_http.py
"""A REAL Streamable-HTTP MCP server for tests — in-process, loopback only.

Speaks spec-shaped JSON-RPC 2025-11-25: POST /mcp, `Mcp-Session-Id`
issuance on initialize, SSE or plain-JSON answers, 404 on a rotated
(stale) session, 202 for notifications, DELETE to terminate. No mocks
inside the protocol — the client's urllib traffic hits this socket.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):            # keep test output clean
        pass

    # -- helpers --------------------------------------------------------
    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            obj = json.loads(raw or b"{}")
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _send(self, status: int, payload: dict | None = None,
              content_type: str = "application/json",
              extra: dict | None = None, raw: bytes | None = None) -> None:
        data = raw if raw is not None else json.dumps(payload or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _sse(self, payload: dict) -> None:
        data = json.dumps(payload)
        blob = f"event: message\ndata: {data}\n\n".encode()
        self._send(200, content_type="text/event-stream", raw=blob)

    # -- verbs ----------------------------------------------------------
    def do_POST(self) -> None:               # noqa: N802 (http.server API)
        srv = self.server
        msg = self._body()
        method = str(msg.get("method") or "")
        srv.requests.append((method, dict(self.headers)))

        if method == "initialize":
            srv.init_count += 1
            if srv.init_count > 1:
                # each handshake issues a fresh session id
                srv.prev_session = srv.session_id
            srv.session_id = f"sess-{srv.init_count}"
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {"protocolVersion": "2025-06-18",
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "fake-http",
                                          "version": "1.0"}},
            }, extra={"Mcp-Session-Id": srv.session_id})
            return

        # every other message must carry the CURRENT session id
        sid = self.headers.get("Mcp-Session-Id")
        if sid != srv.session_id:
            self._send(404, {"error": {"code": -32001,
                                       "message": "session not found"}})
            return

        if method == "notifications/initialized":
            self._send(202, raw=b"")
            return

        if method == "tools/list":
            if srv.garbage:
                self._send(200, content_type="application/json",
                           raw=b"<html>not mcp at all</html>")
                return
            payload = {"jsonrpc": "2.0", "id": msg.get("id"),
                       "result": {"tools": [{
                           "name": "echo-tool",
                           "description": "Echoes text back",
                           "inputSchema": {"type": "object",
                                           "properties": {"text": {
                                               "type": "string"}},
                                           "required": ["text"]},
                       }]}}
            if srv.sse_mode:
                self._sse(payload)
            else:
                self._send(200, payload)
            return

        if method == "tools/call":
            if srv.fail_next:
                srv.fail_next = False
                self._send(500, {"error": {"code": -32000,
                                           "message": "server on fire"}})
                return
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            self._send(200, {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "result": {"content": [{"type": "text",
                                        "text": f"echo:{args.get('text', '')}"}]},
            })
            return

        self._send(200, {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32601,
                                   "message": f"unknown {method}"}})

    def do_DELETE(self) -> None:              # noqa: N802
        srv = self.server
        srv.deletes.append(self.headers.get("Mcp-Session-Id"))
        self._send(200, raw=b"")


class FakeMCPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.init_count = 0
        self.session_id: str | None = None
        self.prev_session: str | None = None
        self.requests: list = []
        self.deletes: list = []
        self.sse_mode = False
        self.garbage = False
        self.fail_next = False

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/mcp"


def make_server() -> FakeMCPServer:
    srv = FakeMCPServer()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
