"""The audio loop, run against a fake sounddevice — core/audio_loop.py.

This is the part of the assistant that has never had a test: it needs a
microphone, a speaker and a Live session, so it was only ever exercised by
talking to it. The loop itself is three asyncio pumps around two device
streams, and with a fake stream and a fake session all three can be run for a
few hundred milliseconds and asked what they did.

What is pinned here is what the code comments promise: the reply's bytes reach
the output device, a chosen device that refuses to open falls back to the
default *and says so*, the mouth gets a schedule of 20 ms frames rather than one
averaged number, `set_speaking` brackets the reply so the mic knows when it is
hearing us, and a queued chunk actually reaches the Live session.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as M                                                     # noqa: E402
from core import audio_devices as ad                                 # noqa: E402
from core import audio_loop as al                                    # noqa: E402
from core.audio_pcm import _clean_transcript, _is_repeat_chunk, _pcm_level  # noqa: E402
from core.plugin_loader import discover_plugins                      # noqa: E402


# ── the fake outside world ─────────────────────────────────────────────────
class OutputStream:
    """A speaker: it records what it was given and reports a latency."""

    latency = 0.02

    def __init__(self, refuse=False):
        self.written = bytearray()
        self.started = False
        self.closed = False
        self.refuse = refuse

    def start(self):
        if self.refuse:
            raise RuntimeError("device is busy (exclusive mode)")
        self.started = True

    def write(self, data: bytes):
        self.written.extend(data)

    def stop(self): pass
    def close(self): self.closed = True


class FakeSD:
    def __init__(self, refuse=()):
        self.refuse = refuse
        self.opened: list = []
        self.streams: list[OutputStream] = []

    def RawOutputStream(self, **kw):
        self.opened.append(kw.get("device"))
        stream = OutputStream(refuse=kw.get("device") in self.refuse)
        self.streams.append(stream)
        return stream


class FakeUI:
    """Only what the audio path touches."""

    muted = False

    def __init__(self):
        self.logs: list[str] = []
        self.levels: list[float] = []
        self.visemes: list[tuple] = []
        self.states: list[str] = []
        self.emotions: list[str] = []
        self.events: list[tuple[str, object]] = []      # ordered: the point

    def write_log(self, text):
        self.logs.append(str(text))
        self.events.append(("log", str(text)))

    def set_audio_level(self, lvl): self.levels.append(lvl)
    def push_visemes(self, frames, hop, at): self.visemes.append((frames, hop, at))
    def set_state(self, s): self.states.append(s)

    def set_emotion(self, e):
        self.emotions.append(e)
        self.events.append(("emotion", e))

    def show_content(self, *a, **k): pass
    def stop_camera_stream(self): pass


class FakeSession:
    def __init__(self):
        self.sent: list = []

    async def send_realtime_input(self, **kw):
        self.sent.append(kw)


class Registry:
    def __init__(self): self._records: dict = {}
    def get_tool_declarations(self): return []
    def names(self): return set()
    def has(self, name): return False
    def scheduling(self, name): return None
    def run(self, name, parameters, ctx=None): return "Done."


@pytest.fixture()
def live(monkeypatch, tmp_path):
    fake = FakeSD()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    ui = FakeUI()
    lv = M.JarvisLive(ui, action_registry=Registry(),
                      plugin_registry=discover_plugins(tmp_path, set(),
                                                       logger=lambda _m: None),
                      base_dir=ROOT)
    lv.audio_in_queue = asyncio.Queue()
    lv._echo = type("E", (), {"is_user_speech": lambda *a: True,
                              "should_interrupt": lambda *a: False,
                              "note_interrupted": lambda *a: None})()
    lv._viseme = type("V", (), {"reset": lambda *a: None})()
    return lv, ui, fake


def _pcm(ms: int, sr: int = 24000, level: float = 8000.0) -> bytes:
    """A tone-ish block: loud enough that the level maths has something to do."""
    n = int(sr * ms / 1000)
    x = (np.sin(np.linspace(0, 40 * np.pi, n)) * level).astype(np.int16)
    return x.tobytes()


def _run(coro, seconds: float):
    async def _scenario():
        task = asyncio.create_task(coro)
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())


# ── the playback pump ──────────────────────────────────────────────────────
class TestThePlaybackPump:
    def test_what_the_model_sent_reaches_the_speaker(self, live):
        lv, _ui, fake = live
        lv.audio_in_queue.put_nowait(_pcm(100))
        _run(lv._play_audio(), 0.35)
        assert bytes(fake.streams[0].written) == _pcm(100)

    def test_a_reply_brackets_itself_as_speaking(self, live):
        """Everything else keys off this flag: the mic stops streaming, the
        echo tail opens, the phone relay pauses. The pump raises it when audio
        arrives and is the thing that lowers it, because only the pump knows the
        queue has drained — the UI sees the two states in that order."""
        lv, ui, fake = live
        assert lv._is_speaking is False
        lv.audio_in_queue.put_nowait(_pcm(50))
        _run(lv._play_audio(), 0.25)
        assert ui.states == ["SPEAKING", "LISTENING"]
        assert lv._is_speaking is False           # cancelled → the finally ran
        assert fake.streams[0].closed is True, "the device was left open"

    def test_the_mouth_gets_a_schedule_not_a_single_number(self, live):
        """One averaged level per write batch could only ever flap; the frames
        are 20 ms apart and carry the timestamp they are to be played at."""
        lv, ui, _fake = live
        lv.audio_in_queue.put_nowait(_pcm(100))
        _run(lv._play_audio(), 0.35)
        assert ui.visemes, "no viseme schedule was handed to the HUD"
        frames, hop, at = ui.visemes[0]
        assert hop == pytest.approx(al._VIS_HOP / al.RECEIVE_SAMPLE_RATE)
        assert frames and len(frames) == 5          # 100 ms at 20 ms a frame
        assert at >= time.time() - 1.0              # a real clock, not 0
        assert all(0.0 <= f[0] <= 1.0 for f in frames)

    def test_a_smaller_batch_is_not_held_back(self, live):
        """Batching exists to cut thread round-trips, not to add latency: a
        single 43 ms chunk must go out on its own."""
        lv, _ui, fake = live
        lv.audio_in_queue.put_nowait(_pcm(43))
        _run(lv._play_audio(), 0.25)
        assert fake.streams[0].written

    def test_a_chosen_device_that_refuses_to_open_falls_back_and_says_so(
            self, live, monkeypatch):
        """Exclusive-mode, wrong sample rate, a device that went to sleep — a
        named output the host API accepts can still refuse. That must not cost
        the user their voice."""
        import memory.config_manager as cm
        lv, ui, fake = live
        fake.refuse = {7}
        monkeypatch.setattr(al, "get_output_device", lambda: "Studio Monitors")
        monkeypatch.setattr(al.audio_devices, "resolve", lambda name, kind: 7)
        lv.audio_in_queue.put_nowait(_pcm(43))
        _run(lv._play_audio(), 0.35)
        assert fake.opened == [7, None]             # fell back to the default
        assert any("unavailable" in m for m in ui.logs)
        assert fake.streams[1].written, "the fallback stream got no audio"
        assert cm is not None

    def test_the_latency_the_device_reports_sizes_the_echo_tail(self, live):
        lv, _ui, fake = live
        fake.streams.clear()
        lv.audio_in_queue.put_nowait(_pcm(43))
        _run(lv._play_audio(), 0.25)
        assert lv._out_latency == pytest.approx(OutputStream.latency)

    def test_the_end_of_a_turn_stops_claiming_to_speak(self, live):
        """The receive loop sets the event when the turn is done; the pump is
        what clears it, because only the pump knows the queue has drained."""
        lv, _ui, _fake = live
        lv._turn_done_event = asyncio.Event()

        async def scenario():
            lv._turn_done_event.set()
            task = asyncio.create_task(lv._play_audio())
            await asyncio.sleep(0.25)
            task.cancel()

        lv.set_speaking(True)
        asyncio.run(scenario())
        assert lv._is_speaking is False
        assert lv._turn_done_event.is_set() is False


class TestTheMicrophoneToSessionPump:
    def test_a_queued_chunk_reaches_the_live_session(self, live):
        """`_send_realtime` is what turns queue items into model input; the
        field name matters — the old `media=` form makes the Live API close the
        socket with a 1007."""
        lv, _ui, _fake = live
        session = FakeSession()
        lv.session = session
        lv.out_queue = asyncio.Queue()
        lv.out_queue.put_nowait({"data": b"pcm-bytes", "mime_type": "audio/pcm"})
        _run(lv._send_realtime(), 0.2)
        assert session.sent, "nothing was sent to the session"
        blob = session.sent[0]["audio"]
        assert blob.data == b"pcm-bytes"
        assert blob.mime_type == "audio/pcm"


# ── the receive loop: what the model sends back ────────────────────────────
class Msg:
    """A LiveServerMessage carrying only what the loop reads."""

    def __init__(self, *, out=None, inp=None, data=None, turn_complete=False,
                 tool_call=None):
        self.session_resumption_update = None
        self.data = data
        self.tool_call = tool_call
        self.server_content = _ServerContent(out, inp, turn_complete)


class _ServerContent:
    def __init__(self, out, inp, turn_complete):
        self.output_transcription = _Text(out)
        self.input_transcription = _Text(inp)
        self.turn_complete = turn_complete


class _Text:
    def __init__(self, text): self.text = text


class ReceiveSession(FakeSession):
    """Yields the scripted messages, then stays open like a live socket."""

    def __init__(self, messages):
        super().__init__()
        self.messages = messages

    async def receive(self):
        for m in self.messages:
            yield m
        await asyncio.sleep(30)

    async def send_tool_response(self, **kw):
        self.sent.append(kw)


def _receive(lv, messages, seconds=0.3, monkeypatch=None):
    lv.session = ReceiveSession(messages)
    if monkeypatch is not None:
        from actions import history_search as hs
        recorded: list[tuple] = []
        monkeypatch.setattr(hs, "record",
                            lambda speaker, text: recorded.append((speaker, text)))
    else:
        recorded = []
    _run(lv._receive_audio(), seconds)
    return recorded


class TestTheReceiveLoop:
    def test_an_emotion_marker_is_stripped_before_the_reply_is_logged(
            self, live):
        """The order is the whole point. If the text is logged — and spoken, and
        stored, and shown on the phone — before the marker is removed, the
        transcript reads `[emotion: surprised] Sure thing`."""
        lv, ui, _fake = live
        # "surprised" is one of the five in core/emotion.EMOTIONS; an unknown
        # name tags as neutral and the marker still has to come out
        _receive(lv, [Msg(out="Sure [emotion: surprised] thing", turn_complete=True)])
        assert ui.emotions == ["surprised"]
        logged = [m for m in ui.logs if m.startswith("JARVIS")]
        assert logged and "[emotion" not in logged[0]
        assert logged[0].endswith(": Sure  thing")
        # tagged BEFORE logged: the avatar is set first, then the text appears
        assert [k for k, _v in ui.events][:2] == ["emotion", "log"]

    def test_a_neutral_reply_does_not_touch_the_avatar(self, live):
        lv, ui, _fake = live
        _receive(lv, [Msg(out="Two plus two is four.", turn_complete=True)])
        assert ui.emotions == []
        assert any("Two plus two" in m for m in ui.logs)

    def test_a_repeated_transcript_tail_is_logged_once(self, live):
        """Tool-using turns produce several turn_completes and the API re-sends
        the tail of the answer across them. The failure is visible: the same
        sentence twice in the transcript, mouthed twice by the avatar."""
        lv, ui, _fake = live
        _receive(lv, [
            Msg(out="Checking the weather in Jaipur", turn_complete=True),
            Msg(out="Checking the weather in Jaipur", turn_complete=True),
        ], seconds=0.4)
        replies = [m for m in ui.logs if m.startswith("JARVIS")]
        assert len(replies) == 1, replies

    def test_reply_audio_is_sliced_so_an_interrupt_is_heard_at_once(
            self, live):
        """One 4800-byte block is 100 ms; the pump can only stop between queue
        items, so the slice size is what bounds the interrupt."""
        lv, ui, _fake = live
        _receive(lv, [Msg(data=b"x" * 4800)], seconds=0.25)
        sizes = []
        while not lv.audio_in_queue.empty():
            sizes.append(len(lv.audio_in_queue.get_nowait()))
        assert sizes == [2400, 2400]

    def test_user_speech_reaches_the_screen_and_the_history(self, live,
                                                            monkeypatch):
        lv, ui, _fake = live
        recorded = _receive(lv, [Msg(inp="turn the lights off",
                                     turn_complete=True)], monkeypatch=monkeypatch)
        assert any(m == "You: turn the lights off" for m in ui.logs)
        assert ("user", "turn the lights off") in recorded

    def test_an_interrupted_turn_is_not_logged_at_all(self, live):
        """The user cut it off mid-sentence: half a reply in the transcript (and
        in the model's next context) is worse than none."""
        lv, ui, _fake = live
        lv._interrupted = True
        _receive(lv, [Msg(out="I was saying that the", turn_complete=True)])
        assert not [m for m in ui.logs if m.startswith("JARVIS")]

    def test_a_tool_call_is_run_and_answered(self, live, monkeypatch):
        """The receive loop is what turns a function call into a tool result the
        model can continue from; without the answer the turn hangs."""
        lv, ui, _fake = live
        calls = []

        async def fake_run_tool_calls(function_calls):
            calls.append(function_calls)
            return ["a response"]

        async def fake_flush():
            return False

        monkeypatch.setattr(lv, "_run_tool_calls", fake_run_tool_calls)
        monkeypatch.setattr(lv, "_flush_pending_vision", fake_flush)
        session = ReceiveSession([Msg(tool_call=type("TC", (), {
            "function_calls": ["fc"]})())])
        lv.session = session
        _run(lv._receive_audio(), 0.25)
        assert calls == [["fc"]]
        assert session.sent and session.sent[-1]["function_responses"] == ["a response"]


# ── the PCM helpers the loop is built on ───────────────────────────────────
class TestThePcmHelpers:
    def test_silence_is_not_a_waveform(self):
        assert _pcm_level(np.zeros(1024, dtype=np.int16)) == 0.0
        assert _pcm_level(np.array([], dtype=np.int16)) == 0.0

    def test_loud_audio_fills_the_waveform(self):
        assert _pcm_level(np.full(1024, 30000, dtype=np.int16)) == 1.0

    def test_a_level_is_never_nan_or_out_of_range(self):
        for block in (np.zeros(8, dtype=np.int16), np.full(8, 32767, dtype=np.int16),
                      np.array([1, -1, 1], dtype=np.int16)):
            assert 0.0 <= _pcm_level(block) <= 1.0

    def test_it_never_raises_on_rubbish(self):
        for bad in (None, "not pcm", [1, 2, 3], object()):
            assert _pcm_level(bad) == 0.0

    def test_the_control_markers_never_reach_the_screen(self):
        """`<ctrl42>` is the model's way of telling the UI to do something; it
        is stripped before any of it becomes a transcript."""
        assert _clean_transcript("hello <ctrl42> world") == "hello  world"
        assert _clean_transcript("\x07beep\x00") == "beep"

    def test_a_repeated_transcript_chunk_is_recognised(self):
        """Tool-using turns produce several turn_completes, and the API re-sends
        the tail of the answer across them; without this the transcript (and the
        log) shows the same sentence twice."""
        buf = ["the weather in Jaipur", "is hot today"]
        assert _is_repeat_chunk("is hot today", buf) is True
        assert _is_repeat_chunk("and humid", buf) is False
        # short interjections may legitimately repeat ("evet, evet")
        assert _is_repeat_chunk("ok", ["ok"]) is True
