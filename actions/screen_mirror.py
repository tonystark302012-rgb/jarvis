# actions/screen_mirror.py
"""screen_mirror — voice/model control for the screen mirror that already
lives in the remote dashboard.

DUPLICATE-CHECK: the mirror itself is dashboard/server.py's PC→phone
stream (low-res JPEG frames over the authenticated websocket, ~3 fps,
`mirror_set`). It existed first and stays the ONLY implementation —
this module adds no capture code at all. What was missing is reach:
the model/voice could not toggle it (only the dashboard button could).
So this is a thin adapter:

    start | stop  → DashboardServer.mirror_set_sync (thread-safe bridge
                    onto the dashboard's event loop)
    status        → public mirror_on + the dashboard URL

If the dashboard isn't loaded (deps missing / disabled), the tool says
so honestly and points at the install line.
"""
from __future__ import annotations


def _server():
    """The live DashboardServer, or None (disabled/uninstalled)."""
    try:
        from dashboard import server as dsrv
        return getattr(dsrv, "ACTIVE", None)
    except Exception:
        return None


def screen_mirror(parameters: dict = None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").lower().strip()
    srv = _server()
    if srv is None:
        return ("The screen mirror lives in the remote dashboard, which "
                "isn't loaded — install it once: "
                'pip install fastapi "uvicorn[standard]" cryptography '
                "— then say 'start screen mirror'.")

    if action in ("start", "on"):
        try:
            srv.mirror_set_sync(True)
        except RuntimeError as e:
            return f"Screen mirror couldn't start: {e}"
        out = ("Screen mirror ON — open the dashboard 🖥️ view on your "
               "phone to watch the PC screen live.")
    elif action in ("stop", "off"):
        try:
            srv.mirror_set_sync(False)
        except RuntimeError as e:
            return f"Screen mirror couldn't stop: {e}"
        out = "Screen mirror OFF."
    elif action == "status":
        if srv.mirror_on:
            try:
                url = srv.get_url()
            except Exception:
                url = ""
            tail = f" Open {url}" if url else ""
            out = f"Screen mirror is ON.{tail}"
        else:
            out = "Screen mirror is OFF."
    else:
        return "action must be start | stop | status."

    if player is not None and action in ("start", "on"):
        try:
            player.show_content("SCREEN MIRROR", out[:500])
        except Exception:
            pass
    return out


TOOL = {
    "name": "screen_mirror",
    "description": (
        "Live view of this computer's screen on the phone dashboard — "
        "toggles the dashboard's existing low-res screen stream (nothing "
        "new is captured; frames flow over the authenticated dashboard "
        "websocket only). Use for 'mirror my screen', 'show my screen on "
        "my phone', 'stop the screen share'. actions: start | stop | "
        "status. Requires the remote dashboard to be running."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status (default) | start | stop"},
        },
        "required": [],
    },
    "handler": screen_mirror,
}
