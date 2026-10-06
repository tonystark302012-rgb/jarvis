# core/paths.py
"""Where things live — one implementation of the app root, and one way to read
a credential out of `config/api_keys.json`.

WHY THIS EXISTS
    Eighteen modules used to re-derive the app root themselves:

        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent
        return Path(__file__).resolve().parent.parent

    That is correct in all eighteen — each one happens to sit at the right
    depth — which is exactly what made it dangerous. It is correct by
    coincidence of file placement, and the next module to copy the pattern
    from a sibling one directory deeper would be silently wrong (it would
    resolve to `actions/` and read a config file that is not there). Three
    more modules imported `get_base_dir` from the `config` package, which is
    the same derivation behind a name that OpenCV also ships (see the guard in
    main.py).

    So: one function, one depth, and a test that fails if a nineteenth copy
    appears (`tests/test_paths.py`).

THE FROZEN CASE
    PyInstaller sets `sys.frozen` and unpacks the app elsewhere, so the repo
    layout is gone and the executable's folder becomes the anchor. Every copy
    of this logic had that branch; this is the only one that keeps it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def base_dir() -> Path:
    """The app root — the folder that contains `main.py`.

    Frozen builds resolve to the executable's folder instead, because that is
    where the user's config/ and memory/ live once the app is shipped.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return base_dir() / "config"


def api_keys_path() -> Path:
    """`config/api_keys.json` — created on first launch, git-ignored."""
    return config_dir() / "api_keys.json"


class MissingAPIKey(RuntimeError):
    """A credential was asked for and is not there.

    Raised with a message that says which file and which key, because the
    alternatives seen in this codebase were an empty string (which turns into
    a confusing 401 several layers later) or a bare `KeyError` (which says
    nothing).
    """


def get_api_key(name: str = "gemini_api_key") -> str:
    """Read one key from `config/api_keys.json`.

    Strict on purpose: a missing file, a missing key and an empty value all
    raise `MissingAPIKey`. An empty string is not a credential.
    """
    path = api_keys_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise MissingAPIKey(
            f"no config/api_keys.json at {path} — the app writes it on first "
            f"launch; add {name!r} to it, or set it in the app's settings"
        ) from e
    except (OSError, ValueError) as e:
        raise MissingAPIKey(f"could not read {path}: {e}") from e
    if not isinstance(data, dict):
        raise MissingAPIKey(f"{path} is not a JSON object")
    value = str(data.get(name) or "").strip()
    if not value:
        raise MissingAPIKey(
            f"{name!r} is missing or empty in {path} — add it, or set it from "
            f"the app's settings")
    return value


def api_key_or_empty(name: str = "gemini_api_key") -> str:
    """The soft variant, for callers that PROBE rather than require.

    `computer_control._screen_find` wants to know whether vision is available
    so it can degrade to another path; a missing key there is an answer, not
    an error. Everything that actually needs the key uses `get_api_key`.
    """
    try:
        return get_api_key(name)
    except MissingAPIKey:
        return ""


def read_config() -> dict:
    """The whole `api_keys.json` as a dict, or {} if it cannot be read.

    Soft on purpose: callers use it for optional settings (assistant name,
    voice, toggles) where "not configured" is a normal state, not an error.
    Credentials go through `get_api_key`, which is strict for the opposite
    reason.
    """
    try:
        data = json.loads(api_keys_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
