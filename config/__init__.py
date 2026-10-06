# config/__init__.py
import json
import platform
import subprocess as _subprocess
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

# ── subprocess flags that exist only on Windows ──────────────────────────────
# CREATE_NO_WINDOW / DETACHED_PROCESS are not attributes of the subprocess
# module anywhere else, so read them defensively: 0 is the documented default
# and means "no flags", which is exactly right off Windows. This used to be
# copy-pasted into five files as `if platform.system() == "Windows" else {}`,
# which is how one of them kept crashing on Linux.
CREATE_NO_WINDOW: int = getattr(_subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS: int = getattr(_subprocess, "DETACHED_PROCESS", 0)

def _win_hide() -> dict:
    """Keyword arguments that stop a subprocess from flashing a console."""
    return {"creationflags": CREATE_NO_WINDOW} if is_windows() else {}

WIN_HIDE: dict = _win_hide()
