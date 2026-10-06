"""The audio loop — microphone in, Gemini Live in the middle, speakers out.

Split out of `main.py` (PROJECT_ANALYSIS P2-15). This is the third of the
session class: the tools and the wake/relay parts left first, and this is the
one that actually moves sound.

It is a mixin, so the methods keep their exact `self.` contract — a move, not a
rewrite — and the audio format constants and the PCM helpers live in
`core/audio_pcm.py`, which both this module and `main.py` import. The loudness
and viseme work that used to be inlined here is now callable on its own, which
is what lets `tests/test_audio_loop.py` run the whole loop against a fake
sounddevice.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from datetime import datetime
from typing import Any

import numpy as np
from google.genai import types

from core import audio_devices
from core.audio_pcm import (
    CHANNELS,
    CHUNK_SIZE,
    RECEIVE_SAMPLE_RATE,
    SEND_SAMPLE_RATE,
    _CURSOR_SLACK,
    _FIRST_SOUND,
    _REPEAT_MIN,
    _TAIL_MARGIN,
    _VIS_HOP,
    _clean_transcript,
    _is_repeat_chunk,
    _pcm_level,
    _pcm_visemes,
    _sd,
)
from core.logging_setup import get_logger
from memory.config_manager import get_input_device, get_output_device

log = get_logger("jarvis.main")  # one session, one log stream


class AudioLoopMixin:
    """Mic capture, the Live session's audio in both directions, and playback."""

    # The session state these methods use. Annotations only — the values come
    # from `JarvisLive.__init__` — but they are what lets this module be
    # type-checked on its own.
    ui: Any = None
    session: Any = None
    audio_in_queue: Any = None
    out_queue: Any = None
    _echo: Any = None
    _viseme: Any = None
    _out_latency: float = 0.05
    _play_cursor: float = 0.0
    _last_out_logged: list = []
    _is_speaking: bool = False
    is_sleeping: Any = None
    wake: Any = None
    interrupt: Any = None
    set_speaking: Any = None
    _flush_pending_vision_sync: Any = None
    _pending_vision: Any = None                # the frame waiting for a turn end
    _vision_cam_active: bool = False
    _wake_enabled: bool = False
    _awake: bool = True

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            # Gemini 3.x Live rejects the old realtime_input.media_chunks field
            # (what `media=...` maps to) and closes the socket with a 1007. Send
            # mic / phone PCM through the new `audio` field instead. Queue items
            # are {"data": <bytes>, "mime_type": <str>} from _listen_audio and
            # the phone relay.
            await self.session.send_realtime_input(
                audio=types.Blob(
                    data=msg["data"],
                    mime_type=msg.get("mime_type", "audio/pcm"),
                )
            )

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_running_loop()

        def callback(indata, frames, time_info, status):
            # ── Wake-word gate ───────────────────────────────────────────────
            # While asleep, the mic audio NEVER goes to Gemini (nothing is
            # streamed, so JARVIS can't respond to speech not addressed to it and
            # nothing leaves the machine). Frames are instead handed to the local
            # detector, which runs its model in ITS OWN thread — the cost here is
            # only a queue push, so the audio path is never slowed. When wake word
            # is off (default) or we're awake, this is a single boolean check.
            if self._wake_enabled and not self._awake:
                det = self._wake_detector
                if det is not None:
                    det.feed(indata)
                return
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking

            # ── Barge-in ─────────────────────────────────────────────────────
            # While JARVIS talks the mic is not streamed, but it is still worth
            # listening to locally: if the user starts speaking, cut the answer
            # short the way a person would stop when interrupted.
            #
            # The whole difficulty is echo — on speakers the mic hears JARVIS.
            # So the test is not "is the mic loud" but "is the mic louder than
            # the echo of what we are playing right now", sustained long enough
            # that a cough or a keystroke cannot trigger it.
            if jarvis_speaking:
                # Nothing is streamed while JARVIS talks.
                #
                # Auto barge-in: classify the block with EchoGuard (content
                # subtraction — survives our own echo) and interrupt() only
                # after `required_blocks` of consecutive agreement, so a
                # cough/TV word can't stop the reply. Gated by the
                # "barge_in" config flag (default on, cached per reply in
                # set_speaking — no disk reads in the audio callback);
                # disabled → old behaviour (listen to the end). Any
                # classifier doubt → no interrupt (should_interrupt's
                # contract).
                if self._barge_on:
                    try:
                        if self._echo.should_interrupt(
                            indata, SEND_SAMPLE_RATE, _pcm_level(indata)
                        ):
                            self._echo.note_interrupted()
                            self.interrupt()
                    except Exception:
                        pass  # audio callback must never raise
                return

            # ── Echo tail ────────────────────────────────────────────────────
            # The speaking flag has dropped but the speakers have not finished.
            # Sending this to the model is how an assistant hears itself, decides
            # it was addressed, and answers its own last sentence. The microphone
            # stays OPEN — the guard only drops blocks that are our own voice, so
            # replying the instant it stops still works.
            if self._tail_active():
                try:
                    if not self._echo.is_user_speech(indata, SEND_SAMPLE_RATE, _pcm_level(indata)):
                        return
                    self._tail_until = 0.0  # a real voice ends the tail early
                except Exception:
                    return
            elif self._echo._hist:
                self._echo.reset()

            # ── Push-to-talk ─────────────────────────────────────────────────
            # When it is on the microphone is closed by default and the chord
            # opens it, which is the whole point: nothing leaves the machine
            # unless you are holding the key.
            if self._ptt_enabled and not self._ptt_held:
                return

            if not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait, {"data": data, "mime_type": "audio/pcm"}
                )
                # Feed the live mic level to the HUD so the waveform reacts to
                # the user's actual voice while listening. Purely cosmetic — any
                # failure here must never disturb the mic.
                try:
                    self.ui.set_audio_level(_pcm_level(indata))
                except Exception:
                    pass

        try:

            def _open_mic(dev):
                return _sd().InputStream(
                    samplerate=SEND_SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    device=dev,
                    callback=callback,
                )

            # Which microphone. resolve() returns None for "system default" and
            # for a saved device that is no longer present — so a headset
            # unplugged since the last run falls back to the built-in mic
            # instead of raising on startup and taking the session with it.
            _mic_name = get_input_device()
            _mic_dev = audio_devices.resolve(_mic_name, "input")
            if _mic_dev is not None:
                print(f"[JARVIS] 🎤 Input device: {_mic_name}")
            try:
                _mic_stream = _open_mic(_mic_dev)
            except Exception as _e:
                # A device the picker listed but the driver will not open right
                # now — exclusive mode, a webcam already in use, a virtual mic
                # whose source went away. Chosen hardware failing must never
                # mean the assistant cannot hear at all.
                if _mic_dev is None:
                    raise
                print(f"[JARVIS] ⚠️  Mic '{_mic_name}' failed: {_e} — using default")
                self.ui.write_log(
                    f"SYS: Microphone '{_mic_name}' unavailable — using system default."
                )
                _mic_stream = _open_mic(None)

            with _mic_stream:
                print("[JARVIS] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[JARVIS] ❌ Mic: {e}")
            raise

    async def _flush_pending_vision(self) -> bool:
        """Send a captured frame immediately after its tool response.

        The frame is already in hand by the time `screen_process` returns — the
        capture happened inside the tool call. The old flow still made the model
        speak a turn first and only injected the image on that turn's
        turn_complete, which cost a whole extra round trip AND produced two
        spoken answers: one improvised without the picture, then the real one.
        Sending it here means the model has the tool result and the image before
        it generates anything, so the user gets one answer, sooner.
        """
        if not (self._pending_vision and self.session):
            return False

        import base64 as _b64

        img_b, mime_t, question, angle = self._pending_vision
        self._pending_vision = None
        b64 = _b64.b64encode(img_b).decode("ascii")
        print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

        # Label the source. Without it the image arrives carrying nothing but
        # the user's own sentence, and a screenshot of this app — which has a
        # face in the middle of it — got read as a photo of the user. What the
        # label *means* is explained once, in the generated [SELF] block.
        src = "[IMAGE SOURCE: WEBCAM]" if angle == "camera" else "[IMAGE SOURCE: SCREEN CAPTURE]"
        await self.session.send_client_content(
            turns={
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime_t, "data": b64}},
                    {"text": f"{src}\n\n{question}"},
                ],
            },
            turn_complete=True,
        )

        if self._vision_cam_active:
            # Camera: stay busy until JARVIS has finished speaking the answer,
            # then close the preview.
            self._vision_cam_active = False
            self._vision_close_pending = True
        else:
            self._vision_busy = False
        return True

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():
                    # ── Session resumption ───────────────────────────────────
                    # The server sends this periodically. `resumable` goes false
                    # while a turn is mid-flight — replaying a handle from that
                    # moment is what the flag exists to prevent — so only
                    # resumable handles are kept. This is three lines and it is
                    # the entire fix for "every reconnect forgets everything".
                    _sru = getattr(response, "session_resumption_update", None)
                    if _sru is not None:
                        if getattr(_sru, "resumable", False) and getattr(_sru, "new_handle", None):
                            if self._resume_handle is None:
                                print("[JARVIS] 🔗 Session resumption armed")
                            self._resume_handle = _sru.new_handle

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            # A turn that involves a tool call passes through
                            # several turn_completes, and the API re-sends the
                            # tail of the transcript across them. Comparing only
                            # against the previous chunk missed that — once
                            # out_buf had been flushed and emptied, the repeat
                            # sailed straight back in, which logged the answer
                            # twice AND made the avatar mouth it twice.
                            if txt and not _is_repeat_chunk(txt, out_buf):
                                out_buf.append(txt)
                                # Hand the words to the mouth as they arrive, so
                                # the avatar can form the consonants the audio
                                # alone cannot show. Pure string work — it adds
                                # nothing measurable to the response path.
                                self._visemes.feed_text(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf = []
                                out_buf = []
                                self._visemes.reset()
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self._last_out_logged = ""  # new exchange
                                self.ui.write_log(f"You: {full_in}")
                                # Dictation mode (Report J): type the
                                # user's spoken words too — no-op when off.
                                try:
                                    from actions import dictation as _dict

                                    _dict.feed(full_in)
                                except Exception:
                                    pass
                                self._session_log.append(f"User: {full_in}")
                                # Durable history: survives restarts (the
                                # session log above is RAM-only).
                                try:
                                    from actions import history_search as _hs

                                    _hs.record("user", full_in)
                                except Exception:
                                    pass
                                # Phrase rules must fire for SPOKEN input too —
                                # 'when I say movie mode' is a voice feature.
                                self._fire_phrase_rules(full_in)
                                if self._dashboard:
                                    asyncio.create_task(
                                        self._dashboard.broadcast(
                                            {
                                                "type": "log",
                                                "speaker": "user",
                                                "text": full_in,
                                                "ts": datetime.now().isoformat(),
                                            }
                                        )
                                    )
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            # Second line of defence: even if a repeat slips
                            # into a *fresh* buffer after a flush, never log the
                            # same answer (or a tail of it) twice in a row.
                            if full_out and len(full_out) >= _REPEAT_MIN and self._last_out_logged:
                                if full_out in self._last_out_logged:
                                    full_out = ""
                            if full_out:
                                # Emotion-tag acting (R2): tag BEFORE logging
                                # so markers never reach TTS/history/HUD text.
                                try:
                                    from core import emotion as _emo_mod

                                    _e, full_out = _emo_mod.tag_and_clean(full_out)
                                    if _e != "neutral":
                                        self.ui.set_emotion(_e)
                                except Exception:
                                    pass
                                self._last_out_logged = full_out
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                try:
                                    from actions import history_search as _hs

                                    _hs.record(self._asst_name, full_out)
                                except Exception as e:
                                    # Conversation history is a searchable
                                    # feature, not a gate: a failure to record
                                    # one turn must not break the session, but
                                    # it does mean `history` will not find it.
                                    log.warning(f"history record failed: {e}")
                                if self._dashboard:
                                    asyncio.create_task(
                                        self._dashboard.broadcast(
                                            {
                                                "type": "log",
                                                "speaker": "jarvis",
                                                "text": full_out,
                                                "ts": datetime.now().isoformat(),
                                            }
                                        )
                                    )
                            out_buf = []

                            if self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False

                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()

                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = await self._run_tool_calls(response.tool_call.function_calls)
                        await self.session.send_tool_response(function_responses=fn_responses)
                        await self._flush_pending_vision()
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        _spk_name = get_output_device()
        _spk_dev = audio_devices.resolve(_spk_name, "output")
        if _spk_dev is not None:
            print(f"[JARVIS] 🔊 Output device: {_spk_name}")

        def _open_spk(dev):
            st = _sd().RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                device=dev,
            )
            st.start()
            return st

        try:
            stream = _open_spk(_spk_dev)
        except Exception as _e:
            # A chosen output that the host API accepts by name but refuses to
            # open (exclusive mode, wrong sample rate, device asleep) must not
            # cost the user their voice. Fall back to the default and say so.
            if _spk_dev is None:
                raise
            print(f"[JARVIS] ⚠️  Output device '{_spk_name}' failed: {_e} — using default")
            self.ui.write_log(f"SYS: Speaker '{_spk_name}' unavailable — using system default.")
            stream = _open_spk(None)

        # Ask the device how far behind the speakers actually are, rather than
        # assuming. This is what the echo tail is sized from, so a machine with a
        # large audio buffer gets a correspondingly longer guard — and one with a
        # tiny buffer is not penalised with a delay it does not need.
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self._out_latency = lat
            print(
                f"[JARVIS] 🔊 Output latency {self._out_latency * 1000:.0f} ms "
                f"→ echo tail {(self._out_latency + _TAIL_MARGIN) * 1000:.0f} ms"
            )
        except Exception:
            # A stream that does not report its latency is not a failure —
            # the default tail is used and the line above simply does not
            # print. Deliberate silence.
            pass

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(self.audio_in_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:  # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Drive the HUD waveform and the avatar's mouth from JARVIS's
                # own voice. The batch is up to 200 ms long, so we hand over a
                # *schedule* of 20 ms viseme frames instead of a single averaged
                # level and let the HUD play it out in step with the audio.
                try:
                    pcm = np.frombuffer(bytes(batch), dtype=np.int16)
                    hop = _VIS_HOP / RECEIVE_SAMPLE_RATE
                    frames = _pcm_visemes(pcm, sr=RECEIVE_SAMPLE_RATE)
                    # When does this batch become audible? The stream was
                    # started at launch and its callback has been pulling
                    # silence ever since, so the first bytes of a reply reach
                    # the speaker about one callback period later — NOT one
                    # buffer later. `stream.latency` reports the buffer's
                    # capacity, which is how much can be queued ahead, and on
                    # Windows that is commonly 300-500 ms. Anchoring on it put
                    # the entire schedule a buffer late; that is the half second
                    # of lag, and it grew with whatever the device reported.
                    #
                    # After the anchor nothing needs measuring: the device
                    # consumes at exactly realtime, so each batch sounds one
                    # batch-duration after the one before it. The cursor is
                    # re-anchored only when it leaves the range physically
                    # possible — behind `now` means the device drained and this
                    # batch starts a fresh stretch of speech, while further
                    # ahead than the buffer can hold means it has drifted.
                    now = time.time()
                    horizon = self._out_latency + _CURSOR_SLACK
                    if not (now <= self._play_cursor <= now + horizon):
                        self._play_cursor = now + _FIRST_SOUND
                    at = self._play_cursor
                    # Advance by the batch's own duration whether or not it
                    # yielded frames, so a block too short to analyse cannot
                    # shift everything after it out of step with the audio.
                    self._play_cursor += pcm.size / RECEIVE_SAMPLE_RATE
                    if frames:
                        frames = self._visemes.frames(frames, hop)
                        self.ui.push_visemes(frames, hop, at)
                        # Barge-in needs to know what we are playing, not just
                        # how loud: the guard subtracts this from the microphone.
                        self._out_level = max(f[0] for f in frames)
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, self._out_level)
                    else:
                        lvl = _pcm_level(pcm)
                        self.ui.set_audio_level(lvl)
                        self._out_level = lvl
                        self._echo.note_output(pcm, RECEIVE_SAMPLE_RATE, lvl)
                except Exception:
                    # Level metering and echo calibration only: the audio is
                    # written on the very next line regardless, so a failure
                    # here costs a moving mouth, not the voice. Deliberate.
                    pass

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break  # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()
