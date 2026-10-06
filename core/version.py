# core/version.py
"""The version, in one place.

`--version` used to mean reading `pyproject.toml` (which had no `[project]`
table) or guessing from git. A bug report needs one honest answer, and the
diagnostics bundle needs to put it next to the log lines.
"""
from __future__ import annotations

#: Bumped by hand. Keep it in step with the tag, and let `tests/test_version.py`
#: check that `--version`, this module and the package metadata cannot disagree.
__version__ = "1.4.0"

NAME = "JARVIS"
TAGLINE = "voice-first desktop assistant"


def version_string() -> str:
    """`JARVIS 1.4.0` — what `--version` prints and the header of a report."""
    return f"{NAME} {__version__}"


def full_version() -> str:
    """Version plus the running interpreter and platform, for bug reports."""
    import platform
    import sys
    return (f"{version_string()} · Python {platform.python_version()} "
            f"({sys.implementation.name}) · {platform.system()} "
            f"{platform.release()}")
