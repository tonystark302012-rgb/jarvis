"""
scanner — read the machine and the network around it.

Everything here is local and free: psutil (already a dependency), the standard
library, and sockets. No API key, no cloud call, nothing leaves the machine
except an optional outbound ping.

Deliberately read-only. This tool inspects; it does not change. That makes it
safe to run in the parallel batch in main.py, and safe to hand to a model whose
context may have come from a web page.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except ImportError:                                    # pragma: no cover
    _PSUTIL = False


def _log(message: str, player=None) -> None:
    print(f"[Scanner] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def _gb(n: float) -> str:
    return f"{n / (1024 ** 3):.1f} GB"


# ── system ────────────────────────────────────────────────────────────────────

def scan_system() -> str:
    """Hardware, OS, disk and what is running."""
    if not _PSUTIL:
        return "System scanning needs psutil. Run: pip install psutil"

    un = platform.uname()
    boot = datetime.fromtimestamp(psutil.boot_time())
    up = time.time() - psutil.boot_time()
    up_str = f"{int(up // 3600)}h {int((up % 3600) // 60)}m"

    vm = psutil.virtual_memory()
    try:
        du = shutil.disk_usage(str(Path.home()))
        disk = (f"{_gb(du.used)} of {_gb(du.total)} used "
                f"({du.used / du.total * 100:.0f}%)")
    except Exception:
        disk = "unavailable"

    cpu_freq = ""
    try:
        f = psutil.cpu_freq()
        if f and f.current:
            cpu_freq = f" at {f.current:.0f} MHz"
    except Exception:
        pass

    lines = [
        f"{un.system} {un.release} ({un.machine}), up since "
        f"{boot:%Y-%m-%d %H:%M}, uptime {up_str}",
        f"CPU: {psutil.cpu_count(logical=True)} logical cores{cpu_freq}, "
        f"{psutil.cpu_percent(interval=0.4):.0f}% busy",
        f"Memory: {_gb(vm.used)} of {_gb(vm.total)} used ({vm.percent:.0f}%)",
        f"Home disk: {disk}",
        f"Processes running: {len(psutil.pids())}",
    ]

    # the few processes actually eating the machine
    try:
        procs = []
        for p in psutil.process_iter(["name", "cpu_percent", "memory_percent"]):
            try:
                i = p.info
                procs.append((i.get("cpu_percent") or 0.0,
                              i.get("memory_percent") or 0.0,
                              i.get("name") or "?"))
            except Exception:
                continue
        top = sorted(procs, key=lambda r: -r[0])[:5]
        if top:
            lines.append("Busiest: " + ", ".join(
                f"{n} ({c:.0f}% cpu)" for c, _m, n in top))
    except Exception:
        pass

    return "\n".join(lines)


# ── network ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))     # does not send a packet
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "unknown"
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def scan_network() -> str:
    """This machine's addresses, and whether the internet is reachable."""
    ip = _local_ip()
    host = socket.gethostname()

    lines = [f"This machine: {host} at {ip}"]

    if _PSUTIL:
        try:
            up = 0
            for nic, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and not a.address.startswith("127."):
                        line = f"  {nic}: {a.address}"
                        try:
                            if psutil.net_if_stats().get(nic) and \
                               psutil.net_if_stats()[nic].isup:
                                up += 1
                                line += " (up)"
                        except Exception:
                            pass
                        lines.append(line)
            lines.append(f"Interfaces up: {up}")
        except Exception:
            pass

        try:
            c = psutil.net_io_counters()
            lines.append(f"Since boot: {_gb(c.bytes_recv)} received, "
                         f"{_gb(c.bytes_sent)} sent")
        except Exception:
            pass

    # one short reachability probe - a DNS name that resolves everywhere
    try:
        t0 = time.time()
        socket.setdefaulttimeout(3)
        socket.gethostbyname("one.one.one.one")
        lines.append(f"Internet reachable (DNS resolved in "
                     f"{(time.time() - t0) * 1000:.0f} ms)")
    except Exception:
        lines.append("Internet unreachable - DNS did not resolve")
    finally:
        socket.setdefaulttimeout(None)

    return "\n".join(lines)


# ── listening ports ───────────────────────────────────────────────────────────

def scan_ports() -> str:
    """What this machine is offering to the network. Read-only."""
    if not _PSUTIL:
        return "Port scanning needs psutil. Run: pip install psutil"
    try:
        rows = []
        for c in psutil.net_connections(kind="inet"):
            if c.status != psutil.CONN_LISTEN:
                continue
            try:
                proc = psutil.Process(c.pid).name() if c.pid else "system"
            except Exception:
                proc = "?"
            addr = c.laddr
            where = f"{addr.ip}:{addr.port}" if addr else "?"
            # only flag it when it is actually reachable from outside
            exposed = addr.ip in ("0.0.0.0", "::") if addr else False
            rows.append((where, proc, exposed))
        if not rows:
            return "Nothing is listening for incoming connections."
        rows.sort(key=lambda r: (not r[2], r[0]))
        out = [f"{len(rows)} services listening:"]
        for where, proc, exposed in rows[:25]:
            out.append(f"  {where}  {proc}"
                       + ("   <- reachable from the network" if exposed else ""))
        if len(rows) > 25:
            out.append(f"  (+{len(rows) - 25} more)")
        return "\n".join(out)
    except Exception as e:
        # macOS and some Linux setups deny this without elevated rights
        return f"Could not list listening ports ({e})."


# ── filesystem ────────────────────────────────────────────────────────────────

def scan_files(target: str = "", top: int = 12) -> str:
    """Where the space went: biggest files and stale ones. Read-only."""
    root = Path(target).expanduser() if target else Path.home()
    if not root.exists():
        return f"No such folder: {root}"
    if not root.is_dir():
        return f"Not a folder: {root}"

    biggest: list[tuple[int, Path]] = []
    skipped = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # never descend into caches or version control - noise, and slow
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", "node_modules", "__pycache__",
                                        ".venv", "venv", ".cache", ".local",
                                        "Library", "AppData"}]
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    biggest.append((p.stat().st_size, p))
                except Exception:
                    skipped += 1
    except Exception as e:
        return f"Scan of {root} stopped early: {e}"

    if not biggest:
        return f"No files under {root} (or none readable)."

    biggest.sort(key=lambda r: -r[0])
    total = sum(s for s, _ in biggest)
    lines = [f"{len(biggest)} files under {root}, {_gb(total)} total"
             + (f" ({skipped} unreadable)" if skipped else ""),
             "Largest:"]
    for size, p in biggest[:top]:
        try:
            rel = p.relative_to(root)
        except Exception:
            rel = p
        try:
            age = (time.time() - p.stat().st_mtime) / 86400
            when = f"{age:.0f}d ago" if age < 3650 else "long ago"
        except Exception:
            when = "?"
        lines.append(f"  {size / (1024 * 1024):.1f} MB  {rel}  (touched {when})")
    return "\n".join(lines)


# ── dispatcher ────────────────────────────────────────────────────────────────

_SCANS = {
    "system": scan_system,
    "network": scan_network,
    "ports": scan_ports,
    "files": scan_files,
}


def scan_action(parameters: dict = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    what = (params.get("what") or params.get("action") or "system").strip().lower()
    target = (params.get("path") or "").strip()

    _log(f"{what}{(' ' + target) if target else ''}", player)

    fn = _SCANS.get(what)
    if fn is None:
        return (f"I can scan: {', '.join(sorted(_SCANS))}. "
                f"'{what}' is not one of them.")

    try:
        if what == "files":
            return fn(target, int(params.get("top", 12) or 12))
        return fn()
    except Exception as e:
        msg = f"The {what} scan failed: {e}"
        _log(msg, player)
        return msg


# ── Tool declaration (auto-discovered by core/action_loader.py) ───────────────
TOOL = {
    "name": "scan",
    "description": (
        "Read-only scan of the machine and its surroundings. Use for: 'what is "
        "my computer doing', 'how much disk space is left', 'what is running', "
        "'scan my network', 'what ports are open', 'what is taking up space', "
        "'find my biggest files'. Reports facts about this computer and its "
        "network - it never changes anything and never needs an API key."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "what": {
                "type": "STRING",
                "description": "What to scan: system | network | ports | files. Default: system.",
            },
            "path": {
                "type": "STRING",
                "description": "For what='files': folder to inspect. Default: your home folder.",
            },
            "top": {
                "type": "STRING",
                "description": "For what='files': how many results. Default: 12.",
            },
        },
        "required": [],
    },
    "handler": scan_action,
}
