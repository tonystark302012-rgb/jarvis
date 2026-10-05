"""
dashboard/server.py — JARVIS Local HTTP Dashboard

TLS first: config/certs holds a locally generated self-signed pair (created on
first run — nothing private ships in the repo) and BOTH ports serve HTTPS with
it. Plain HTTP on port 8000 is only the fallback when `cryptography` is not
installed, and it says so loudly — the PIN login then crosses the LAN in the
clear, so the fallback warns before it binds 0.0.0.0.

Security at the application layer: AES-256-CBC with a session-key-derived key
(the PIN never becomes key material), one-time login keys, per-IP lockout.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import re
import json
import secrets
import socket
import string
import time
from pathlib import Path

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key.

    This is only safe because `session_key` is now a server-generated 256-bit
    random value, not the six-character PIN the user types. It used to be the
    PIN: 31**6 ≈ 8.9e8 possibilities, one unsalted hash round, so a captured
    ciphertext fell to an offline GPU sweep in roughly a tenth of a second.

    The fix was not a slower KDF - it was giving the key 256 bits of entropy and
    keeping the PIN for authentication only, where a wrong guess is rejected by
    the server (and now rate-limited) instead of being checkable offline.
    """
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys
    import subprocess
    import os
    import tempfile
    import threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes
        import time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print("[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _ensure_certs() -> bool:
    """
    Make sure config/certs holds a TLS key pair, generating a self-signed one the
    first time the dashboard runs.

    The pair is deliberately NOT shipped in the repository. A private key that
    every user downloads is the same as having no private key at all: anyone can
    present a certificate that matches it. Generating locally gives each install
    its own key, costs about a second, and happens exactly once.

    Returns True when a usable pair exists afterwards; False leaves the caller on
    plain HTTP, which still works — the QR code simply encodes http:// instead.
    """
    certs = BASE_DIR / "config" / "certs"
    key_p = certs / "jarvis.key"
    crt_p = certs / "jarvis.crt"
    if key_p.exists() and crt_p.exists():
        return True

    try:
        import datetime
        import ipaddress
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        print("[Dashboard] cryptography not installed — serving over plain HTTP.")
        print("[Dashboard] For HTTPS run:  pip install cryptography")
        return False

    try:
        certs.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        who = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS Dashboard"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JARVIS"),
        ])

        # The SAN has to cover every address the phone might use: the LAN IP the
        # QR code encodes, plus localhost when testing on the machine itself.
        alt = [x509.DNSName("localhost"),
               x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
        try:
            lan = _local_ip()
            if not lan.startswith("127."):
                alt.append(x509.IPAddress(ipaddress.IPv4Address(lan)))
        except Exception:
            pass          # no LAN address resolvable — localhost entries still work

        # Timezone-aware UTC: datetime.utcnow() is deprecated from Python 3.12 on,
        # and the builder normalises aware values to UTC itself.
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(who)
            .issuer_name(who)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )

        key_p.write_bytes(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))
        crt_p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

        try:
            import os as _os
            _os.chmod(key_p, 0o600)   # best effort — largely a no-op on Windows
        except Exception:
            pass

        print(f"[Dashboard] Generated a self-signed certificate for this machine: {certs}")
        return True
    except Exception as e:
        print(f"[Dashboard] Certificate generation failed ({e}) — serving over plain HTTP.")
        return False


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

# The one live server instance — tools (actions/screen_mirror.py) reach the
# existing mirror through this instead of growing a second capture stack.
ACTIVE: "DashboardServer | None" = None


class DashboardServer:

    def __init__(self):
        global ACTIVE
        ACTIVE = self
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ip                          = _local_ip()
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._clients: set[WebSocket]     = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._pending_keys: dict[str, float] = {}
        # Encryption key per token. Random, 256-bit, generated here - the PIN the
        # user types never becomes key material (see _derive_key).
        self._token_enckey: dict[str, str]   = {}
        # auth-token -> encryption key, and rate-limit state for /login
        self._login_fails: dict[str, list]   = {}   # ip -> [timestamps]
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=200)
        # Screen mirror: phone can ask the PC to stream downscaled frames.
        self._mirror_on: bool                = False
        self._mirror_task                     = None
        # Camera motion detect — stateful frame differ (see dashboard/motion.py)
        from dashboard.motion import MotionDetector
        self._motion                          = MotionDetector()
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    # ── brute-force guard ─────────────────────────────────────────────────
    # Six-character keys over a 31-character alphabet are ~8.9e8 combinations.
    # That is comfortable against a human guessing by hand and worthless against
    # an unthrottled script on the same LAN - and this server binds 0.0.0.0 and
    # opens its own firewall rule. So failures are counted per source address
    # and the address is locked out after _LOGIN_MAX_FAILS.
    _LOGIN_MAX_FAILS  = 8
    _LOGIN_WINDOW     = 300.0    # failures older than this stop counting
    _LOGIN_LOCKOUT    = 600.0    # seconds a locked-out address stays locked

    def _rate_limited(self, ip: str) -> bool:
        """True when `ip` has failed too often and is still inside its lockout."""
        now  = time.time()
        hits = [t for t in self._login_fails.get(ip, []) if now - t < self._LOGIN_WINDOW]
        if hits:
            self._login_fails[ip] = hits
        else:
            self._login_fails.pop(ip, None)
        if len(hits) >= self._LOGIN_MAX_FAILS:
            # still locked out until the newest failure ages out of the lockout
            return now - hits[-1] < self._LOGIN_LOCKOUT
        return False

    def _note_login_fail(self, ip: str) -> None:
        self._login_fails.setdefault(ip, []).append(time.time())

    def _new_enc_key(self, tok: str) -> str:
        """Mint a random 256-bit encryption key for this session and cache it."""
        enc = secrets.token_hex(32)
        self._token_enckey[tok] = enc
        self._aes_key(enc)                 # pre-derive & cache
        return enc

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    def ui_session(self) -> str:
        """Mint a long-lived bearer token for the desktop HUD's workspace
        panels (Spaces/Agents/Computers/Calls/Agenda). Same process, loopback
        only — the token dies with the server, never written to disk."""
        tok = secrets.token_urlsafe(32)
        self._tokens.add(tok)
        self._token_keys[tok] = "hud"
        return tok

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        # Key material is the per-token random `enc` (see _new_enc_key), NOT the
        # PIN in _token_keys. The client encrypts with SHA256(enc‖salt) — using
        # the PIN here made every encrypted command fail to decrypt (the phone
        # dashboard could never deliver a command while ENC was on).
        sk = self._token_enckey.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict, history: bool = True) -> None:
        if history:
            self._history.append(msg)
            if len(self._history) > 300:
                self._history = self._history[-300:]
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    # ── screen mirror ────────────────────────────────────────────────────

    async def mirror_set(self, on: bool) -> None:
        """Toggle the PC→phone screen stream. Frames are low-res JPEGs sent
        as base64; activity/mirror traffic never enters the replay history.
        The state message goes out BEFORE the capture task starts so a
        client's first frame of messages is deterministic."""
        on = bool(on)
        if on == self._mirror_on:
            return
        self._mirror_on = on
        await self.broadcast({"type": "sys",
                              "text": "Screen mirror ON." if on else
                                      "Screen mirror OFF."})
        if on and (self._mirror_task is None or self._mirror_task.done()):
            self._mirror_task = asyncio.create_task(self._mirror_loop())

    @property
    def mirror_on(self) -> bool:
        """Public read of the mirror toggle (GIL-safe bool)."""
        return bool(self._mirror_on)

    def mirror_set_sync(self, on: bool, timeout: float = 5.0) -> None:
        """Thread-safe toggle for tools running OFF this server's loop
        (action handlers execute in an executor thread). Raises
        RuntimeError when the dashboard loop isn't up yet — callers
        surface that honestly instead of pretending to toggle."""
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("dashboard loop not ready")
        fut = asyncio.run_coroutine_threadsafe(
            self.mirror_set(bool(on)), loop)
        fut.result(timeout)

    async def _mirror_loop(self) -> None:
        """~3 fps while enabled. Every failure path stops the stream with a
        message instead of silently spinning (headless/no PIL/no display)."""
        import base64
        import io as _io
        import time as _t
        try:
            from PIL import ImageGrab
        except Exception as e:
            await self.broadcast({"type": "sys",
                                  "text": f"Screen mirror unavailable ({e})."})
            self._mirror_on = False
            return
        sent = 0
        while self._mirror_on:
            t0 = _t.monotonic()
            try:
                img = ImageGrab.grab()
                w, h = img.size
                if w > 960:
                    img = img.resize((960, max(1, int(h * 960 / w))))
                buf = _io.BytesIO()
                img.save(buf, format="JPEG", quality=45)
                b64 = base64.b64encode(buf.getvalue()).decode()
                await self.broadcast({"type": "frame", "data": b64},
                                     history=False)
                sent += 1
            except Exception as e:
                # ImageGrab raises on headless/locked sessions — stop cleanly.
                await self.broadcast({"type": "sys",
                                      "text": f"Screen mirror stopped ({e})."})
                self._mirror_on = False
                return
            elapsed = _t.monotonic() - t0
            await asyncio.sleep(max(0.05, 0.33 - elapsed))
        # loop exited because toggle went off — nothing to clean

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            # Header first; ?token= fallback for <img>/<a download> —
            # they cannot send custom headers (same rule /uploads uses).
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if not tok:
                tok = str(req.query_params.get("token") or "").strip()
            return bool(tok) and tok in self._tokens

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            ip = (req.client.host if req.client else "unknown") or "unknown"
            if self._rate_limited(ip):
                return JSONResponse(
                    {"ok": False,
                     "error": "Too many incorrect keys. Wait a few minutes and "
                              "generate a new one in JARVIS."},
                    status_code=429)

            body    = await req.json()
            entered = str(body.get("pin", "")).strip().upper()
            now     = time.time()
            if entered in self._pending_keys and self._pending_keys[entered] > now:
                del self._pending_keys[entered]          # one-time use
                self._login_fails.pop(ip, None)          # clean slate on success
                tok = secrets.token_urlsafe(32)
                self._tokens.add(tok)
                self._token_keys[tok] = entered          # kept for bookkeeping
                enc = self._new_enc_key(tok)             # real key material
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                # `enc` is 256 random bits; the PIN never becomes key material.
                return JSONResponse({"ok": True, "token": tok, "enc": enc})

            self._note_login_fail(ip)
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()
            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = secrets.token_urlsafe(32)
            dev_tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = key          # bookkeeping only
            enc = self._new_enc_key(tok)         # 256 random bits = real key material
            self._device_sessions[dev_tok] = {"session_key": key}
            enc_js = json.dumps(enc)          # safe to embed: hex, no quotes

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key',{enc_js!r});
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = secrets.token_urlsafe(32)
            self._tokens.add(tok)
            self._token_keys[tok] = session_key
            enc = self._new_enc_key(tok)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            # `enc` is random; `session_key` (the original PIN) is not returned -
            # it is auth material, and must never become encryption material.
            return JSONResponse({"ok": True, "token": tok, "key": enc})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone camera → PC vision ─────────────────────────────────────────
        # The phone streams JPEG frames here; the latest one is kept on disk
        # for the phone_vision action to read. One file, overwritten each time
        # — history of what your desk looked like is not the point, the CURRENT
        # view is. Body is JSON {"frame": "<base64 jpeg>", "ts": epoch} —
        # no multipart, so the endpoint also works without python-multipart.

        @app.post("/api/camera-frame")
        async def camera_frame(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"error": "Bad JSON"}, status_code=400)
            import base64 as _b64
            import binascii
            raw = str(body.get("frame") or "")
            if "," in raw and raw.strip().startswith("data:"):
                raw = raw.split(",", 1)[1]           # data:image/jpeg;base64,…
            try:
                data = _b64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError):
                return JSONResponse({"error": "Bad base64"},
                                    status_code=400)
            if not (100 <= len(data) <= 4 * 1024 * 1024):
                return JSONResponse(
                    {"error": "Frame must be 100 B – 4 MB"},
                    status_code=413)
            cam_dir = self._uploads_dir / "camera"
            try:
                cam_dir.mkdir(parents=True, exist_ok=True)
                (cam_dir / "frame.jpg").write_bytes(data)
                (cam_dir / "frame.json").write_text(
                    json.dumps({"ts": float(body.get("ts") or time.time()),
                                "size": len(data)}),
                    encoding="utf-8")
            except Exception as exc:
                return JSONResponse({"error": str(exc)}, status_code=500)
            asyncio.create_task(self.broadcast({
                "type": "camera_frame",
                "size": len(data),
                "ts": float(body.get("ts") or time.time()),
            }, history=False))
            # Motion detect: frame-over-frame diff (dashboard/motion.py).
            # feed() never raises and degrades to None without PIL.
            motion = self._motion.feed(data, time.time())
            if motion:
                asyncio.create_task(self.broadcast(motion, history=False))
            return JSONResponse({"ok": True, "size": len(data),
                                 "motion": bool(motion)})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        pass  # drop frame rather than block
            except WebSocketDisconnect:
                pass
            finally:
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        # ── PWA: installable manifest + service worker + web push ────────
        @app.get("/manifest.json")
        async def pwa_manifest():
            return FileResponse(str(STATIC_DIR / "manifest.json"),
                                media_type="application/manifest+json")

        @app.get("/sw.js")
        async def pwa_sw():
            resp = FileResponse(str(STATIC_DIR / "sw.js"),
                                media_type="application/javascript")
            resp.headers["Service-Worker-Allowed"] = "/"
            resp.headers["Cache-Control"] = "no-cache"
            return resp

        @app.get("/api/push/public-key")
        async def push_public_key(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                from dashboard import push as push_mod
                return JSONResponse({"key": push_mod.public_key_b64()})
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.post("/api/push/subscribe")
        async def push_subscribe(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
                from dashboard import push as push_mod
                return JSONResponse(push_mod.subscribe(body))
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.post("/api/push/notify")
        async def push_notify(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            try:
                body = await req.json()
                from dashboard import push as push_mod
                out = push_mod.notify(str(body.get("title", "JARVIS")),
                                      str(body.get("body", "")))
                return JSONResponse(out)
            except Exception as e:
                return JSONResponse({"error": str(e)[:200]}, status_code=500)

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or tok not in self._tokens:
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or tok not in self._tokens:
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    if data.get("type") == "mirror":
                        await self.mirror_set(bool(data.get("on")))
                    elif data.get("type") == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        # ── Dots workspace (specialist agents, pages, approvals, memory)
        # Redesigned UI assets: css/ + js/ (+ crypto.js falls through to
        # the explicit route above, registered first). Registered before
        # the dots router include so route order stays deterministic.
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)),
                  name="static")

        # mounted on THIS app: one server, one login, one origin — the
        # routes ride the same bearer-token auth as the rest of the API.
        try:
            from fastapi import Depends, HTTPException as _HE
            from dots.server import router as _dots_router

            def _dots_auth(req: Request) -> None:
                if not _auth(req):
                    raise _HE(status_code=401, detail="Not authenticated")

            app.include_router(_dots_router, dependencies=[Depends(_dots_auth)])
        except Exception as _e:
            print(f"[Dots] routes disabled: {_e}")

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_running_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_running_loop().run_in_executor(None, _ensure_network_access, PORT)

        # Generate the TLS pair on first run so no private key ships in the repo.
        _ensure_certs()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())
        else:
            print("[Dashboard] ⚠️  No TLS — serving PLAIN HTTP on the LAN.")
            print("[Dashboard]    The login PIN will be visible to anyone on")
            print("[Dashboard]    this network. For HTTPS: pip install cryptography")

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
