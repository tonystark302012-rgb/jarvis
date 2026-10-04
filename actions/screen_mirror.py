# actions/screen_mirror.py
"""screen_mirror — live MJPEG mirror of this machine's display, for free.

WHAT IT DOES
    Captures the primary screen (mss — already a core dep), JPEG-encodes
    it (Pillow — core dep) and streams it from a stdlib HTTP server
    bound to 127.0.0.1 ONLY. Open the URL in any browser (or the in-app
    HUD) to watch the display live — useful while the gui_agent works,
    for demos, or as a poor-man's second screen.

        screen_mirror action=start [port=8699] [fps=4]
        screen_mirror action=status
        screen_mirror action=stop

    GET /        tiny status page
    GET /stream  multipart/x-mixed-replace MJPEG (works in every browser)

SAFETY / FREE
    * binds 127.0.0.1 — never reachable from the network.
    * local-only: nothing is uploaded, so it is NOT a cloud tool
      (privacy mode does not apply — no data leaves the machine).
    * no new dependencies: mss + Pillow + http.server, all present.
"""
from __future__ import annotations

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_LOCK = threading.Lock()
_STATE: dict = {
    "server": None, "thread": None, "producer": None, "running": False,
    "port": None, "frames": 0, "served": 0, "started": 0.0,
    "fps": 4, "error": "",
}
_BOUNDARY = b"--framejarvis"


# ── capture (test seam) ─────────────────────────────────────────────────────

def _grab() -> bytes:
    """Primary monitor → JPEG bytes. Raises honestly when mss/Pillow or a
    display is missing (headless box, missing dep)."""
    import mss
    import mss.tools
    import PIL.Image
    with mss.mss() as sct:
        mon = sct.monitors[1]                 # primary display
        shot = sct.grab(mon)
        img = PIL.Image.frombytes("RGB", shot.size,
                                  shot.bgra, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    return buf.getvalue()


def _producer(fps: float) -> None:
    """Grab loop — runs beside the HTTP server; records the first error
    and stops the mirror instead of pretending to stream."""
    period = 1.0 / max(1.0, float(fps))
    while True:
        with _LOCK:
            if not _STATE["running"]:
                return
        try:
            frame = _grab()
        except Exception as e:
            with _LOCK:
                _STATE["error"] = str(e)
                _STATE["running"] = False
            server = _STATE.get("server")
            if server is not None:
                threading.Thread(target=server.shutdown, daemon=True).start()
            return
        with _LOCK:
            if not _STATE["running"]:
                return
            _STATE["frame"] = frame
            _STATE["frames"] += 1
        time.sleep(period)


# ── HTTP side ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"             # no chunked encoding needed
    server_version = "JarvisMirror/1.0"

    def log_message(self, *args):             # silent
        pass

    def do_GET(self):                          # noqa: N802
        if self.path.split("?", 1)[0] in ("/stream", "/mjpeg"):
            return self._stream()
        body = (
            "<!doctype html><title>JARVIS screen mirror</title>"
            "<h1>JARVIS screen mirror</h1>"
            "<p>running — <a href='/stream'>/stream</a> (MJPEG)</p>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=framejarvis")
        self.end_headers()
        srv = self.server
        try:
            while getattr(srv, "running", False):
                with _LOCK:
                    frame = _STATE.get("frame")
                    _STATE["served"] = _STATE.get("served", 0) + 1
                if frame:
                    self.wfile.write(
                        _BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                        + f"Content-Length: {len(frame)}\r\n\r\n".encode()
                        + frame + b"\r\n")
                    self.wfile.flush()
                time.sleep(1.0 / max(1.0, float(_STATE.get("fps", 4))))
                # cheap pacing: producer already caps fps; this only keeps
                # a slow client from spinning
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                                # client navigated away


class _MirrorServer(ThreadingHTTPServer):
    daemon_threads = True
    running = True


# ── lifecycle ────────────────────────────────────────────────────────────────

def _start(port: int, fps: int) -> str:
    try:
        srv = _MirrorServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        return (f"Port {port} is not free ({e}) — pick another: "
                f"screen_mirror action=start port={port + 1}")
    # verify a frame can actually be produced BEFORE claiming success
    try:
        first = _grab()
    except Exception as e:
        srv.server_close()
        dep = ("mss + Pillow + a display" if isinstance(e, ImportError)
               else "a capture source")
        return (f"Screen capture unavailable: {e} — install {dep} "
                f"(pip install mss pillow) and retry.")
    with _LOCK:
        _STATE.update({
            "server": srv, "running": True, "port": srv.server_address[1],
            "frames": 1, "served": 0, "started": time.time(),
            "fps": fps, "error": "", "frame": first,
        })
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _STATE["thread"] = t
    p = threading.Thread(target=_producer, args=(fps,), daemon=True)
    p.start()
    _STATE["producer"] = p
    url = f"http://127.0.0.1:{srv.server_address[1]}/stream"
    return (f"Screen mirror LIVE at {url} (loopback only, {fps} fps). "
            f"Open it in any browser; action=status to check, "
            f"action=stop to end.")


def _stop() -> str:
    with _LOCK:
        srv = _STATE.get("server")
        was = bool(_STATE.get("running")) or srv is not None
        frames = _STATE.get("frames", 0)
        _STATE["running"] = False
        _STATE["server"] = None
        _STATE["port"] = None
    if not was:
        return "Screen mirror is not running."
    if srv is not None:
        try:
            srv.running = False
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
    return f"Screen mirror stopped after {frames} frame(s)."


def _status() -> str:
    with _LOCK:
        if not _STATE.get("running"):
            err = _STATE.get("error") or ""
            note = f" Last error: {err}" if err else ""
            return f"Screen mirror is not running.{note}"
        up = time.time() - _STATE.get("started", time.time())
        return (f"Screen mirror LIVE — "
                f"http://127.0.0.1:{_STATE.get('port')}/stream — "
                f"{_STATE.get('frames', 0)} frame(s) captured, "
                f"{_STATE.get('served', 0)} sent, up {up:.0f}s, "
                f"{_STATE.get('fps', 4)} fps.")


# ── handler ─────────────────────────────────────────────────────────────────

def screen_mirror(parameters: dict = None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").lower().strip()
    if action == "start":
        try:
            port = int(params.get("port") or 8699)
        except (TypeError, ValueError):
            return "port must be a number."
        if not (1024 <= port <= 65535):
            return "port must be between 1025 and 65535 (below 1024 = root)."
        try:
            fps = int(params.get("fps") or 4)
        except (TypeError, ValueError):
            return "fps must be a number."
        fps = max(1, min(15, fps))
        with _LOCK:
            if _STATE.get("running"):
                return (f"Screen mirror already running at "
                        f"http://127.0.0.1:{_STATE.get('port')}/stream "
                        f"(action=stop first to change port/fps).")
        out = _start(port, fps)
    elif action == "stop":
        out = _stop()
    elif action == "status":
        out = _status()
    else:
        out = "action must be start | stop | status."
    if player is not None and action == "start" and "LIVE" in out:
        try:
            player.show_content("SCREEN MIRROR", out[:500])
        except Exception:
            pass
    return out


TOOL = {
    "name": "screen_mirror",
    "description": (
        "Live mirror of this computer's display — starts a loopback-only "
        "(127.0.0.1) MJPEG stream and gives a URL to open in any browser. "
        "Use for 'show my screen live', 'mirror the display', 'watch what "
        "the gui agent is doing'. actions: start (optional port, fps), "
        "status, stop. Nothing leaves the machine."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status (default) | start | stop"},
            "port": {"type": "NUMBER",
                     "description": "Loopback port for start. Default 8699."},
            "fps": {"type": "NUMBER",
                    "description": "Capture rate 1-15. Default 4."},
        },
        "required": [],
    },
    "handler": screen_mirror,
}
