"""
MARK XL — Dependency installer.

NOT called automatically. It used to say it ran on first launch; nothing had
called it in a long time, which left this file unreachable and one promise
broken — `actions/dictation.py` and `actions/meeting.py` need faster-whisper,
and nothing installed it.

It is now reached from `python main.py --doctor --fix` (tools/doctor.py), which
is the honest place for it: the doctor reports what is missing, and installing
packages happens when the user asks for it — never behind their back.

Installs only the packages that are actually missing, and reports what failed
instead of raising.
"""
from __future__ import annotations

import importlib.util
import platform
import subprocess
import sys
from typing import Callable

# ── Package lists ─────────────────────────────────────────────────────────
# Each entry: (import_name, pip_package_name)

_CORE: list[tuple[str, str]] = [
    ("psutil",             "psutil"),
    ("PIL",                "pillow"),
    ("sounddevice",        "sounddevice"),
    ("numpy",              "numpy"),
    ("requests",           "requests"),
    ("bs4",                "beautifulsoup4"),
    ("ddgs",               "ddgs"),
    ("pyautogui",          "pyautogui"),
    ("pyperclip",          "pyperclip"),
    ("pygetwindow",        "pygetwindow"),
    ("mss",                "mss"),
    ("cv2",                "opencv-python"),
    ("soundfile",          "soundfile"),
    ("miniaudio",          "miniaudio"),
    ("send2trash",         "send2trash"),
    ("pptx",               "python-pptx"),
    ("youtube_transcript_api", "youtube-transcript-api"),
]

# Windows-only (pywinauto, pycaw, win10toast, comtypes)
_WINDOWS: list[tuple[str, str]] = [
    ("comtypes",   "comtypes"),
    ("pycaw",      "pycaw"),
    ("win10toast", "win10toast"),
    ("pywinauto",  "pywinauto"),
]

# STT engine packages
_STT: dict[str, list[tuple[str, str]]] = {
    "whisper": [("faster_whisper", "faster-whisper")],
    "vosk":    [("vosk",           "vosk")],
}

# TTS engine packages
_TTS: dict[str, list[tuple[str, str]]] = {
    "edgetts":    [("edge_tts", "edge-tts")],
    # kokoro>=0.9 dropped AlbertModel/AutoModel from transformers — version pin is critical
    "kokoro":     [("kokoro",   "kokoro>=0.9"), ("soundfile", "soundfile")],
    "elevenlabs": [],   # uses only requests, already in core
}


#: Flattened views for callers that have a package name and need the import
#: name (or vice versa) without knowing which table it lives in.
_STT_ALL = [pair for pairs in _STT.values() for pair in pairs]
_TTS_ALL = [pair for pairs in _TTS.values() for pair in pairs]


# ── Helpers ───────────────────────────────────────────────────────────────

def _available(module: str) -> bool:
    """Return True if the module can be imported (no actual import)."""
    return importlib.util.find_spec(module) is not None


def _pip(package: str, log: Callable | None = None) -> bool:
    if log:
        log(f"SYS: pip install {package} …")
    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "install", package,
            "--quiet", "--disable-pip-version-check",
        ],
        capture_output=True,
    )
    ok = result.returncode == 0
    if not ok and log:
        stderr = result.stderr.decode(errors="replace").strip()
        log(f"ERR: {package} install failed — {stderr[:140]}")
    return ok


# ── Public API ────────────────────────────────────────────────────────────

def install_missing(packages: list[str], log: Callable | None = None,
                    import_names: dict[str, str] | None = None) -> list[str]:
    """pip-install each package (skipping the ones already importable).

    Returns the pip names that FAILED, so a caller can report them and carry on.

    `packages` are pip names ("faster-whisper"), but the thing that decides
    whether they are needed is the IMPORT name, and the two differ often enough
    that guessing is how you reinstall a package on every run. Lookup order:
    the caller's own mapping (the doctor has the full dependency table, and it
    covers packages this file's tables never mentioned — `paho-mqtt` installs
    `paho.mqtt`, not `paho_mqtt`), then this file's tables, then a dash-to-
    underscore best effort.
    """
    by_pip = dict(import_names or {})
    by_pip.update({pip: imp for imp, pip in _CORE + _WINDOWS + _STT_ALL + _TTS_ALL})
    failed: list[str] = []
    for pip_name in packages:
        import_name = by_pip.get(pip_name) or pip_name.replace("-", "_")
        if _available(import_name):
            if log:
                log(f"{import_name} already present")
            continue
        if not _pip(pip_name, log):
            failed.append(pip_name)
    return failed


def install_for_config(config: dict, log: Callable | None = None) -> None:
    """
    Install all missing packages required by *config*.

    Blocking — always call from a background thread.
    Progress is reported via the optional *log* callback (receives a str).
    """
    stt = config.get("stt_engine", "whisper").lower()
    tts = config.get("tts_engine", "edgetts").lower()

    needed: list[tuple[str, str]] = list(_CORE)
    needed += _STT.get(stt, [])
    needed += _TTS.get(tts, [])
    if platform.system() == "Windows":
        needed += _WINDOWS

    # Deduplicate (preserve order, key = pip name)
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for mod, pkg in needed:
        if pkg not in seen:
            seen.add(pkg)
            unique.append((mod, pkg))

    missing = [(mod, pkg) for mod, pkg in unique if not _available(mod)]

    if not missing:
        if log:
            log("SYS: All dependencies already installed ✓")
        return

    pkg_names = ", ".join(p for _, p in missing)
    if log:
        log(f"SYS: Installing {len(missing)} package(s): {pkg_names}")

    for _mod, pkg in missing:
        _pip(pkg, log)

    # Playwright: install the package + download Chromium browser
    if not _available("playwright"):
        _pip("playwright", log)
        if log:
            log("SYS: Downloading Playwright browser (Chromium, ~150 MB — one-time)…")
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
        )
        if log:
            log("SYS: Playwright browser ready.")

    if log:
        log("SYS: All dependencies ready ✓")
