"""The wake word: core/wake_word.py.

Two things can go wrong here and both are quiet. The detector can fail to load
(and the user presses a button that does nothing), or it can fail to *stay*
quiet — firing on the television, or firing twice on one utterance, which starts
two sessions from one "Hey Jarvis".

The model itself is a 330 MB download, so what is tested here is the machinery
around it: readiness that does not lie, a start() that fails honestly, an
inference loop driven by a fake model, and the drain that stops double-fires.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from core import wake_word
from core.wake_word import (DEFAULT_THRESHOLD, SAMPLE_RATE, WAKE_MODEL,
                            WakeWordDetector, is_installed, is_ready)


class TestReadiness:
    def test_it_never_raises(self):
        """Called from the UI thread while the drawer is being built."""
        assert isinstance(is_installed(), bool)
        assert isinstance(is_ready(), bool)

    def test_ready_implies_installed(self):
        if is_ready():
            assert is_installed()

    def test_readiness_is_a_file_check_not_a_model_construction(self, monkeypatch):
        """Constructing a Model to probe readiness was slow AND clashed with the
        detector's own model, which made the UI flicker to 'not downloaded'."""
        import sys
        import types
        fake = types.ModuleType("openwakeword")
        fake.__file__ = str(Path("/nonexistent/openwakeword/__init__.py"))
        monkeypatch.setitem(sys.modules, "openwakeword", fake)
        monkeypatch.setattr(wake_word, "is_installed", lambda: True)
        assert is_ready() is False          # no resources/models dir → not ready

    def test_all_three_model_files_are_required(self, monkeypatch, tmp_path):
        import sys
        import types
        models = tmp_path / "openwakeword" / "resources" / "models"
        models.mkdir(parents=True)
        (models / f"{WAKE_MODEL}_v0.1.onnx").write_bytes(b"x")
        fake = types.ModuleType("openwakeword")
        fake.__file__ = str(tmp_path / "openwakeword" / "__init__.py")
        monkeypatch.setitem(sys.modules, "openwakeword", fake)
        monkeypatch.setattr(wake_word, "is_installed", lambda: True)
        # the wake model alone is not enough — mel + embedding are its inputs
        assert is_ready() is False
        (models / "melspectrogram.onnx").write_bytes(b"x")
        assert is_ready() is False
        (models / "embedding_model.onnx").write_bytes(b"x")
        assert is_ready() is True

    def test_a_tflite_set_also_counts(self, monkeypatch, tmp_path):
        """The package ships either framework depending on how it was built."""
        import sys
        import types
        models = tmp_path / "openwakeword" / "resources" / "models"
        models.mkdir(parents=True)
        for name in (f"{WAKE_MODEL}_v0.1.tflite", "melspectrogram.tflite",
                     "embedding_model.tflite"):
            (models / name).write_bytes(b"x")
        fake = types.ModuleType("openwakeword")
        fake.__file__ = str(tmp_path / "openwakeword" / "__init__.py")
        monkeypatch.setitem(sys.modules, "openwakeword", fake)
        monkeypatch.setattr(wake_word, "is_installed", lambda: True)
        assert is_ready() is True

    def test_the_ui_contract_is_unchanged(self):
        assert WAKE_MODEL == "hey_jarvis"
        assert SAMPLE_RATE == 16000
        assert 0.0 < DEFAULT_THRESHOLD < 1.0


class FakeModel:
    """Stands in for openwakeword's Model: predictable scores, real predict()."""

    def __init__(self, scores):
        self._scores = list(scores)
        self.calls = []

    def predict(self, frame):
        """Returns the shape openwakeword really returns: one score per model,
        keyed by model name. A bare float would make the loader look broken."""
        self.calls.append(len(frame))
        score = self._scores.pop(0) if self._scores else 0.0
        if isinstance(score, dict):
            return score
        return {"hey_jarvis_v0.1": float(score)}


@pytest.fixture()
def detector(monkeypatch):
    """A detector with a fake model installed, as if start() had succeeded."""
    made = {}

    def make(scores=(0.9,), threshold=DEFAULT_THRESHOLD, on_detect=None):
        fired = on_detect or (lambda: None)
        d = WakeWordDetector(fired, threshold=threshold,
                             logger=lambda _m: None, notify=lambda _m: None)
        d._model = FakeModel(list(scores))
        d._running = True
        d._ready = True
        made["d"] = d
        return d

    yield make
    for d in made.values():
        d.stop()


class TestStart:
    def test_it_returns_false_instead_of_raising_when_the_model_is_missing(
            self, monkeypatch):
        logs, notices = [], []
        d = WakeWordDetector(lambda: None, logger=logs.append,
                             notify=notices.append)
        monkeypatch.setitem(__import__("sys").modules, "openwakeword", None)
        assert d.start() is False
        assert d.ready is False
        assert logs and "could not load model" in logs[0]
        # and it tells the user what to do instead of failing silently
        assert notices and "WAKE NOW" in notices[0]

    def test_starting_twice_is_a_no_op(self, detector, monkeypatch):
        d = detector()
        d._thread = None
        assert d.start() is True                 # already running
        assert d.ready is True

    def test_stop_clears_readiness(self, detector):
        d = detector()
        d.stop()
        assert d.ready is False and d._model is None


class TestFeed:
    def test_nothing_is_queued_before_start(self):
        d = WakeWordDetector(lambda: None, logger=lambda _m: None)
        d.feed(__import__("numpy").zeros(1280, dtype="int16"))
        assert d._queue.empty()

    def test_a_mono_frame_goes_in_copied(self, detector):
        import numpy as np
        d = detector()
        frame = np.arange(64, dtype=np.int16)
        d.feed(frame)
        frame[:] = 0                              # the mic reuses its buffer
        queued = d._queue.get_nowait()
        assert queued[0] == 0 and len(queued) == 64

    def test_a_stereo_frame_is_flattened_to_one_channel(self, detector):
        """PortAudio hands back (frames, channels); the model wants 1-D."""
        import numpy as np
        d = detector()
        stereo = np.zeros((32, 2), dtype=np.int16)
        stereo[:, 1] = 7
        d.feed(stereo)
        queued = d._queue.get_nowait()
        assert queued.ndim == 1 and len(queued) == 32

    def test_a_full_queue_drops_the_frame_instead_of_blocking(self, detector):
        """This runs on the audio callback thread — blocking it drops mic audio
        and glitches every sound the app makes."""
        import numpy as np
        d = detector()
        for _ in range(200):
            d.feed(np.zeros(1280, dtype=np.int16))     # queue is capped at 50
        assert d._queue.qsize() <= 50

    def test_garbage_input_is_swallowed(self, detector):
        d = detector()
        d.feed("not an array")                    # no raise, no crash
        d.feed(None)

    def test_the_queue_is_drained_after_a_detection(self, detector):
        d = detector()
        for _ in range(20):
            d.feed(__import__("numpy").zeros(64, dtype="int16"))
        d._drain()
        assert d._queue.empty()


class TestInferenceLoop:
    def test_a_detection_fires_the_callback(self, detector):
        fired = threading.Event()
        d = detector(scores=(0.99,), on_detect=fired.set)
        import numpy as np
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        d.feed(np.zeros(SAMPLE_RATE // 10, dtype="int16"))
        assert fired.wait(2.0), "detection never fired"
        d._running = False

    def test_a_low_score_does_not_fire(self, detector):
        fired = threading.Event()
        d = detector(scores=(0.01, 0.01, 0.01), on_detect=fired.set)
        import numpy as np
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        for _ in range(3):
            d.feed(np.zeros(1280, dtype="int16"))
        assert not fired.wait(0.4)
        d._running = False

    def test_the_threshold_is_what_decides(self, detector):
        """A model score of 0.6 is a detection at 0.5 and not at 0.7."""
        for threshold, should_fire in ((0.5, True), (0.7, False)):
            fired = threading.Event()
            d = detector(scores=(0.6,), threshold=threshold, on_detect=fired.set)
            import numpy as np
            d._thread = threading.Thread(target=d._loop, daemon=True)
            d._thread.start()
            d.feed(np.zeros(1280, dtype="int16"))
            assert fired.wait(0.6) is should_fire, threshold
            d._running = False

    def test_it_stops_when_told_to(self, detector):
        d = detector(scores=[])
        import numpy as np
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        d.stop()
        d._thread.join(timeout=2.0)
        assert not d._thread.is_alive()

    def test_one_utterance_fires_once(self, detector):
        """Undrained frames from the same "Hey Jarvis" would fire again a moment
        later and start a second session."""
        calls = []
        d = detector(scores=(0.99, 0.99, 0.99, 0.99), on_detect=lambda: calls.append(1))
        import numpy as np
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        d.feed(np.zeros(1280, dtype="int16"))
        time.sleep(0.25)                     # the loop drained the rest
        d._running = False
        assert len(calls) == 1

    def test_a_crashing_callback_does_not_kill_the_loop(self, detector):
        """The mic must keep working after one bad detection."""
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("callback exploded")

        d = detector(scores=(0.99, 0.99), on_detect=boom)
        import numpy as np
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        for _ in range(2):
            d.feed(np.zeros(1280, dtype="int16"))
            time.sleep(0.1)
        d._running = False
        assert calls                            # it fired, and the thread survived

    def test_a_crashing_model_is_logged_and_survived(self, detector):
        logs = []

        class Boom(FakeModel):
            def predict(self, frame):
                raise RuntimeError("inference blew up")

        d = detector(scores=())
        d._model = Boom([])
        d._logger = logs.append
        import numpy as np
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        d.feed(np.zeros(1280, dtype="int16"))
        time.sleep(0.2)
        d._running = False
        assert any("inference error" in m for m in logs)


class TestScoreInterpretation:
    """The model's dict keys carry a version suffix, and a build could add one
    more — matching on the name keeps this working across versions."""

    def _run_one(self, scores, threshold=0.5):
        import numpy as np
        fired = threading.Event()
        d = WakeWordDetector(fired.set, threshold=threshold, logger=lambda _m: None)
        d._model = FakeModel([scores])
        d._running = True
        d._thread = threading.Thread(target=d._loop, daemon=True)
        d._thread.start()
        d.feed(np.zeros(1280, dtype="int16"))
        got = fired.wait(0.6)
        d._running = False
        return got

    def test_it_finds_the_jarvis_key_among_others(self):
        assert self._run_one({"alexa": 0.1, "hey_jarvis_v0.2": 0.9}) is True

    def test_it_falls_back_to_the_highest_score(self):
        """A renamed model should still work rather than never firing."""
        assert self._run_one({"something_else": 0.9}) is True

    def test_a_non_dict_result_does_not_crash(self):
        assert self._run_one(None) is False
