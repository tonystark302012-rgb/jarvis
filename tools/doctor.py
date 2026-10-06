# tools/doctor.py
"""`python main.py --doctor` — check this machine for what JARVIS needs.

WHY
    A fresh clone can come up looking fine and then quietly under-deliver:
    Playwright installed but its browsers not downloaded, no audio device, no
    display, a key file that is present but empty. Every one of those shows up
    later as a tool that "does nothing", which is the hardest kind of bug to
    report. This turns them into a list you can read before anything goes
    wrong.

DESIGN
    Checks are data, not control flow: each returns `(status, name, detail,
    fix)`. Nothing here raises, nothing imports the app (no Qt, no audio), and
    every optional component reports as optional. Required-vs-optional is the
    only thing that decides the exit code.

    `--fix` acts on the report by installing the missing packages through
    core/installer.py, which is otherwise unreachable code.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import sys
from pathlib import Path

# Run as `python tools/doctor.py` the script's own folder is sys.path[0], so
# `from core...` fails — the repo root has to be importable. Same guard, same
# reason, as main.py.
if __name__ == "__main__" or __package__ is None:
    _root = Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_ICON = {OK: "✓", WARN: "!", FAIL: "✗", SKIP: "·"}

#: (import name, label, why it matters) — required for the core experience.
CORE_DEPS = (
    ("PyQt6", "PyQt6", "the window, HUD and avatar"),
    ("sounddevice", "sounddevice", "microphone and speaker"),
    ("numpy", "numpy", "audio buffers and the avatar mesh"),
    ("google.genai", "google-genai", "the Gemini Live conversation"),
)

#: Optional — the tool that needs each one says so honestly when it is absent.
OPTIONAL_DEPS = (
    ("playwright", "playwright", "browser_control"),
    ("yt_dlp", "yt-dlp", "youtube_video streaming"),
    ("bs4", "beautifulsoup4", "scrape"),
    ("duckduckgo_search", "duckduckgo-search", "web_search (DDG rung)"),
    ("croniter", "croniter", "dots scheduling"),
    ("feedparser", "feedparser", "feed reading"),
    ("duckdb", "duckdb", "data (SQL over CSV)"),
    ("jedi", "jedi", "code_intel navigation"),
    ("trafilatura", "trafilatura", "scrape structured mode"),
    ("paho.mqtt", "paho-mqtt", "smart_home"),
    ("cryptography", "cryptography", "dashboard device crypto"),
    ("psutil", "psutil", "system_status / scanner"),
)

#: Binaries we shell out to somewhere.
OPTIONAL_BINS = (
    ("tesseract", "region_ocr offline"),
    ("ffmpeg", "meeting recorder / video"),
    ("pactl", "volume on Linux"),
    ("xrandr", "brightness on Linux"),
)


def _check_import(module: str, label: str, why: str, required: bool):
    try:
        found = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        found = False
    if found:
        return OK, label, why, ""
    return ((FAIL if required else WARN), label,
            f"missing — needed by {why}",
            f"pip install {label}")


def run_checks(base_dir: Path | None = None) -> list[tuple[str, str, str, str]]:
    """Every check, as data. Never raises."""
    checks: list[tuple[str, str, str, str]] = []

    # Python itself
    v = sys.version_info
    if v >= (3, 11):
        checks.append((OK, f"python {v.major}.{v.minor}",
                       "supported", ""))
    else:
        checks.append((FAIL, f"python {v.major}.{v.minor}",
                       "3.11 or newer required",
                       "install a newer Python"))

    checks.append((OK, platform.system() or "unknown OS",
                   f"{platform.release()} {platform.machine()}", ""))

    for module, label, why in CORE_DEPS:
        checks.append(_check_import(module, label, why, required=True))
    for module, label, why in OPTIONAL_DEPS:
        checks.append(_check_import(module, label, why, required=False))

    for binary, why in OPTIONAL_BINS:
        if shutil.which(binary):
            checks.append((OK, binary, why, ""))
        else:
            checks.append((SKIP, binary, f"not found — {why} degrades", ""))

    # Where things live
    root = Path(base_dir) if base_dir else _app_root()
    if root and (root / "actions").is_dir():
        checks.append((OK, "app root", str(root), ""))
    else:
        checks.append((FAIL, "app root",
                       f"actions/ not found under {root}",
                       "run from the JARVIS folder"))

    key = _key_state(root)
    checks.append(key)

    cfg = (root / "config") if root else None
    if cfg is not None:
        try:
            cfg.mkdir(parents=True, exist_ok=True)
            probe = cfg / ".doctor-write-test"
            probe.write_text("x", encoding="utf-8")
            probe.unlink()
            checks.append((OK, "config writable", str(cfg), ""))
        except Exception as e:
            checks.append((FAIL, "config writable", f"{cfg}: {e}",
                           "check folder permissions"))

    # Display: the GUI cannot start without one on Linux.
    if platform.system() == "Linux" and not (os.environ.get("DISPLAY")
                                            or os.environ.get("WAYLAND_DISPLAY")):
        checks.append((WARN, "display", "neither DISPLAY nor WAYLAND_DISPLAY is set",
                       "run on a desktop session, or use QT_QPA_PLATFORM=offscreen"))
    else:
        checks.append((OK, "display", "available", ""))

    try:
        import sounddevice as sd
        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels")]
        outputs = [d for d in devices if d.get("max_output_channels")]
        if inputs and outputs:
            checks.append((OK, "audio devices",
                           f"{len(inputs)} input / {len(outputs)} output", ""))
        else:
            checks.append((FAIL, "audio devices",
                           f"{len(devices)} device(s), no usable input+output pair",
                           "check your sound settings"))
    except Exception as e:
        checks.append((WARN, "audio devices", f"could not enumerate: {e}", ""))

    return checks


def _app_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "main.py").is_file():
            return parent
    return None


def _key_state(root: Path | None):
    try:
        from core.paths import get_api_key, MissingAPIKey
        try:
            get_api_key()
            return (OK, "gemini api key", "present", "")
        except MissingAPIKey as e:
            return (FAIL, "gemini api key", str(e),
                    "add it in the app's first-run screen")
        except Exception as e:                       # pragma: no cover
            return (WARN, "gemini api key", f"could not check: {e}", "")
    except Exception:
        # core.paths not importable (run from outside the tree) — fall back
        p = (root / "config" / "api_keys.json") if root else None
        if p and p.exists() and p.stat().st_size > 2:
            return (OK, "gemini api key", f"file present at {p}", "")
        return (WARN, "gemini api key", "could not check", "")


def format_report(checks: list[tuple[str, str, str, str]]) -> str:
    from core.version import full_version
    width = max((len(c[1]) for c in checks), default=10)
    lines = [full_version(), ""]
    for status, name, detail, fix in checks:
        lines.append(f" {_ICON.get(status, '?')} {name:<{width}}  {detail}")
        if fix and status in (FAIL, WARN):
            lines.append(f"   {' ' * width}  → {fix}")
    fails = [c for c in checks if c[0] == FAIL]
    warns = [c for c in checks if c[0] == WARN]
    lines.append("")
    if fails:
        lines.append(f"{len(fails)} problem(s) to fix, {len(warns)} warning(s).")
    elif warns:
        lines.append(f"Everything required is present. {len(warns)} optional "
                     f"thing(s) missing — those tools say so when asked.")
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def _missing_pip_names(checks: list[tuple[str, str, str, str]]) -> list[str]:
    """The pip names behind the missing imports, in report order.

    The check tuple carries a pip install hint already; scissoring it back out
    of prose is how advice and action drift apart. The mapping is from the one
    place that knows it (the dependency tables above).
    """
    # tuples are (import name, pip name, why); the report shows the pip name,
    # which is the one a user can act on
    labels = {label: label for _, label, _ in CORE_DEPS + OPTIONAL_DEPS}
    out: list[str] = []
    for status, name, _detail, _fix in checks:
        if status in (FAIL, WARN) and name in labels:
            out.append(labels[name])
    return out


def fix(checks: list[tuple[str, str, str, str]]) -> int:
    """Install the missing packages — the action behind the advice.

    This is where core/installer.py is used from. It was written as "called
    automatically on first launch", which stopped being true at some point and
    left ~200 lines of unreachable code plus a promise the app did not keep
    (offline transcription needs faster-whisper, which nothing installed).
    Installing packages is not something to do behind the user's back: it
    happens when they ask for `--fix`, and it says what it is doing.
    """
    missing = _missing_pip_names(checks)
    if not missing:
        print("Nothing to install — every dependency the doctor knows about is present.")
        return 0
    from core.installer import install_missing          # noqa: PLC0415
    print(f"Installing {len(missing)} missing package(s): {', '.join(missing)}")
    # the doctor's table is the one that knows which import each pip name
    # provides (paho-mqtt → paho.mqtt, beautifulsoup4 → bs4)
    names = {pip: imp for imp, pip, _why in CORE_DEPS + OPTIONAL_DEPS}
    failures = install_missing(missing, log=lambda m: print("  " + m),
                               import_names=names)
    if failures:
        print(f"\n{len(failures)} package(s) could not be installed: "
              f"{', '.join(failures)}")
        print("Everything else will work; the tools that need these will say so.")
        return 1
    print("\nDone. Run the doctor again to confirm.")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    checks = run_checks()
    print(format_report(checks))
    if "--fix" in args:
        print()
        return fix(checks)
    if any(c[0] == FAIL for c in checks):
        print("\nRun with --fix to install what is missing.")
    return 1 if any(c[0] == FAIL for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
