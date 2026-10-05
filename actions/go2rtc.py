"""
go2rtc — RTSP/IP-camera stream bridge (Report #1 / go2rtc, MIT).

JARVIS's built-in camera understands the LOCAL webcam. go2rtc adds the
missing piece: RTSP/ONVIF IP cameras, phone streams, NVRs — bridged to
browser-playable WebRTC/HLS at localhost:1984, with JARVIS managing
the process and its stream list.

Guarded, honest, free:
  * binary = system `go2rtc`, else `<base>/bin/go2rtc`
  * `action=install` downloads the OFFICIAL release tarball
    (github.com/AlexxIT/go2rtc, MIT) — explicit, no silent installs
  * start/stop/status with readiness polling; every failure says why

WHY NOT A DUPLICATE: youtube_video plays web videos; video_qa reads
files; the HUD camera is a local device. This is LIVE IP streams.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

_STATE: dict = {"proc": None, "port": 1984, "url": "",
                "streams": [], "error": ""}
_API = ("https://api.github.com/repos/AlexxIT/go2rtc/releases/latest")
_TARBALL_KEY = "linux_amd64"         # official static build name


# ── seams (tests replace these) ────────────────────────────────────────────

def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _bin_dir() -> Path:
    d = _base_dir() / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _binary() -> str | None:
    exe = shutil.which("go2rtc")
    if exe:
        return exe
    local = _bin_dir() / "go2rtc"
    if local.is_file():
        return str(local)
    return None


def _fetch_json(url: str, timeout: int = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url: str, dest: Path, timeout: int = 120) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis"})
    with urllib.request.urlopen(req, timeout=timeout) as r, \
            open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)
    return dest


def _install() -> str:
    """Explicit go2rtc install: official release → <base>/bin/go2rtc."""
    try:
        rel = _fetch_json(_API)
        assets = rel.get("assets") or []
        want = next((a for a in assets
                     if _TARBALL_KEY in str(a.get("name", ""))
                     and str(a.get("name", "")).endswith((".tar.gz", ".tgz"))),
                    None)
        if not want:
            return (f"Go: no {_TARBALL_KEY} asset in latest release — "
                    "download manually from github.com/AlexxIT/go2rtc/releases")
        with tempfile.TemporaryDirectory() as td:
            tgz = Path(td) / "go2rtc.tar.gz"
            _download(want["browser_download_url"], tgz)
            with tarfile.open(tgz, "r:gz") as tar:
                member = next((m for m in tar.getmembers()
                               if m.name.endswith("go2rtc")), None)
                if member is None:
                    return "Tarball had no go2rtc binary — aborting, nothing installed."
                try:
                    tar.extract(member, path=td, filter="data")
                except TypeError:          # python < 3.12
                    tar.extract(member, path=td)
                src = Path(td) / member.name
                dest = _bin_dir() / "go2rtc"
                shutil.copyfile(str(src), str(dest))
                dest.chmod(0o755)
        return f"go2rtc installed: {dest} (release {rel.get('tag_name', '?')})"
    except Exception as e:
        return (f"go2rtc install failed: {e} — get the free MIT binary "
                "manually: github.com/AlexxIT/go2rtc/releases")


# ── config / process ────────────────────────────────────────────────────────

def _config_text(streams: list[str]) -> str:
    """go2rtc.yaml from 'name=url' entries (pure — tested)."""
    lines = ["streams:"]
    for spec in streams:
        if "=" in spec:
            name, url = spec.split("=", 1)
        else:
            name, url = spec, spec
        lines.append(f"  {name.strip()}: {url.strip()}")
    return "\n".join(lines) + "\n"


def _spawn(binary: str, cfg: Path, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [binary, "-config", str(cfg), "-listen", f"0.0.0.0:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _port_ready(port: int, host: str = "127.0.0.1",
                timeout: float = 8.0) -> bool:
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _parse_streams(raw) -> list[str]:
    """Accept a list, a comma string, or nothing."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


# ── tool ────────────────────────────────────────────────────────────────────

def go2rtc(parameters: dict = None, player=None,
           session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()
    proc = _STATE.get("proc")

    if action in ("install", "setup"):
        return _install()

    if action == "start":
        if proc is not None and getattr(proc, "poll", lambda: 1)() is None:
            return f"go2rtc already running: {_STATE['url']}"
        binary = _binary()
        if not binary:
            return ("go2rtc binary not found — free MIT install: "
                    "go2rtc action=install (official release) or get it "
                    "from github.com/AlexxIT/go2rtc/releases. I never "
                    "fake a stream server.")
        streams = _parse_streams(params.get("streams"))
        if not streams:
            return ("Give me streams — go2rtc action=start "
                    "streams='cam1=rtsp://user:pass@192.168.1.10/stream' "
                    "(comma-separate multiple).")
        port = int(params.get("port", 1984) or 1984)
        cfg = _base_dir() / "go2rtc.yaml"
        cfg.write_text(_config_text(streams), encoding="utf-8")
        try:
            proc = _spawn(binary, cfg, port)
        except Exception as e:
            _STATE["error"] = str(e)[:200]
            return f"go2rtc failed to spawn: {e}"
        ready = _port_ready(port)
        _STATE.update({"proc": proc, "port": port, "streams": streams,
                       "url": f"http://127.0.0.1:{port}",
                       "error": "" if ready else "port not ready"})
        if not ready:
            err = ""
            try:
                if proc.stderr:
                    err = proc.stderr.read().decode(errors="replace")[-300:]
            except Exception:
                pass
            return (f"go2rtc started but port {port} never opened"
                    + (f" — {err}" if err else "") + ".")
        return (f"go2rtc running: {_STATE['url']} — "
                f"{len(streams)} stream(s) configured "
                f"(config {cfg}). Open {_STATE['url']} for the player UI.")

    if action == "stop":
        if proc is None or getattr(proc, "poll", lambda: 1)() is not None:
            _STATE.update({"proc": None, "url": ""})
            return "go2rtc was not running."
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        finally:
            _STATE.update({"proc": None, "url": ""})
        return "go2rtc stopped."

    # status
    running = proc is not None and getattr(proc, "poll", lambda: 1)() is None
    if running:
        return (f"go2rtc RUNNING at {_STATE['url']} — "
                f"streams: {', '.join(map(str, _STATE['streams'])) or 'none'}.")
    binary = _binary()
    head = f"go2rtc not running (binary: {binary or 'MISSING'})."
    if not binary:
        return head + " Install free: go2rtc action=install."
    return head + " Start: go2rtc action=start streams='name=rtsp://…'."


TOOL = {
    "name": "go2rtc",
    "description": (
        "Manage go2rtc — the MIT RTSP/ONVIF IP-camera bridge (browser-"
        "playable WebRTC). action=status (default), action=install "
        "(downloads the official release to <base>/bin), action=start "
        "with streams='name=rtsp://…' (comma-separated) + optional "
        "port, action=stop. Use for 'add/stream my IP camera', 'rtsp "
        "feed', NVR/ONVIF questions. Local webcam needs none of this "
        "(the HUD camera already covers it)."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "status | install | start | stop."},
            "streams": {"type": "STRING",
                        "description": "name=url pairs, comma-separated."},
            "port": {"type": "INTEGER",
                     "description": "Listen port (default 1984)."},
        },
        "required": [],
    },
    "handler": go2rtc,
}
