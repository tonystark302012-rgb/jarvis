"""
webrtc — low-latency WebRTC upgrade for the EXISTING screen mirror
(Report P2 / aiortc). The JPEG-over-websocket mirror stays the
reliable default; this is the fast path ON TOP of it:

  * same capture (PIL ImageGrab, like dashboard/server._mirror_loop)
  * aiortc RTCPeerConnection answers the dashboard's SDP offer
  * browser gets a real <video> track; if anything is missing the
    dashboard silently keeps the JPEG flow (progressive, never worse)

GUARDED: aiortc (+av) is a free MIT dependency but heavy —
`pip install aiortc`. Without it every entry point returns that exact
line; no fake SDP, no stub sessions.

One-server rule intact: negotiation rides the dashboard's existing
authenticated HTTP (POST /api/webrtc/offer) — no new listener.
"""
from __future__ import annotations

_INSTALL = ("aiortc not installed — free (BSD-3): `pip install aiortc` "
            "(bundles PyAV). The JPEG mirror keeps working without it; "
            "WebRTC answers are only produced when the library is real.")
_state: dict = {"pc": None, "negotiations": 0, "last_error": ""}


def _require():
    """Import aiortc or raise the honest install line. Seam for tests."""
    try:
        from aiortc import RTCPeerConnection, RTCSessionDescription
        return RTCPeerConnection, RTCSessionDescription
    except Exception as e:
        raise RuntimeError(_INSTALL) from e


def _grab_frame():
    """Shared capture — same source the websocket mirror uses."""
    import numpy as np
    from PIL import ImageGrab
    img = ImageGrab.grab()
    w, h = img.size
    if w > 960:
        img = img.resize((960, max(1, int(h * 960 / w))))
    return np.asarray(img.convert("RGB"))


def _make_track():
    """aiortc VideoStreamTrack pulling screen frames at ~10 fps."""
    import time
    from av import VideoFrame
    from aiortc import VideoStreamTrack

    class ScreenTrack(VideoStreamTrack):
        kind = "video"

        def __init__(self):
            super().__init__()
            self._start = time.time()

        async def recv(self):
            pts, time_base = await self.next_timestamp()
            try:
                arr = _grab_frame()
            except Exception:
                # headless/locked — send a black frame instead of killing
                # the peer connection (viewer sees black, session lives)
                import numpy as np
                arr = np.zeros((360, 640, 3), dtype="uint8")
            frame = VideoFrame.from_ndarray(arr, format="rgb24")
            frame.pts = pts
            frame.time_base = time_base
            return frame

    return ScreenTrack()


async def answer(offer_sdp: str) -> str:
    """SDP offer → SDP answer over a fresh PC carrying the screen track."""
    if not str(offer_sdp or "").strip():
        raise ValueError("empty SDP offer")
    RTCPeerConnection, RTCSessionDescription = _require()
    await stop()                              # one session at a time
    pc = RTCPeerConnection()
    _state["pc"] = pc
    try:
        pc.addTrack(_make_track())
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=str(offer_sdp), type="offer"))
        await pc.setLocalDescription(await pc.createAnswer())
        _state["negotiations"] += 1
        _state["last_error"] = ""
        return pc.localDescription.sdp
    except Exception as e:
        _state["last_error"] = str(e)[:200]
        try:
            await pc.close()
        except Exception:
            pass
        _state["pc"] = None
        raise


async def stop() -> bool:
    pc = _state.get("pc")
    _state["pc"] = None
    if pc is None:
        return False
    try:
        await pc.close()
    except Exception:
        pass
    return True


def status() -> str:
    try:
        _require()
        available = True
    except RuntimeError:
        available = False
    live = _state.get("pc") is not None
    if not available:
        return f"WebRTC mirror: unavailable — {_INSTALL}"
    if live:
        return (f"WebRTC mirror: LIVE session "
                f"({_state['negotiations']} negotiation(s) so far).")
    return ("WebRTC mirror: ready (no active session) — the dashboard "
            "opens it automatically when mirror is ON; JPEG fallback "
            "stays active if negotiation fails.")
