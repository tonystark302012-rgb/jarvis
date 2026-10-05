"""
video_qa — captions + question-answering over video files (Report R2).

Pipeline (all local):
  1. audio  — ffmpeg extracts mono 16 kHz wav (system ffmpeg, else the
              bundled imageio-ffmpeg binary — both free). Honest refuse
              when neither exists.
  2. captions — core.stt faster-whisper segments → <video>.srt + .txt
              (transcribe_segments gives real start/end times).
  3. ask     — question + transcript through the free Gemini ladder;
              WITHOUT a key → honest keyword-window of the transcript
              (labeled as context, never as an answer).

WHY NOT A DUPLICATE: dots call captions are live-voice subtitles; this
is offline FILE video (recordings, lectures) → SRT + Q&A.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import numpy as np


_LAST_NOTE = ""


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _ffmpeg_exe() -> str | None:
    """System ffmpeg first, then the pip-bundled static binary."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg  # free static ffmpeg wheel
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _extract_audio(video: str, timeout: int = 600) -> np.ndarray:
    """ffmpeg → float32 mono 16 kHz. Raises with the honest reason."""
    exe = _ffmpeg_exe()
    if not exe:
        raise RuntimeError(
            "ffmpeg not found — install it free (apt install ffmpeg) "
            "or pip install imageio-ffmpeg, then retry.")
    out = Path(video).with_suffix(".jarvis-audio.wav")
    proc = subprocess.run(
        [exe, "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", str(out)],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or "")[-300:]
        raise RuntimeError(f"audio extraction failed: {tail}")
    try:
        import wave
        with wave.open(str(out), "rb") as w:
            frames = w.readframes(w.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
        audio /= 32768.0
        return audio
    finally:
        try:
            out.unlink()
        except Exception:
            pass


def _srt_ts(seconds: float) -> str:
    ms = int(max(0.0, seconds) * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(segments: list[dict], path: Path) -> Path:
    lines = []
    for i, seg in enumerate(segments, 1):
        lines += [str(i),
                  f"{_srt_ts(seg['start'])} --> {_srt_ts(seg['end'])}",
                  seg["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _transcribe_segments(audio: np.ndarray,
                         engine: str = "whisper") -> tuple[list[dict], str]:
    """→ (segments, note). engine=whisperx uses the optional WhisperX
    (word/diarization) and FALLS BACK to faster-whisper with an honest
    note when the library or token is missing — never silent, never
    fake speakers."""
    if engine == "whisperx":
        try:
            from core.stt import WhisperXSTT
            wx = WhisperXSTT()
            segs = wx.transcribe_diarized(audio)
            out = []
            for s in segs:
                txt = s["text"]
                if s.get("diarized") and s.get("speaker"):
                    txt = f"[{s['speaker']}] {txt}"
                out.append({"start": s["start"], "end": s["end"],
                            "text": txt})
            return out, ("whisperx" + ("" if any(
                s.get("diarized") for s in segs) else
                " (no HF_TOKEN — single speaker, diarization skipped)"))
        except Exception as e:
            from core.stt import WhisperSTT
            return WhisperSTT().transcribe_segments(audio), (
                f"whisperx unavailable ({str(e)[:80]}…), "
                "used faster-whisper instead")
    from core.stt import WhisperSTT
    return WhisperSTT().transcribe_segments(audio), "whisper"


def _answer(prompt: str, transcript: str) -> str | None:
    """Gemini ladder answer, or None when no key/every model down."""
    from core import gemini
    if not gemini.api_key():
        return None
    try:
        return str(gemini.text([
            {"role": "user", "content":
                "Answer the question using ONLY this video transcript. "
                "Cite timestamps like [MM:SS] when you use them.\n\n"
                f"TRANSCRIPT:\n{transcript[:24000]}\n\n"
                f"QUESTION: {prompt}"}]) or "").strip() or None
    except Exception:
        return None


def _keyword_window(question: str, transcript: str,
                    segs: list[dict], radius: int = 2) -> str:
    """No-key fallback: transcript snippets around question keywords —
    returned as CONTEXT with its timestamps, explicitly not an answer."""
    q_words = {w for w in re.findall(r"[a-z0-9']{3,}",
                                     question.lower())}
    hits = [i for i, s in enumerate(segs)
            if any(w in s["text"].lower() for w in q_words)]
    if not hits:
        return ""
    chunks = []
    for i in hits[:4]:
        lo, hi = max(0, i - radius), min(len(segs), i + radius + 1)
        for s in segs[lo:hi]:
            chunks.append(f"[{_srt_ts(s['start'])[:8]}] {s['text']}")
    return "… " + " ".join(dict.fromkeys(chunks)) + " …"


# ── tool ────────────────────────────────────────────────────────────────────

def video_qa(parameters: dict = None, player=None,
             session_memory=None) -> str:
    from core import privacy as _privacy
    blocked = _privacy.gate("video_qa")
    if blocked:
        return blocked
    params = parameters or {}
    action = str(params.get("action", "captions")).lower().strip()
    path = str(params.get("path") or "").strip()
    if not path:
        return ("Give me the video file — video_qa path=… "
                "[action=captions|ask] [question=…].")
    p = Path(path).expanduser()
    if not p.is_file():
        return f"No such file: {p}"

    try:
        audio = _extract_audio(str(p))
    except RuntimeError as e:
        return f"video_qa: {e}"
    except Exception as e:
        return f"video_qa: audio extraction error ({e})"

    if audio is None or len(audio) < 1600:            # <0.1 s
        return ("No audible track found in that video "
                "(or it is shorter than 0.1s).")

    engine = str(params.get("engine", "whisper") or "whisper").lower()
    try:
        segs, stt_note = _transcribe_segments(audio, engine)
    except Exception as e:
        return f"video_qa: transcription failed ({e}) — is faster-whisper installed?"
    transcript = " ".join(s["text"] for s in segs).strip()
    if not transcript:
        return "Transcribed 0 words — the video has no speech I could hear."

    if action in ("captions", "caption", "srt"):
        out = p.with_suffix(".srt")
        try:
            _write_srt(segs, out)
            txt = p.with_suffix(".transcript.txt")
            txt.write_text(transcript, encoding="utf-8")
        except Exception as e:
            return f"Transcript ({len(segs)} segments) but saving failed: {e}"
        preview = transcript[:300]
        return (f"Captions written: {out} (+ {txt.name}) — "
                f"{len(segs)} segments, {len(transcript)} chars "
                f"[engine: {stt_note}].\n"
                f"Preview: {preview}…")

    if action in ("ask", "q", "question"):
        question = str(params.get("question") or params.get("prompt")
                       or "").strip()
        if not question:
            return "action=ask needs question=… (what to ask the video)."
        ans = _answer(question, transcript)
        # engine note surfaces in the no-key context path too
        global _LAST_NOTE
        _LAST_NOTE = stt_note
        if ans:
            return f"Answer: {ans}"
        win = _keyword_window(question, transcript, segs)
        if win:
            return (f"NO LLM KEY (stt: {stt_note}) — keyword-window "
                    "CONTEXT (not an answer):\n" + win)
        return ("NO LLM KEY and no transcript lines match those words — "
                "add a Gemini key for real answers, or ask different "
                "keywords.")

    return "action must be captions | ask."


TOOL = {
    "name": "video_qa",
    "description": (
        "Offline video understanding: action=captions extracts the audio "
        "(ffmpeg), transcribes with faster-whisper segments and writes a "
        ".srt + transcript next to the file; action=ask answers a "
        "question from the transcript (free Gemini ladder; without a key "
        "returns an honest keyword-window of context, never a fake "
        "answer). Use for lectures, recordings, 'what does this video "
        "say', 'when do they mention X'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "captions (default) | ask."},
            "path": {"type": "STRING",
                     "description": "Path to the video file."},
            "question": {"type": "STRING",
                         "description": "Question for action=ask."},
            "engine": {"type": "STRING",
                       "description": "whisper (default) | whisperx "
                                      "(word-level + diarization when "
                                      "installed; honest fallback)."},
        },
        "required": ["path"],
    },
    "handler": video_qa,
}
