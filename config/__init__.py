# config/__init__.py
import json
import platform
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "api_keys.json"


def get_base_dir() -> Path:
    """The app root. One implementation lives in `core/paths.py`.

    This used to re-derive it (`sys.frozen` → executable folder, else
    `__file__.parent.parent`), as did seventeen other modules. Each copy was
    correct only because of where its file happened to sit, and the name it is
    reached by here (`config`) is also shipped by OpenCV as `cv2/config.py` —
    see the sys.path guard in main.py. Delegating keeps the ~30 existing
    `from config import get_base_dir` callers working without a second copy of
    the logic to get wrong.
    """
    from core.paths import base_dir
    return base_dir()


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
