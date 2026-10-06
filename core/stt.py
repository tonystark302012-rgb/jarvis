"""
Speech-to-Text engines for MARK XL.

Whisper  – offline transcription via faster-whisper (VAD-buffered)
Vosk     – offline streaming transcription (lighter)
"""
import json
import numpy as np

from core.logging_setup import get_logger

log = get_logger(__name__)


class WhisperSTT:
    """Offline transcription using faster-whisper."""

    def __init__(self, model_name: str = "base", language: str | None = None):
        import os
        from faster_whisper import WhisperModel
        print(f"[STT] Loading Whisper '{model_name}'…")
        try:
            import torch
            device  = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
        except Exception:
            device, compute = "cpu", "int8"

        try:
            self._model = WhisperModel(model_name, device=device, compute_type=compute)
        except Exception as _first_err:
            # Offline flag set but model not cached yet → clear flags and download once.
            # Keywords cover multiple huggingface_hub error message variants across versions.
            _e = str(_first_err).lower()
            _offline_keywords = (
                "offline", "not found", "cache", "localentry",
                "does not exist", "outgoing", "local_files_only",
            )
            if any(k in _e for k in _offline_keywords):
                print(f"[STT] Whisper '{model_name}' not in local cache — downloading (one-time, internet required)…")
                os.environ.pop("HF_HUB_OFFLINE",      None)
                os.environ.pop("TRANSFORMERS_OFFLINE", None)
                os.environ.pop("HF_DATASETS_OFFLINE",  None)
                try:
                    self._model = WhisperModel(model_name, device=device, compute_type=compute)
                except Exception as _dl_err:
                    raise RuntimeError(
                        f"Whisper '{model_name}' model download failed.\n"
                        f"Internet access is required the first time to download the speech model (~75–290 MB).\n"
                        f"After the first download it runs fully offline.\n"
                        f"Details: {_dl_err}"
                    ) from _dl_err
            else:
                raise

        self._language = None if (not language or language.strip().lower() == "auto") else language.strip().lower()
        print(f"[STT] Whisper '{model_name}' ready ({device})")

    def transcribe(self, audio: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz numpy array. Returns transcript string."""
        try:
            segments, _ = self._model.transcribe(
                audio,
                language=self._language,
                beam_size=1,                       # greedy — 2-3x faster
                best_of=1,
                condition_on_previous_text=False,  # no hallucinations, faster
                vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(s.text for s in segments).strip()
        except Exception as e:
            log.warning(f"Transcription error: {e}")
            raise


    def transcribe_segments(self, audio: np.ndarray) -> list[dict]:
        """Timestamped segments for captions/SRT (additive — transcribe()
        still returns the joined text). [{start, end, text}, …]."""
        if audio is None or len(audio) == 0:
            return []
        segments, _info = self._model.transcribe(
            audio,
            language=self._language,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        out = []
        for seg in segments:
            txt = (seg.text or "").strip()
            if not txt:
                continue
            out.append({"start": float(seg.start or 0.0),
                        "end": float(seg.end or 0.0), "text": txt})
        return out


class WhisperXSTT:
    """OPTIONAL WhisperX (MIT): word-level timestamps + pyannote
    diarization. Honest — every entry point tells you exactly what is
    missing (library vs HuggingFace token) and NOTHING is faked.

    Parity with WhisperSTT: transcribe() → str. Extras:
      transcribe_words()  → [{start, end, word}, …]
      transcribe_diarized() → [{speaker, start, end, text}, …] (needs
                              a free HF token for pyannote; without it
                              one speaker, stated in the return dict)
    """

    _INSTALL = ("WhisperX not installed — free (MIT): `pip install "
                "whisperx` (pulls faster-whisper + pyannote). Word "
                "timestamps work offline; speaker labels additionally "
                "need a free HuggingFace token accepted for "
                "pyannote/speaker-diarization-3.1.")

    def __init__(self, model_name: str = "small",
                 language: str | None = None):
        try:
            import whisperx  # noqa: F401
        except Exception as e:
            raise RuntimeError(self._INSTALL) from e
        self._language = None if (not language or
                                  language.strip().lower() == "auto") \
            else language.strip().lower()
        self._model_name = model_name
        self._loaded = None            # lazy (model download is slow)

    def _load(self):
        if self._loaded is None:
            import whisperx
            device = "cuda"
            try:
                import torch
                if not torch.cuda.is_available():
                    device = "cpu"
            except Exception:
                device = "cpu"
            model = whisperx.load_model(self._model_name, device,
                                        language=self._language)
            self._loaded = (whisperx, model)
        return self._loaded

    def transcribe(self, audio: np.ndarray) -> str:
        segs = self.transcribe_diarized(audio)
        return " ".join(s.get("text", "") for s in segs).strip()

    def transcribe_words(self, audio: np.ndarray) -> list[dict]:
        """Word-level timings — the reason WhisperX exists."""
        whisperx, model = self._load()
        result = model.transcribe(audio)
        words = []
        for seg in (result.get("segments") or []):
            for w in (seg.get("words") or []):
                if w.get("word"):
                    words.append({"start": float(w.get("start", 0.0)),
                                  "end": float(w.get("end", 0.0)),
                                  "word": str(w["word"]).strip()})
        return words

    def transcribe_diarized(self, audio: np.ndarray) -> list[dict]:
        """Segments + speakers. With no HF token: honest single-speaker
        dict (diarized=False), never invented voices."""
        whisperx, model = self._load()
        result = model.transcribe(audio)
        segments = [{"start": float(s.get("start", 0.0)),
                     "end": float(s.get("end", 0.0)),
                     "text": str(s.get("text", "")).strip(),
                     "speaker": "SPEAKER_00", "diarized": False}
                    for s in (result.get("segments") or [])
                    if str(s.get("text", "")).strip()]
        try:
            import os
            token = os.environ.get("HF_TOKEN") or \
                os.environ.get("HUGGINGFACE_TOKEN") or ""
            if not token:
                return segments
            import whisperx as _wx
            diarize_mod = getattr(_wx, "diarize", None)
            if diarize_mod is None:
                return segments
            diarize_segments = diarize_mod(audio, token=token)
            assigned = _wx.assign_word_speakers(diarize_segments, result)
            out = []
            for s in (assigned.get("segments") or []):
                if not str(s.get("text", "")).strip():
                    continue
                out.append({"start": float(s.get("start", 0.0)),
                            "end": float(s.get("end", 0.0)),
                            "text": str(s.get("text", "")).strip(),
                            "speaker": str(s.get("speaker", "SPEAKER_00")),
                            "diarized": True})
            return out or segments
        except Exception:
            return segments


class VoskSTT:
    """Streaming transcription using Vosk."""

    def __init__(self, model_path: str | None = None, language: str = "en-us"):
        from vosk import Model, KaldiRecognizer
        print("[STT] Loading Vosk model…")
        if model_path:
            model = Model(model_path)
        else:
            lang  = language.strip().lower() if language and language.strip().lower() != "auto" else "en-us"
            model = Model(lang=lang)
        self._rec = KaldiRecognizer(model, 16000)
        print("[STT] Vosk ready.")

    def process_chunk(self, audio_bytes: bytes) -> tuple[str, bool]:
        """Feed raw int16 LE PCM bytes. Returns (text, is_final)."""
        if self._rec.AcceptWaveform(audio_bytes):
            result = json.loads(self._rec.Result())
            return result.get("text", ""), True
        partial = json.loads(self._rec.PartialResult())
        return partial.get("partial", ""), False
