# config/__init__.py
import json
import platform
import sys
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "api_keys.json"


def get_base_dir() -> Path:
    """Repository/app root — the folder that contains main.py.

    Frozen (PyInstaller) builds resolve to the executable's folder instead.
    Every module that needs a stable anchor for config/, macros/, diagrams/
    imports this one function rather than re-deriving it (four copies of
    that derivation existed before and one caller imported a name that was
    never defined — see tests/test_new_features.py::TestWiringGuards).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _platform_os() -> str:
    """Auto-detect OS when config file is absent."""
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def get_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def get_os() -> str:
    """Returns: 'windows' | 'mac' | 'linux'"""
    return get_config().get("os_system", _platform_os()).lower()

def is_windows() -> bool: return get_os() == "windows"
def is_mac()     -> bool: return get_os() == "mac"
def is_linux()   -> bool: return get_os() == "linux"
