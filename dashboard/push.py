"""
push — Web Push plumbing for the JARVIS dashboard PWA (Report #1).

  * VAPID keypair   — EC P-256, generated ONCE with `cryptography`
                      (already a dependency), stored under
                      <base>/config/vapid_{pub,priv}.pem. No py_vapid.
  * subscribe()     — store browser PushSubscription JSON (endpoint +
                      p256dh/auth), deduped by endpoint, capped at 20.
  * notify()        — RFC 8291 aes128gcm payload encryption + ES256
                      VAPID JWT, POSTed with urllib. Implemented on the
                      standard library + cryptography only — NO new
                      dependencies, nothing faked: failures are
                      collected and reported per endpoint.

The browser side lives in dashboard/static/sw.js + pwa.js; icons and
manifest make the dashboard installable.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_VAPID_TTL = 12 * 3600
_SUBS_CAP = 20


def _base_dir() -> Path:
    from config import get_base_dir
    return get_base_dir()


def _subs_path() -> Path:
    return _base_dir() / "config" / "push_subs.json"


# ── VAPID ───────────────────────────────────────────────────────────────────

def _vapid_pair() -> tuple[ec.EllipticCurvePrivateKey, Path, Path]:
    """Load or create the P-256 keypair. Returns (priv, pub_pem, priv_pem)."""
    cfg = _base_dir() / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    pub_pem, priv_pem = cfg / "vapid_pub.pem", cfg / "vapid_priv.pem"
    if priv_pem.is_file() and pub_pem.is_file():
        priv = serialization.load_pem_private_key(
            priv_pem.read_bytes(), password=None)
        if not isinstance(priv, ec.EllipticCurvePrivateKey):
            # The file is ours, so this only happens if it was replaced by hand.
            # Regenerate rather than hand a wrong key type to the signer.
            raise ValueError(f"{priv_pem} is not an EC private key")
        return priv, pub_pem, priv_pem
    priv = ec.generate_private_key(ec.SECP256R1())
    priv_pem.write_bytes(priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    pub_pem.write_bytes(priv.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub_pem, priv_pem


def public_key_b64() -> str:
    """Uncompressed point (0x04‖X‖Y), base64url NO padding — what the
    browser's pushManager.subscribe() expects."""
    priv, _, _ = _vapid_pair()
    nums = priv.public_key().public_numbers()
    raw = b"\x04" + nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ── subscriptions ───────────────────────────────────────────────────────────

def _load_subs() -> list[dict]:
    try:
        data = json.loads(_subs_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def subscribe(sub: dict) -> dict:
    """Store a PushSubscription (endpoint/keys). Deduped by endpoint."""
    if not isinstance(sub, dict) or not str(sub.get("endpoint", "")):
        raise ValueError("subscription needs an endpoint")
    subs = [s for s in _load_subs()
            if s.get("endpoint") != sub.get("endpoint")]
    subs.append(sub)
    subs = subs[-_SUBS_CAP:]
    p = _subs_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(subs, indent=1), encoding="utf-8")
    return {"stored": len(subs)}


def unsubscribe(endpoint: str) -> dict:
    subs = [s for s in _load_subs() if s.get("endpoint") != endpoint]
    _subs_path().write_text(json.dumps(subs, indent=1), encoding="utf-8")
    return {"stored": len(subs)}


# ── crypto (RFC 8291 aes128gcm + VAPID ES256) ───────────────────────────────

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64u(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _encrypt_payload(p256dh_b64: str, auth_b64: str, plaintext: bytes,
                     salt: bytes | None = None) -> bytes:
    """RFC 8291 aes128gcm: salt(16) ‖ rs(2) ‖ idlen(1) ‖ keyid ‖ ct."""
    ua_pub = _unb64u(p256dh_b64)
    auth = _unb64u(auth_b64)
    if len(ua_pub) != 65 or ua_pub[0] != 4:
        raise ValueError("bad subscriber p256dh key")
    if len(auth) != 16:
        raise ValueError("bad subscriber auth secret")
    salt = salt or os.urandom(16)
    # ephemeral ECDH pair
    eph = ec.generate_private_key(ec.SECP256R1())
    _en = eph.public_key().public_numbers()
    eph_pub = (b"\x04" + _en.x.to_bytes(32, "big")
               + _en.y.to_bytes(32, "big"))       # uncompressed, 65 B
    peer = ec.EllipticCurvePublicNumbers(
        int.from_bytes(ua_pub[1:33], "big"),
        int.from_bytes(ua_pub[33:], "big"),
        ec.SECP256R1()).public_key()
    prk = HKDF(algorithm=hashes.SHA256(), length=32, salt=auth,
               info=b"WebPush: info\x00" + ua_pub + eph_pub
               ).derive(eph.exchange(ec.ECDH(), peer))
    cek = HKDF(algorithm=hashes.SHA256(), length=16, salt=salt,
               info=b"Content-Encoding: aes128gcm\x00").derive(prk)
    nonce = HKDF(algorithm=hashes.SHA256(), length=12, salt=salt,
                 info=b"Content-Encoding: nonce\x00").derive(prk)
    # RFC 8188 single record, zero-length padding: plaintext ‖ 0x01
    body = plaintext + b"\x01"
    ct = AESGCM(cek).encrypt(nonce, body, None)
    idlen = len(eph_pub)
    return salt + (8426).to_bytes(2, "big") + bytes([idlen]) + eph_pub + ct


def _vapid_jwt(aud: str, sub: str) -> str:
    priv, _, _ = _vapid_pair()
    header = _b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = _b64u(json.dumps({
        "aud": aud, "exp": int(time.time()) + _VAPID_TTL, "sub": sub,
    }).encode())
    signing = f"{header}.{claims}".encode()
    der = priv.sign(signing, ec.ECDSA(hashes.SHA256()))
    # DER → raw r‖s (64 bytes) for JWT ES256
    r, s = _der_to_rs(der)
    sig = _b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"{header}.{claims}.{sig}"


def _der_to_rs(der: bytes) -> tuple[int, int]:
    from cryptography.hazmat.primitives.asymmetric.utils import \
        decode_dss_signature
    r, s = decode_dss_signature(der)
    return r, s


def _post_push(endpoint: str, headers: dict, body: bytes,
               timeout: int = 15) -> tuple[int, str]:
    """POST the encrypted payload. Returns (status, tail). Seam."""
    req = urllib.request.Request(endpoint, data=body, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), ""
    except urllib.error.HTTPError as e:
        return int(e.code), (e.read().decode(errors="replace")[:200]
                             if hasattr(e, "read") else str(e))
    except Exception as e:
        return 0, str(e)[:200]


# ── notify ──────────────────────────────────────────────────────────────────

def notify(title: str, body: str, ttl: int = 60) -> dict:
    """Fan out a web push to every stored subscription. Honest per-
    endpoint results (410/404 drop the dead sub)."""
    subs = _load_subs()
    if not subs:
        return {"sent": 0, "total": 0,
                "note": "no subscriptions — open the installed "
                        "dashboard and allow notifications first."}
    payload = json.dumps({"title": str(title)[:120],
                          "body": str(body)[:400],
                          "url": "/"}).encode("utf-8")
    sent, errors, keep = 0, [], []
    for sub in subs:
        endpoint = str(sub.get("endpoint", ""))
        try:
            p256dh = sub.get("keys", {}).get("p256dh", "")
            auth = sub.get("keys", {}).get("auth", "")
            ct = _encrypt_payload(p256dh, auth, payload)
            from urllib.parse import urlparse
            jwt = _vapid_jwt(urlparse(endpoint).scheme + "://"
                             + urlparse(endpoint).netloc,
                             "mailto:jarvis@localhost")
            headers = {
                "Content-Type": "application/octet-stream",
                "Content-Encoding": "aes128gcm",
                "TTL": str(ttl),
                "Authorization": f"vapid t={jwt}, k={public_key_b64()}",
                "Urgency": "normal",
            }
            status, tail = _post_push(endpoint, headers, ct)
            if 200 <= status < 300:
                sent += 1
                keep.append(sub)
            elif status in (404, 410):
                errors.append(f"{endpoint[:60]}: gone ({status}) — dropped")
            else:
                errors.append(f"{endpoint[:60]}: HTTP {status} {tail}")
                keep.append(sub)
        except Exception as e:
            errors.append(f"{endpoint[:60]}: {e}")
    if keep != subs:
        _subs_path().write_text(json.dumps(keep, indent=1),
                                encoding="utf-8")
    return {"sent": sent, "total": len(subs), "errors": errors[:6]}
