"""Security-camera motion detection for the phone→PC camera pipe.

Every frame the phone POSTs to /api/camera-frame is diffed against the
previous one. When enough of the picture changes — and the cooldown since
the last alert has passed — the server broadcasts a {"type": "motion"}
event the dashboard (and anything else listening) can react to.

Design:
  * decode is a SEAM (`_decode`): PIL is optional. Without it (or on a
    frame PIL cannot parse) `feed` returns None and the pipe keeps
    working — motion detection degrades to "off", it never breaks the
    camera feed.
  * the interesting logic — baseline, threshold, cooldown — lives in
    `update()`, which takes plain frame vectors, so it is unit-testable
    without PIL or a running server.
  * comparison is mean absolute pixel difference on a 64×48 grayscale
    thumbnail: JPEG noise sits far below 0.02, a person entering a room
    pushes 0.1+.  Full-size comparison would amplify codec noise and
    cost 60× more; a thumbnail is the right level for "did the scene
    change".
"""

from __future__ import annotations

import io

# Thumbnail size for comparison — small enough to diff every frame,
# large enough that a person-sized change is unmistakable.
GRID = (64, 48)

# Mean |Δ| over 0..255 pixels, normalised to 0..1.  Static scene with
# JPEG re-encoding noise ≈ 0.01–0.03; a body walking into frame ≥ 0.10.
DEFAULT_THRESHOLD = 0.08

# Seconds between two motion events — stops a continuous change (a fan,
# a curtain) from flooding the dashboard.
DEFAULT_COOLDOWN = 3.0


def frame_diff(prev: list[float], cur: list[float]) -> float:
    """Mean absolute difference of two equal-length frame vectors, 0..1."""
    if not prev or len(prev) != len(cur):
        return 1.0           # incomparable → treat as full change
    total = sum(abs(a - b) for a, b in zip(prev, cur))
    return total / (len(cur) * 255.0)


def _decode(data: bytes) -> list[float] | None:
    """JPEG bytes → 64×48 grayscale vector, or None (no PIL / bad frame).

    Kept separate from `update` so tests can drive the logic with plain
    vectors and so a PIL failure can never propagate into the request
    handler.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            thumb = img.convert("L").resize(GRID)
            # PIL's stub types getdata() loosely (ImagingCore); the value is
            # iterable at runtime for an "L" image.
            return [float(v) for v in list(thumb.getdata())]  # type: ignore[arg-type]
    except Exception:
        return None


class MotionDetector:
    """Stateful frame-over-frame change detector with a cooldown."""

    def __init__(self, threshold: float = DEFAULT_THRESHOLD,
                 cooldown: float = DEFAULT_COOLDOWN) -> None:
        self.threshold = float(threshold)
        self.cooldown = float(cooldown)
        self._prev: list[float] | None = None
        self._last_alert = 0.0

    @property
    def armed(self) -> bool:
        """True once a baseline frame exists (first frame never alerts)."""
        return self._prev is not None

    def update(self, cur: list[float], now: float) -> dict | None:
        """Feed a decoded frame; return a motion event or None.

        First frame only sets the baseline.  Subsequent frames must both
        exceed `threshold` AND respect `cooldown` since the last alert.
        """
        prev, self._prev = self._prev, cur
        if prev is None:
            return None                       # baseline, nothing to compare
        score = frame_diff(prev, cur)
        if score < self.threshold:
            return None
        if now - self._last_alert < self.cooldown:
            return None
        self._last_alert = now
        return {"type": "motion", "score": round(score, 3),
                "ts": float(now)}

    def feed(self, data: bytes, now: float) -> dict | None:
        """JPEG bytes → motion event (None: no PIL, undecodable, or calm).

        Never raises: this runs inside the camera request handler.
        """
        try:
            cur = _decode(data)
            if cur is None:
                return None
            return self.update(cur, now)
        except Exception:
            return None
