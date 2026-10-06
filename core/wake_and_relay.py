"""Wake word, sleep state and the phone/dashboard relays.

Split out of `main.py` (PROJECT_ANALYSIS P2-15). Two self-contained pieces of
the live session that do not touch the audio loop:

* the wake state machine — mic gating, the "Hey Jarvis" detector, the sleep
  watcher, and the three UI callbacks the HUD calls from the Qt thread;
* the relays — phone audio in, commands out, and stopping the phone feed when
  it disconnects.

Both are mixins so the methods keep their exact `self.` contract: this is a
move, not a rewrite.
"""
from __future__ import annotations

import asyncio
from typing import Any
import time

from core.logging_setup import get_logger
from core.wake_word import WakeWordDetector
from core.wake_word import install_and_download as wake_install
from core.wake_word import is_ready as wake_is_ready
from memory.config_manager import save_wake_word_enabled

log = get_logger("jarvis.main")      # one session, one log stream


class WakeAndRelayMixin:
    """Wake state machine + the dashboard/phone relays.

    Same contract as `ToolDispatchMixin`: annotations for what the session
    provides, no values, so this module type-checks standalone.
    """

    ui: Any
    session: Any = None
    out_queue: Any = None
    _dashboard: Any = None
    _speaking_lock: Any = None
    _is_speaking: bool = False
    _wake_sleep_timeout: float = 0.0
    _wake_detector: Any = None
    _awake: bool = True
    # supplied by the session class, not by this mixin
    _fire_phrase_rules: Any = None
    set_speaking: Any = None

    def _wake_state(self) -> dict:
        # A loaded, running detector is definitively ready; otherwise fall back
        # to the cheap on-disk model-file check (no Model construction).
        ready = bool(self._wake_detector and self._wake_detector.ready) or wake_is_ready()
        return {"enabled": self._wake_enabled, "awake": self._awake, "ready": ready}

    def _ensure_wake_detector(self) -> bool:
        """Load the detector once (model loads on first start). Idempotent."""
        if self._wake_detector is None:
            self._wake_detector = WakeWordDetector(
                on_detect=self._on_wake_detected,
                logger=lambda m: print(f"[Wake] {m}"),
                notify=lambda m: self.ui.write_log(f"SYS: {m}"),
            )
        if not self._wake_detector.ready:
            return self._wake_detector.start()
        return True

    def _on_wake_detected(self) -> None:
        """Called from the detector thread when 'Hey Jarvis' is heard."""
        self.wake(reason="wake word")

    def wake(self, reason: str = "wake word") -> None:
        if self._awake:
            return
        self._awake = True
        self._last_user_speech = time.monotonic()   # start the auto-sleep clock now
        if not self.ui.muted:
            self.ui.set_state("LISTENING")
        self.ui.write_log(f"SYS: Awake — {reason}.")

    def sleep(self, reason: str = "timeout") -> None:
        if not self._awake:
            return
        self._awake = False
        self.set_speaking(False)
        self.ui.set_state("SLEEPING")
        self.ui.write_log(f"SYS: Sleeping — {reason}. Say 'Hey Jarvis' to wake me.")

    async def _run_sleep_watch(self) -> None:
        """Auto-sleep after the configured silence window (wake-word mode only)."""
        while True:
            await asyncio.sleep(5)
            if not self._wake_enabled or not self._awake:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue
            if (time.monotonic() - self._last_user_speech) > self._wake_sleep_timeout:
                self.sleep(reason="no speech for 2 minutes")

    # ── Wake word: UI callbacks (called from the Qt thread) ──────────────────

    def _ui_wake_toggle(self, enable: bool) -> str:
        """Enable/disable wake word from the settings UI. Returns a status token:
        'enabled' | 'disabled' | 'need_download'."""
        if enable:
            if not wake_is_ready():
                return "need_download"
            self._wake_enabled = True
            save_wake_word_enabled(True)
            self._ensure_wake_detector()
            self.sleep(reason="wake word enabled")
            return "enabled"
        else:
            self._wake_enabled = False
            save_wake_word_enabled(False)
            self.wake(reason="wake word disabled")
            return "disabled"

    def _ui_wake_manual(self) -> None:
        """Manual sleep/wake button in the UI."""
        if not self._wake_enabled:
            return
        if self._awake:
            self.sleep(reason="you tapped sleep")
        else:
            self.wake(reason="you tapped wake")

    def _ui_wake_install(self) -> tuple[bool, str]:
        """Download openwakeword + the model (runs in a UI worker thread)."""
        # Triggered by the user pressing the button, so its progress is exactly
        # what they are waiting to see.
        return wake_install(logger=lambda m: print(f"[Wake] {m}"),
                            notify=lambda m: self.ui.write_log(f"SYS: {m}"))

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    # Phone-typed commands get phrase rules as well — the
                    # remote box is a first-class input, not a side door.
                    self._fire_phrase_rules(text)
                    # A remote command is deliberate control and the phone user
                    # has no desktop WAKE button — so it wakes JARVIS if asleep.
                    if self._wake_enabled and not self._awake:
                        self.wake(reason="remote command")
                    await self.session.send_client_content(
                        turns={"role": "user", "parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)
