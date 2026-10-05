"""
dictation — continuous speech-to-text typing mode (Report J).

Two honest sources, one destination layer:

  * OFFLINE segments (default): a record→transcribe→append loop built on
    the same primitives meeting.py uses (sounddevice capture, 16 kHz,
    core.stt faster-whisper/Vosk — fully local, zero API).
  * LIVE feed: while the Gemini session hears you, main.py hands each
    final input transcript to `dictation.feed()` so your spoken words are
    also TYPED into the destination (clipboard / file / HUD panel).

Destinations: `clipboard` (pyperclip via clip_history's setter),
`file` (append, path required), `hud` (content panel).

HONEST SCOPE: with the live session connected, JARVIS still hears you —
this types your words, it does not mute the model. For pure typing use
offline mode (session idle) or stop the session first.

WHY NOT A DUPLICATE: meeting.py records a bounded meeting to disk; this
is an always-on typing layer with destinations. STT is REUSED (core.stt),
never rebuilt.
"""
from __future__ import annotations

import threading

_ON = False
_DEST = "clipboard"
_FILE = ""
_PLAYER = None
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_STATE = {"typed": 0, "chars": 0, "last": "", "error": ""}
_SEG_S = 5.0


# ── seams (tests replace these) ─────────────────────────────────────────────

def _record_segment(seconds: float) -> object:
    """One mic window → float32 mono 16 kHz numpy (meeting's primitives)."""
    import numpy as np
    import sounddevice as sd
    try:
        data = sd.rec(int(seconds * 16000), samplerate=16000, channels=1,
                      dtype="float32")
        sd.wait()
        return np.asarray(data, dtype="float32").reshape(-1)
    except Exception as e:
        raise RuntimeError(f"mic unavailable ({e})") from e


def _transcribe(audio) -> str:
    """Offline STT — core.stt's engines (faster-whisper → Vosk)."""
    from core import stt as stt_mod
    engine = stt_mod.WhisperSTT()
    return (engine.transcribe(audio) or "").strip()


def _clip_set(text: str) -> bool:
    from actions import clip_history as ch
    return ch._clip_set(text)


# ── delivery ────────────────────────────────────────────────────────────────

def feed(text: str) -> bool:
    """Type one transcript chunk (live-session path calls this)."""
    text = (text or "").strip()
    if not text or not _ON:
        return False
    if _DEST == "clipboard":
        ok = _clip_set(text)
        if not ok:
            _STATE["error"] = ("clipboard unavailable (headless?) — "
                               "use dest=file instead")
            return False
    elif _DEST == "file":
        try:
            with open(_FILE, "a", encoding="utf-8") as fh:
                fh.write(text.rstrip("\n") + "\n")
        except Exception as e:
            _STATE["error"] = str(e)[:120]
            return False
    elif _DEST == "hud":
        p = _PLAYER
        if p is not None:
            try:
                p.show_content("DICTATION", text[:4000])
            except Exception:
                pass
    _STATE["typed"] += 1
    _STATE["chars"] += len(text)
    _STATE["last"] = text
    return True


def _tick(seconds: float = _SEG_S) -> None:
    """One offline cycle: record → transcribe → deliver. Never raises."""
    try:
        audio = _record_segment(seconds)
    except Exception as e:
        _STATE["error"] = str(e)[:160]
        return
    try:
        text = _transcribe(audio) if audio is not None else ""
    except Exception as e:
        _STATE["error"] = f"stt: {e}"[:160]
        return
    if text:
        feed(text)


def _loop() -> None:
    while not _STOP.is_set():
        _tick()
        # brief pause so stop() lands promptly between segments
        _STOP.wait(0.2)


# ── tool ────────────────────────────────────────────────────────────────────

def dictation(parameters: dict = None, player=None,
              session_memory=None) -> str:
    global _ON, _DEST, _FILE, _PLAYER, _THREAD
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()
    dest = str(params.get("dest", "") or "").lower().strip()
    path = str(params.get("path", "") or "").strip()
    if player is not None:
        _PLAYER = player

    if action in ("on", "start", "begin"):
        new_dest = dest or _DEST
        if new_dest not in ("clipboard", "file", "hud"):
            return "dest must be clipboard | file | hud."
        if new_dest == "file" and not path and not _FILE:
            return "dest=file needs a path (dictation path=notes/dict.txt)."
        _DEST = new_dest
        if path:
            _FILE = path
        if _ON:
            return f"Dictation already on (dest={_DEST})."
        _STOP.clear()
        _ON = True
        mode = params.get("mode", "offline")
        if str(mode).lower() in ("live", "session"):
            return (f"Dictation ON (live feed) → {_DEST}"
                    + (f" :: {_FILE}" if _DEST == "file" else "") +
                    ". Your spoken transcripts are being typed.")
        _THREAD = threading.Thread(target=_loop, daemon=True,
                                   name="dictation")
        _THREAD.start()
        return (f"Dictation ON (offline segments) → {_DEST}"
                + (f" :: {_FILE}" if _DEST == "file" else "") +
                ". Keep talking; text lands as you speak. Say 'stop "
                "dictation' when done.")

    if action in ("off", "stop"):
        was = _ON
        _ON = False
        _STOP.set()
        _THREAD = None
        if not was:
            return "Dictation was not running."
        return (f"Dictation OFF — typed {_STATE['typed']} chunk(s), "
                f"{_STATE['chars']} char(s).")

    # status
    state = "ON" if _ON else "off"
    out = (f"Dictation: {state}, dest={_DEST}"
           + (f", file={_FILE}" if _DEST == "file" else "")
           + f" — typed {_STATE['typed']} chunk(s), "
           f"{_STATE['chars']} char(s).")
    if _STATE["last"]:
        out += f" Last: {_STATE['last'][:80]}"
    if _STATE["error"]:
        out += f" ⚠ {_STATE['error']}"
    if not _ON:
        out += " Start: dictation action=on [dest=clipboard|file|hud] " \
               "[mode=offline|live]."
    return out


TOOL = {
    "name": "dictation",
    "description": (
        "Speech-to-text TYPING mode: offline record→transcribe→append "
        "segments (core.stt, fully local) or live session transcripts "
        "mode=live, delivered to dest=clipboard | file (path) | hud. "
        "Actions: on (start), off (stop), status (default). Honest: with "
        "the session connected JARVIS still hears you — this types your "
        "words, it does not mute the model. Use for 'type what I say', "
        "'dictation on', 'likhna shuru karo bol ke'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "on | off | status."},
            "dest": {"type": "STRING",
                     "description": "clipboard | file | hud."},
            "path": {"type": "STRING",
                     "description": "File to append when dest=file."},
            "mode": {"type": "STRING",
                     "description": "offline (default) | live (session "
                                    "transcripts only)."},
        },
        "required": [],
    },
    "handler": dictation,
}
