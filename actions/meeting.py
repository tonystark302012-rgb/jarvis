# actions/meeting.py
"""meeting — record a conversation on the mic, get a transcript back.

WHAT IT DOES
    start   — opens a 16 kHz mono mic stream (sounddevice) and buffers it
    stop    — closes the stream, writes <base>/meetings/<ts>.wav, runs the
              offline STT engine (core.stt — Whisper or Vosk) and saves a
              .txt transcript next to it; returns the head of the transcript
    status  — is it recording, for how long
    list    — past transcripts in meetings/
    summary — pull one transcript through Gemini for decisions/action items

WHY NOT A DUPLICATE
    core/stt is the engine, not the product — nothing called it end-to-end.
    This is the whole loop: record → file → transcript → summary, with the
    seams (recorder, transcriber) injectable so tests never touch a mic.

FREE TOOLS ONLY: sounddevice (mic), stdlib wave (file), faster-whisper /
vosk (offline STT), Gemini (optional summary — degrades to raw transcript).
"""
from __future__ import annotations

import json
import re
import threading
import time
import wave
from pathlib import Path

_SAMPLE_RATE = 16_000        # what WhisperSTT.transcribe expects
_MAX_SECS = 3600             # hard cap — a forgotten meeting can't eat disk


# ── recorder ─────────────────────────────────────────────────────────────────

class _Recorder:
    """Mic → float32 mono buffer. sounddevice only appears inside start(),
    so importing or constructing this class is safe everywhere."""

    def __init__(self) -> None:
        self._stream = None
        self._chunks: list = []
        self._lock = threading.Lock()
        self._started = 0.0
        self._src_rate = _SAMPLE_RATE
        self._auto_stop: threading.Timer | None = None

    @property
    def running(self) -> bool:
        return self._stream is not None

    @property
    def seconds(self) -> float:
        if not self.running:
            return 0.0
        return time.time() - self._started

    def start(self) -> None:
        if self.running:
            raise RuntimeError("already recording")
        import numpy as np
        import sounddevice as sd

        def _cb(indata, frames, t_info, status):     # noqa: ANN001
            with self._lock:
                self._chunks.append(np.asarray(indata, dtype=np.float32).copy())

        # 16 kHz if the device takes it, else its native rate (resampled on stop)
        try:
            stream = sd.InputStream(samplerate=_SAMPLE_RATE, channels=1,
                                    dtype="float32", callback=_cb)
            stream.start()
            self._src_rate = _SAMPLE_RATE
        except Exception:
            dev = sd.query_devices(kind="input")
            rate = int(dev.get("default_samplerate") or 44_100)
            stream = sd.InputStream(samplerate=rate, channels=1,
                                    dtype="float32", callback=_cb)
            stream.start()
            self._src_rate = rate
        self._stream = stream
        self._started = time.time()
        with self._lock:
            self._chunks.clear()
        self._auto_stop = threading.Timer(_MAX_SECS, self._force_stop)
        self._auto_stop.daemon = True
        self._auto_stop.start()

    def _force_stop(self) -> None:
        try:
            self.stop()
            print("[Meeting] auto-stopped at the 60-minute cap.")
        except Exception:
            pass

    def stop(self):
        """Closes the stream; returns float32 mono numpy at 16 kHz."""
        if not self.running:
            raise RuntimeError("not recording")
        if self._auto_stop is not None:
            self._auto_stop.cancel()
            self._auto_stop = None
        stream, self._stream = self._stream, None
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass
        with self._lock:
            chunks, self._chunks = self._chunks, []
        import numpy as np
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks, axis=0)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)               # downmix → mono
        if self._src_rate != _SAMPLE_RATE and audio.size:
            # linear resample — voice-grade is enough for whisper input
            n_out = int(audio.size * _SAMPLE_RATE / self._src_rate)
            x_old = np.linspace(0.0, 1.0, num=audio.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)
        return audio.astype(np.float32)


_RECORDER = _Recorder()       # module singleton — injected in tests


# ── seams ────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _meetings_dir() -> Path:
    d = _base_dir() / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _transcribe(audio) -> str:
    """Offline STT through core.stt — Whisper preferred, honest when the
    engine isn't installed (audio is still saved either way)."""
    import numpy as np
    if audio is None or getattr(audio, "size", 0) == 0:
        return ""
    from core import stt as stt_mod
    try:
        engine = stt_mod.WhisperSTT()
    except Exception as e:
        raise RuntimeError(
            f"offline STT unavailable ({e}) — install faster-whisper, "
            f"the WAV is saved either way") from e
    return str(engine.transcribe(np.asarray(audio, dtype=np.float32)) or "")


def _save_wav(audio, path: Path) -> None:
    import numpy as np
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
           * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


def _summarize(transcript: str, which: str) -> str:
    from core import gemini
    if not gemini.api_key():
        return ""                                   # caller falls back
    resp = gemini.call(
        ["Summarise this meeting transcript for the participant: decisions, "
         "action items (who/what/when if stated), open questions. Markdown, "
         "under 200 words.\n\nTRANSCRIPT:\n" + transcript[:12000]],
        tier=gemini.SMART, timeout_ms=45_000)
    if resp is None:
        return ""
    try:
        return "".join(p.text for p in resp.candidates[0].content.parts
                       if getattr(p, "text", None)).strip()
    except Exception:
        return ""


# ── handler ──────────────────────────────────────────────────────────────────

def _extract_action_items(summary: str) -> list[dict]:
    """Summary markdown → [{task, date, time}] with CONCRETE dates.

    Seamed for tests. Returns [] without a key — the caller then just
    reports that nothing was pushed (honest, no fake scheduling)."""
    from core import gemini
    if not gemini.api_key():
        return []
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    resp = gemini.call(
        ["Extract action items with deadlines from this meeting summary. "
         "Return ONLY a JSON array, no prose, no fences. Each element: "
         '{"task": str, "date": "YYYY-MM-DD", "time": "HH:MM"}. '
         f"Use today={today} to resolve words like 'tomorrow', 'Friday'. "
         "When no deadline is stated, use date=\"\" and time=\"\". "
         "Max 8 items. Items without deadlines are fine — keep them.\n\n"
         f"SUMMARY:\n{summary[:6000]}"],
        tier=gemini.FAST, timeout_ms=30_000)
    if resp is None:
        return []
    try:
        raw = "".join(p.text for p in resp.candidates[0].content.parts
                      if getattr(p, "text", None)).strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
        data = json.loads(raw)
        out = []
        for it in (data if isinstance(data, list) else [])[:8]:
            if isinstance(it, dict) and str(it.get("task", "")).strip():
                out.append({"task": str(it["task"]).strip()[:200],
                            "date": str(it.get("date") or "").strip(),
                            "time": str(it.get("time") or "").strip()})
        return out
    except Exception as e:
        print(f"[Meeting] action-item parse failed: {e}")
        return []


def _push_action_items(summary: str) -> str:
    """Create reminders for dated action items; undated ones come back as
    a checklist. Never raises — a scheduling failure is reported, not
    swallowed silently."""
    items = _extract_action_items(summary)
    if not items:
        return ("Push: no action items extracted (no Gemini key or nothing "
                "found) — nothing was scheduled.")
    from actions import reminder as _rem
    scheduled: list[str] = []
    unscheduled: list[str] = []
    failed: list[str] = []
    for it in items:
        if it["date"] and it["time"]:
            try:
                out = _rem.reminder({"date": it["date"],
                                     "time": it["time"],
                                     "message": f"Meeting follow-up: "
                                                f"{it['task']}"})
            except Exception as e:
                failed.append(f"{it['task']} ({e})")
                continue
            if out and "Reminder set" in out:
                scheduled.append(f"{it['task']} — {it['date']} "
                                 f"{it['time']}")
            else:
                failed.append(f"{it['task']} ({str(out)[:80]})")
        else:
            unscheduled.append(it["task"])
    lines = [f"Pushed {len(scheduled)} reminder(s) from the meeting."]
    if scheduled:
        lines.append("Scheduled:\n" + "\n".join(f"  ✓ {s}" for s in scheduled))
    if unscheduled:
        lines.append("No deadline stated (say when and I'll schedule):\n"
                     + "\n".join(f"  • {t}" for t in unscheduled))
    if failed:
        lines.append("Could not schedule:\n"
                     + "\n".join(f"  ✗ {f}" for f in failed))
    return "\n".join(lines)


def _latest_transcript() -> Path | None:
    files = sorted(_meetings_dir().glob("*.txt"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def meeting(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "status").lower().strip()

    if action == "start":
        if _RECORDER.running:
            return (f"Already recording — {int(_RECORDER.seconds)}s in. "
                    "Say stop when the meeting ends.")
        try:
            _RECORDER.start()
        except RuntimeError as e:
            return f"Meeting recorder: {e}"
        except Exception as e:
            return (f"Couldn't open the microphone: {e}. Check the mic "
                    "picker in settings if one is selected.")
        return ("Meeting recording started — say 'meeting stop' when it's "
                "done and I'll transcribe it.")

    if action == "stop":
        if not _RECORDER.running:
            return "No meeting is being recorded."
        try:
            audio = _RECORDER.stop()
        except Exception as e:
            return f"Couldn't stop the recorder: {e}"
        ts = time.strftime("%Y%m%d-%H%M%S")
        wav = _meetings_dir() / f"meeting-{ts}.wav"
        txt = _meetings_dir() / f"meeting-{ts}.txt"
        try:
            _save_wav(audio, wav)
        except Exception as e:
            return f"Recording stopped but the WAV couldn't be saved: {e}"
        dur = int(audio.size / _SAMPLE_RATE) if getattr(audio, "size", 0) else 0
        if dur < 1:
            return f"Meeting saved ({wav.name}) but the mic captured nothing."
        try:
            transcript = _transcribe(audio)
        except RuntimeError as e:
            return (f"Meeting saved: {wav.name} ({dur}s). "
                    f"Transcription skipped — {e}.")
        except Exception as e:
            return f"Meeting saved: {wav.name} ({dur}s) but transcription failed: {e}"
        if not transcript.strip():
            return (f"Meeting saved ({wav.name}, {dur}s) — the engine heard "
                    "no speech.")
        try:
            txt.write_text(transcript, encoding="utf-8")
        except Exception:
            pass
        head = transcript.strip()
        return (f"Meeting {ts} recorded {dur}s. Transcript: "
                f"{txt.name}\n{head[:700]}" +
                ("…" if len(head) > 700 else ""))

    if action == "status":
        if _RECORDER.running:
            return f"Recording — {int(_RECORDER.seconds)}s so far."
        return "Not recording. Say 'meeting start' to begin."

    if action == "list":
        files = sorted(_meetings_dir().glob("*.txt"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        if not files:
            return "No meeting transcripts yet."
        lines = [f"{i + 1}. {p.name} ({p.stat().st_size} B, "
                 f"{time.strftime('%d %b %H:%M', time.localtime(p.stat().st_mtime))})"
                 for i, p in enumerate(files)]
        return "Meetings:\n" + "\n".join(lines)

    if action == "summary":
        which = str(params.get("which") or "latest").strip().lower()
        path = _latest_transcript()
        if which.isdigit():
            files = sorted(_meetings_dir().glob("*.txt"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            if which.isdigit() and 1 <= int(which) <= len(files):
                path = files[int(which) - 1]
        if path is None:
            return "No transcript to summarise yet."
        transcript = path.read_text(encoding="utf-8", errors="replace")
        summary = _summarize(transcript, which)
        if not summary:
            return (f"No Gemini key — here is the raw transcript "
                    f"({path.name}):\n{transcript[:700]}")
        reply = f"Summary of {path.name}:\n{summary}"
        # Roadmap: meeting → reminder auto-push. opt-in so a summary
        # never schedules things behind the user's back.
        push = str(params.get("push") or "").lower() in ("1", "true", "yes",
                                                         "reminders")
        if push:
            reply += "\n\n" + _push_action_items(summary)
        return reply

    return f"Unknown meeting action {action!r} — use start|stop|status|list|summary."


TOOL = {
    "name": "meeting",
    "description": (
        "Record a meeting on the microphone, transcribe it offline "
        "(faster-whisper) and optionally summarise it. Actions: start "
        "(begin recording), stop (end + save WAV + transcript), status "
        "(recording? for how long), list (past transcripts), summary "
        "(decisions/action items via Gemini). Use for 'record this "
        "meeting', 'start taking notes', 'summarise our last meeting'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "start | stop | status | list | summary"},
            "which": {"type": "STRING",
                      "description": "Transcript number for summary — default latest"},
            "push": {"type": "STRING",
                     "description": "yes — after summary, create reminders "
                                    "for dated action items (undated ones "
                                    "are listed, not guessed)"},
        },
        "required": [],
    },
    "handler": meeting,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return meeting(params,
                   player=(ctx or {}).get("player"),
                   session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(meeting({"action": "status"}))
