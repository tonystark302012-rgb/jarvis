"""Minimal MCP stdio server for tests — speaks real JSON-RPC 2.0 so
`actions/mcp.py` is exercised end-to-end (spawn → initialize →
tools/list → tools/call) without any network. Not a test module
(no test_ prefix) — tests spawn it as [sys.executable, this file].
"""
import json
import sys

TOOLS = [
    {"name": "echo-tool",
     "description": "Echo the text back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "echo.tool",
     "description": "Same sanitised name as echo-tool on purpose",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}}}},
    {"name": "add",
     "description": "Add two numbers",
     "inputSchema": {"type": "object",
                     "properties": {"a": {"type": "number"},
                                    "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:          # notification (notifications/initialized)
            continue
        method = msg.get("method", "")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": "1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params") or {}
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo-tool":
                result = {"content": [
                    {"type": "text", "text": f"ECHO:{args.get('text', '')}"}]}
            elif name == "echo.tool":
                result = {"content": [
                    {"type": "text", "text": f"DOT:{args.get('text', '')}"}]}
            elif name == "add":
                try:
                    total = float(args.get("a", 0)) + float(args.get("b", 0))
                except (TypeError, ValueError):
                    result = {"content": [{"type": "text",
                                           "text": "bad numbers"}],
                              "isError": True}
                else:
                    total = int(total) if total == int(total) else total
                    result = {"content": [
                        {"type": "text", "text": f"SUM:{total}"}]}
            else:
                result = {"content": [{"type": "text", "text": "no such tool"}],
                          "isError": True}
        else:
            sys.stdout.write(json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"],
                 "error": {"code": -32601, "message": method}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps(
            {"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
