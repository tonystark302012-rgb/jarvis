"""The device picker, over a fake sounddevice — no audio hardware required.

`core/audio_devices.py` exists because `sd.query_devices()` is unusable as a
menu: one entry per (device × host API), so a normal Windows machine shows 41
rows for what the sound settings call 4 microphones and 4 speakers. The module
reduces that to the list the OS itself would show, and — the part that is easy
to get wrong — refuses devices that *open* but do not *move audio*.

None of that could be tested before: the module imports sounddevice inside each
function, and this machine has no PortAudio. The fake below is the smallest
thing that can answer the questions the module actually asks: which devices
exist, which host APIs carry them, and whether a stream consumes audio in real
time or swallows it instantly.
"""
from __future__ import annotations

import platform
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import audio_devices as ad                                 # noqa: E402

# ── the fake ────────────────────────────────────────────────────────────────
# sounddevice reports a host API as an INDEX into query_hostapis(), not a name —
# the first version of this fake used the name and the module under test failed
# with a type error it would never see in the wild.
APIS = ("MME", "Windows DirectSound", "WASAPI")
MME, DS, WASAPI = APIS


def dev(name, api, *, ins=0, outs=0):
    return {"name": name, "hostapi": APIS.index(api),
            "max_input_channels": ins, "max_output_channels": outs}


class Stream:
    """A stream that behaves like hardware: it takes real time to move audio."""

    def __init__(self, **kw):
        self.kw = kw
        self.started = False
        self.closed = False
        self._thread = None
        self.frames = 0

    def start(self):
        self.started = True
        cb = self.kw.get("callback")
        if cb is None:
            return
        block = self.kw.get("blocksize", 1024)

        def _feed():
            while self.started:
                cb(b"\x00" * block * 2, block, None, None)
                self.frames += block
                time.sleep(0.001)          # faster than real time: tests are short

        self._thread = threading.Thread(target=_feed, daemon=True)
        self._thread.start()

    def write(self, data: bytes):
        time.sleep(len(data) / 2 / self.kw.get("samplerate", 24000))

    def stop(self):
        self.started = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def close(self):
        self.closed = True


class SwallowingStream(Stream):
    """A host API that reports success and discards the buffer instantly."""

    def write(self, data: bytes):
        return None


class FakeSD:
    def __init__(self, devices, apis=APIS, *, swallow=(), fail_open=(),
                 broken=False):
        self.devices = devices
        self.apis = apis
        self.swallow = swallow
        self.fail_open = fail_open
        self.broken = broken
        self.opened: list[int] = []

    def query_devices(self):
        if self.broken:
            raise RuntimeError("PortAudio is having a bad day")
        return list(self.devices)

    def query_hostapis(self):
        return [{"name": name} for name in self.apis]

    def _open(self, swallow_ok, **kw):
        device = kw.pop("device", None)
        if device in self.fail_open:
            raise RuntimeError(f"cannot open device {device}")
        self.opened.append(device)
        if swallow_ok and device in self.swallow:
            return SwallowingStream(**kw)
        return Stream(**kw)

    def RawOutputStream(self, **kw):        # output: the sink may swallow
        return self._open(True, **kw)

    def InputStream(self, **kw):            # input: it either feeds or it does not
        return self._open(False, **kw)


# ── a normal machine ───────────────────────────────────────────────────────
def _machine():
    """Two mics, two speakers, each visible through three host APIs — plus the
    alias rows Windows adds that nobody wants to see."""
    return [
        dev("Sound Mapper - Input", MME, ins=2),
        dev("Microphone (Realtek(R) Audio)", MME, ins=2),
        dev("Microphone (Realtek(R) Audio)", DS, ins=2),
        dev("Microphone (Realtek(R) Audio)", WASAPI, ins=2),
        dev("Microphone (USB Headset)", DS, ins=1),
        dev("Speakers (Realtek(R) Audio)", MME, outs=2),
        dev("Speakers (Realtek(R) Audio)", DS, outs=2),
        dev("Speakers (Realtek(R) Audio)", WASAPI, outs=2),
        # output-only: must never appear in the input list, and vice versa
        dev("HDMI Output (NVIDIA)", DS, outs=2),
        # ALSA-style aliases
        dev("sysdefault:CARD=PCH", MME, ins=2, outs=2),
        dev("default", MME, ins=2, outs=2),
    ]


@pytest.fixture()
def sd(monkeypatch):
    """Install a fake sounddevice and reset the module's caches."""
    fake = FakeSD(_machine())
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(ad, "_cache", None)
    monkeypatch.setattr(ad, "_chosen_api", {"input": None, "output": None})
    # The real probe measures wall-clock, so it costs real seconds per host API
    # per direction. Every test except the ones *about* the probe starts with
    # the measurement already taken — keyed the way the module keys it, by the
    # lowercase API filter and the direction.
    monkeypatch.setattr(ad, "_probe_results",
                        {(a, k): True for a in ("directsound", "mme", "wasapi", None)
                         for k in ("input", "output")})
    # and when a test does run it, the probe is miniature: the fake stream still
    # moves audio in real time, so the measurement still means something
    monkeypatch.setattr(ad, "_PROBE_SECONDS", {"output": 0.12, "input": 0.05})
    yield fake


# ── what the picker shows ──────────────────────────────────────────────────
class TestTheList:
    def test_aliases_are_not_devices(self, sd):
        names = ad.list_devices("input")
        assert names == ["Microphone (Realtek(R) Audio)", "Microphone (USB Headset)"]
        assert not any("Sound Mapper" in n or n in ("default",) for n in names)

    def test_the_same_device_is_not_listed_once_per_host_api(self, sd):
        # it appears under MME, DirectSound and WASAPI; the picker shows one
        names = ad.list_devices("input")
        assert len(names) == len(set(names)) == 2

    def test_one_direction_never_shows_the_other_side(self, sd):
        assert "HDMI Output (NVIDIA)" not in ad.list_devices("input")
        assert "Microphone (USB Headset)" not in ad.list_devices("output")

    def test_the_picker_prefers_the_api_that_actually_carries_audio(self, sd, monkeypatch):
        """Each direction settles on ONE host API, and the names come from it.
        DirectSound is preferred on Windows; WASAPI and MME are fallbacks."""
        monkeypatch.setattr(ad, "_probe_results", {})
        ad.list_devices("input", refresh=True)
        assert ad._chosen_api["input"] == "directsound"
        assert ad._chosen_api["output"] == "directsound"

    def test_an_api_that_swallows_audio_is_passed_over(self, sd, monkeypatch):
        """Opening a stream proves nothing: a DirectSound sink on this machine
        accepted the write and consumed 2.0 s of audio in 0.00 s. The probe
        measures the clock instead, and the next API gets the direction."""
        monkeypatch.setattr(ad, "_probe_results", {})
        # indices 2, 4, 6, 8 in `_machine()` are the DirectSound endpoints
        sd.swallow = {2, 4, 6, 8}
        names = ad.list_devices("output", refresh=True)
        assert ad._chosen_api["output"] == "mme"
        assert names and all("Speakers" in n or "HDMI" in n for n in names)

    def test_a_device_that_cannot_be_opened_is_left_out(self, sd, monkeypatch):
        monkeypatch.setattr(ad, "_probe_results", {})
        # every index that can capture, on every host API: nothing is left for
        # the picker to offer, and it must say so rather than show a dead device
        sd.fail_open = {0, 1, 2, 3, 4, 9, 10}
        assert ad.list_devices("input", refresh=True) == []

    def test_enumeration_never_raises_and_returns_nothing(self, sd):
        sd.broken = True
        assert ad.list_devices("input", refresh=True) == []
        assert ad.list_devices("output") == []

    def test_a_shortened_name_is_shown_in_full(self):
        """MME truncates to 31 characters; another API knows the whole name. The
        person reads the readable one while the stream opens the truncated one."""
        devices = [dev("Realtek HD Audio 2nd output (Re", MME, outs=2),
                       dev("Realtek HD Audio 2nd output (Realtek(R) Audio)", DS, outs=2)]
        assert ad._display_name("Realtek HD Audio 2nd output (Re", devices) == \
            "Realtek HD Audio 2nd output (Realtek(R) Audio)"
        assert ad._display_name("Microphone", devices) == "Microphone"


class TestTheCache:
    def test_the_second_call_does_not_ask_the_driver_again(self, sd, monkeypatch):
        calls = []
        real = sd.query_devices
        monkeypatch.setattr(sd, "query_devices",
                            lambda: (calls.append(1), real())[1])
        ad.list_devices("input", refresh=True)
        ad.list_devices("input")
        ad.list_devices("output")
        assert len(calls) == 1

    def test_refresh_asks_again(self, sd, monkeypatch):
        calls = []
        real = sd.query_devices
        monkeypatch.setattr(sd, "query_devices",
                            lambda: (calls.append(1), real())[1])
        ad.list_devices("input", refresh=True)
        ad.list_devices("input", refresh=True)
        assert len(calls) == 2

    def test_a_cold_cache_is_filled_synchronously_rather_than_returned_empty(
            self, sd):
        """The settings drawer calls this from the Qt thread the first time it
        opens; an empty list there is a bug the user sees, not a slow paint."""
        assert ad._cache is None
        assert ad.list_devices("input") == [
            "Microphone (Realtek(R) Audio)", "Microphone (USB Headset)"]
        assert ad._cache is not None

    def test_prefetch_warms_it_on_a_background_thread(self, sd, monkeypatch):
        monkeypatch.setattr(ad, "_cache", None)
        ad.prefetch()
        for _ in range(60):
            if ad._cache is not None:
                break
            time.sleep(0.05)
        assert ad._cache is not None, "prefetch never filled the cache"
        assert ad._cache["output"]


class TestRates:
    def test_configure_tells_the_module_the_rates_and_drops_the_list(self, sd):
        ad.list_devices("input", refresh=True)
        assert ad._cache is not None
        ad.configure(8000, 48000)
        assert ad._RATES == {"input": 8000, "output": 48000}
        assert ad._cache is None, "a list built at the old rate is stale"

    def test_the_probe_uses_the_configured_rate(self, sd, monkeypatch):
        monkeypatch.setattr(ad, "_probe_results", {})
        ad.configure(8000, 12000)
        ad.list_devices("output", refresh=True)
        assert sd.opened, "no device was opened — the probe did not run"


class TestResolve:
    def test_the_default_label_and_empty_mean_default(self, sd):
        assert ad.resolve("", "input") is None
        assert ad.resolve(ad.DEFAULT_LABEL, "input") is None
        assert ad.resolve(None, "output") is None

    def test_a_saved_name_resolves_to_its_device(self, sd):
        assert ad.resolve("Microphone (USB Headset)", "input") == 4

    def test_a_missing_device_degrades_to_default(self, sd, capsys):
        """A headset that has been unplugged must not stop the app from
        starting; the user gets the built-in speakers and a line in the log."""
        assert ad.resolve("Bluetooth Headset", "input") is None
        assert "cannot be opened" in capsys.readouterr().out

    def test_a_name_the_api_could_only_abbreviate_still_matches(self, sd):
        # MME can only spell the first 31 characters, so the name in the config
        # may be the truncated one — it still has to find its endpoint
        long_name = "Speakers (Realtek(R) Audio)"
        assert ad.resolve(long_name[:24], "output") == 6

    def test_resolve_never_raises(self, sd):
        sd.broken = True
        assert ad.resolve("Microphone (USB Headset)", "input") is None
