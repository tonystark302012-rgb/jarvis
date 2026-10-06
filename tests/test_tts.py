"""core/tts.py — the TTS engines and their playback plumbing.

Two things worth saying before the tests.

**This module is not on the Live audio path.** Speech in a normal session comes
back as audio frames from Gemini Live; nothing calls create_tts_player() today
(`PROJECT_ANALYSIS.md` P1-10 assumed it did). These tests exist because the
module is real, self-contained code that someone will wire up for offline/local
voice — and when they do, the contract should already be pinned rather than
discovered.

**Nothing here talks to a network or an audio device.** EdgeTTS and ElevenLabs
are cloud services and Kokoro is a 330 MB model, so the parts tested are the
ones that decide *what* to say and *how* to hand it to the speaker: silence
compression, sample conversion, engine selection, thread-safe playback state,
and the promise that a broken engine does not take the caller down with it.
"""
from __future__ import annotations

import sys
import types

import numpy as np
import pytest

# sounddevice needs PortAudio, which is a system library CI does not install.
# The module imports it at import time, so a stand-in goes in first — and it is
# removed again on teardown, because a fake left in sys.modules is worse than
# no test at all.
_REAL_SD = sys.modules.get("sounddevice")
if _REAL_SD is None or not hasattr(_REAL_SD, "play"):
    _stub = types.ModuleType("sounddevice")
    _stub.play = lambda *a, **k: None
    _stub.wait = lambda *a, **k: None
    _stub.stop = lambda *a, **k: None
    _stub.query_devices = lambda *a, **k: []
    _stub.CallbackFlags = type("CallbackFlags", (), {})
    _stub.PortAudioError = type("PortAudioError", (Exception,), {})
    sys.modules["sounddevice"] = _stub

from core import tts  # noqa: E402  (after the sounddevice stand-in)
from core.tts import (TTSPlayer, _compress_silence, _to_numpy,  # noqa: E402
                      create_tts_player)


class TestToNumpy:
    def test_a_list_becomes_float32(self):
        out = _to_numpy([0, 1, -1])
        assert out.dtype == np.float32 and list(out) == [0.0, 1.0, -1.0]

    def test_a_numpy_array_survives(self):
        src = np.arange(8, dtype=np.float32)
        assert np.array_equal(_to_numpy(src), src)

    def test_int16_audio_keeps_its_values(self):
        """The caller scales; this function must not silently normalise or clip."""
        out = _to_numpy(np.array([0, 32767, -32768], dtype=np.int16))
        assert out[1] == 32767.0 and out[2] == -32768.0

    def test_a_torch_like_tensor_uses_detach(self):
        """Kokoro >= 0.9 returns a torch tensor. Reading numpy off it raises
        when torch was built against a different numpy, so the fallback path
        matters — reproduced here with a stub that raises on .numpy()."""

        class FakeTensor:
            def detach(self): return self
            def cpu(self): return self
            def float(self): return self
            def numpy(self): raise RuntimeError("Numpy is not available")
            def tolist(self): return [0.25, 0.5]

        assert list(_to_numpy(FakeTensor())) == [0.25, 0.5]

    def test_the_fast_path_is_used_when_numpy_works(self):
        class FakeTensor:
            def detach(self): return self
            def cpu(self): return self
            def float(self): return self
            def numpy(self): return np.array([1.0, 2.0], dtype=np.float32)

        assert list(_to_numpy(FakeTensor())) == [1.0, 2.0]

    def test_empty_input_is_an_empty_array(self):
        assert _to_numpy([]).size == 0


class TestCompressSilence:
    """Kokoro leaves 1–2 s of silence at punctuation. Left alone, the avatar
    stands there with its mouth shut for two seconds, which reads as a stall."""

    def test_a_long_pause_is_shortened(self):
        quiet = np.zeros(24_000, dtype=np.float32)          # 1 s of silence
        out = _compress_silence(quiet, 24_000, max_silence_ms=500)
        assert len(out) < len(quiet)

    def test_speech_is_not_touched(self):
        speech = (np.sin(np.arange(24_000) / 10.0) * 0.5).astype(np.float32)
        out = _compress_silence(speech, 24_000, max_silence_ms=500)
        assert len(out) == len(speech)

    def test_the_result_is_never_longer(self):
        signal = np.concatenate([
            np.zeros(4800, dtype=np.float32),
            (np.sin(np.arange(9600) / 5.0) * 0.4).astype(np.float32),
            np.zeros(4800, dtype=np.float32),
        ])
        assert len(_compress_silence(signal, 24_000)) <= len(signal)

    def test_it_does_not_clip_quiet_consonants(self):
        """A threshold that is too high eats the quiet start of words, which is
        audible as a lisp."""
        soft = (np.sin(np.arange(2400) / 7.0) * 0.02).astype(np.float32)
        out = _compress_silence(soft, 24_000)
        assert len(out) == len(soft)

    def test_all_silence_does_not_return_nothing(self):
        """Returning an empty array here would hand the player silence where it
        expected audio; the guard is `if out else arr`."""
        out = _compress_silence(np.zeros(120_000, dtype=np.float32), 24_000)
        assert out.size > 0

    def test_empty_input_is_survivable(self):
        assert _compress_silence(np.array([], dtype=np.float32)).size == 0

    def test_a_different_sample_rate_is_respected(self):
        """The cap is in milliseconds, so 48 kHz must compress twice as many
        samples as 24 kHz for the same pause."""
        at24 = len(_compress_silence(np.zeros(48_000, dtype=np.float32),
                                     24_000, max_silence_ms=250))
        at48 = len(_compress_silence(np.zeros(96_000, dtype=np.float32),
                                     48_000, max_silence_ms=250))
        assert at48 > at24


class _RecordingEngine:
    def __init__(self, raise_with=None):
        self.spoken: list[str] = []
        self.raise_with = raise_with

    def speak(self, text: str) -> None:
        self.spoken.append(text)
        if self.raise_with:
            raise self.raise_with


class TestTTSPlayer:
    def test_speak_passes_the_text_to_the_engine(self):
        engine = _RecordingEngine()
        TTSPlayer(engine).speak("hello")
        assert engine.spoken == ["hello"]

    def test_the_callbacks_run_in_order(self):
        order = []
        TTSPlayer(_RecordingEngine()).speak(
            "hi", on_start=lambda: order.append("start"),
            on_done=lambda: order.append("done"))
        assert order == ["start", "done"]

    def test_a_failing_engine_is_swallowed_and_still_reports_done(self):
        """This runs on a dedicated thread; an exception here would kill the
        thread and the assistant would go mute for the rest of the session."""
        seen = []
        TTSPlayer(_RecordingEngine(raise_with=RuntimeError("no network"))).speak(
            "hi", on_done=lambda: seen.append("done"))
        assert seen == ["done"]

    def test_is_playing_is_false_before_and_after(self):
        player = TTSPlayer(_RecordingEngine())
        assert player.is_playing is False
        player.speak("hi")
        assert player.is_playing is False

    def test_is_playing_is_true_while_the_engine_is_speaking(self):
        player = TTSPlayer(None)

        class Slow:
            def speak(self, text):
                assert player.is_playing is True

        player._engine = Slow()
        player.speak("hi")

    def test_stop_clears_the_flag(self):
        player = TTSPlayer(_RecordingEngine())
        player.stop()
        assert player.is_playing is False

    def test_the_lock_is_real(self):
        """Two threads speaking at once is a real sequence — the HUD can ask
        for a sentence while a tool result is still being read out."""
        import threading
        import time
        player = TTSPlayer(None)
        inside = []

        class Slow:
            def speak(self, text):
                inside.append(text)
                time.sleep(0.05)

        player._engine = Slow()
        threads = [threading.Thread(target=player.speak, args=(f"line {i}",))
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)
        assert sorted(inside) == [f"line {i}" for i in range(5)]
        assert player.is_playing is False


class TestCreatePlayer:
    def test_it_defaults_to_edge_tts(self):
        player = create_tts_player({})
        assert player._engine.__class__.__name__ == "EdgeTTSEngine"

    def test_the_voice_setting_is_honoured(self):
        player = create_tts_player({"tts_voice": "en-GB-SoniaNeural"})
        assert player._engine.voice == "en-GB-SoniaNeural"

    def test_the_engine_name_is_case_insensitive(self):
        assert create_tts_player({"tts_engine": "EDGETTS"})._engine.voice

    def test_an_unknown_engine_falls_back_rather_than_crashing(self):
        """A config typo must not leave the user with no voice at all."""
        player = create_tts_player({"tts_engine": "nonsense"})
        assert player._engine.__class__.__name__ == "EdgeTTSEngine"

    def test_elevenlabs_needs_no_network_to_construct(self):
        player = create_tts_player({"tts_engine": "elevenlabs",
                                    "elevenlabs_api_key": "k",
                                    "tts_voice": "custom-voice"})
        assert player._engine.api_key == "k"
        assert player._engine.voice_id == "custom-voice"

    def test_an_empty_api_key_is_not_validated_at_construction(self):
        """Failing here would break app start-up for a user who simply has not
        filled the key in yet."""
        player = create_tts_player({"tts_engine": "elevenlabs"})
        assert player._engine.api_key == ""


class TestKokoroLanguage:
    """Voice names are prefixed by language (`af_heart`, `jf_alpha`) and the
    prefix picks the model's language pack."""

    @pytest.mark.parametrize("voice,expected", [
        ("af_heart", "a"), ("am_adam", "a"),
        ("bf_emma", "b"), ("jf_alpha", "j"),
    ])
    def test_the_prefix_selects_the_language(self, voice, expected):
        engine = tts.KokoroTTSEngine.__new__(tts.KokoroTTSEngine)
        engine.voice = voice
        assert engine._lang_code == expected

    def test_every_shipped_pack_is_covered(self):
        for prefix in "abjzsfhipre":
            engine = tts.KokoroTTSEngine.__new__(tts.KokoroTTSEngine)
            engine.voice = f"{prefix}f_heart"
            assert engine._lang_code == prefix

    def test_an_unknown_prefix_falls_back_to_english(self):
        engine = tts.KokoroTTSEngine.__new__(tts.KokoroTTSEngine)
        engine.voice = "qf_something"          # 'q' is not a Kokoro pack
        assert engine._lang_code == "a"

    def test_an_empty_voice_name_does_not_crash(self):
        engine = tts.KokoroTTSEngine.__new__(tts.KokoroTTSEngine)
        engine.voice = ""
        assert engine._lang_code == "a"


class TestUnreachable:
    """A guard on the finding this suite was written around."""

    def test_nothing_in_the_app_calls_create_tts_player(self):
        """If this starts failing, someone wired TTS up — good news, and the
        note in PROJECT_ANALYSIS P1-10 should go with it."""
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        callers = []
        for p in root.rglob("*.py"):
            if any(part in p.parts for part in (".venv", "tests", "__pycache__")):
                continue
            if p.name == "tts.py":
                continue
            if "create_tts_player" in p.read_text(encoding="utf-8"):
                callers.append(str(p.relative_to(root)))
        assert callers == [], (
            "create_tts_player is now called from "
            f"{callers} — update PROJECT_ANALYSIS Part 9 (P1-10) and drop this "
            "test, because core/tts.py is reachable now")

    def test_the_module_says_so_itself(self):
        """The docstring is the only place a reader of the file finds out."""
        assert "not on the live audio path" in tts.__doc__.lower()
