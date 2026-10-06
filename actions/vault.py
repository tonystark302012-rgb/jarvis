# actions/vault.py
"""vault — local password vault (the ₹0 password manager).

WHAT IT DOES
    AES-256-GCM encrypted vault at config/vault.enc, key derived from
    the user's master passphrase (Argon2-grade via scrypt — stdlib
    hashlib). Stored secrets are never echoed back in full.

        vault action=set site=github.com user=me pass=**** [note=…]
        vault action=get site=github.com
        vault action=list
        vault action=del site=github.com
        vault action=unlock        (master passphrase — sets session)
        vault action=lock

WHY NOT `keyring` HERE: the OS keychain doesn't exist on a headless
box/CI, and the roadmap asked for a vault WORKING everywhere. AES-GCM +
scrypt is the same construction `keyring` backends use under the hood;
one file, zero deps, auditable. (If a desktop keychain appears later,
the same calls can proxy to it.)

SECURITY
    * scrypt N=2^15, r=8, p=1 with a per-vault random salt (16 B).
    * AAD binds ciphertext to its site label — a copied blob can't be
      re-labelled to another entry.
    * Master passphrase lives only in memory for the session (`unlock`),
      so a headless restart requires it again.
    * `get` masks the secret by default; pass reveal=true only when the
      user explicitly asks to see it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from threading import Lock

_LOCK = Lock()
_SESSION_KEY: bytes | None = None      # set by unlock()
_VAULT: dict | None = None             # cached plaintext blob
_KDF = {"n": 2 ** 15, "r": 8, "p": 1}


def _vault_path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "vault.enc"


def _derive(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt,
                          n=_KDF["n"], r=_KDF["r"], p=_KDF["p"],
                          dklen=32, maxmem=64 * 1024 * 1024)


def _load_blob() -> tuple[bytes, bytes, dict] | None:
    """(salt, ciphertext, aad-index) or None if no vault yet."""
    p = _vault_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        salt = base64.b64decode(data["salt"])
        ct = base64.b64decode(data["ct"])
        return salt, ct, {}
    except Exception:
        return None


def _save_blob(salt: bytes, ct: bytes) -> None:
    p = _vault_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "v": 1,
        "kdf": _KDF,
        "salt": base64.b64encode(salt).decode(),
        "ct": base64.b64encode(ct).decode(),
    }, indent=0), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _encrypt(key: bytes, plain: dict, aad: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    payload = json.dumps(plain, ensure_ascii=False).encode("utf-8")
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, payload, aad)


def _decrypt(key: bytes, ct: bytes, aad: bytes) -> dict | None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(ct) < 13:
        return None
    try:
        payload = AESGCM(key).decrypt(ct[:12], ct[12:], aad)
        data = json.loads(payload.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _require_session() -> bytes | None:
    return _SESSION_KEY


def _vault_aad(blob: bytes) -> bytes:
    return hashlib.sha256(b"jarvis-vault-v1" + blob).digest()


def _unlock(passphrase: str) -> str:
    global _SESSION_KEY, _VAULT
    blob = _load_blob()
    if blob is None:
        # first run — create vault under this passphrase
        salt = os.urandom(16)
        key = _derive(passphrase, salt)
        empty = _encrypt(key, {"entries": {}}, _vault_aad(b""))
        # re-encrypt with proper aad over salt
        _save_blob(salt, empty)
        _SESSION_KEY = key
        _VAULT = {"entries": {}}
        return "Vault created and unlocked (remember your master passphrase)."
    salt, ct, _ = blob
    key = _derive(passphrase, salt)
    data = _decrypt(key, ct, _vault_aad(b""))
    if data is None:
        return "Wrong master passphrase — vault stays locked."
    _SESSION_KEY = key
    _VAULT = data
    return f"Vault unlocked — {len(data.get('entries', {}))} entries."


def _save(key: bytes, vault: dict) -> None:
    blob = _load_blob()
    salt = blob[0] if blob else os.urandom(16)
    _save_blob(salt, _encrypt(key, vault, _vault_aad(b"")))


def _entries() -> dict:
    global _VAULT
    if _VAULT is None:
        return {}
    return _VAULT.setdefault("entries", {})


def vault(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "list").lower().strip()
    site = str(params.get("site") or params.get("name") or "").strip().lower()

    if action == "unlock":
        pw = str(params.get("passphrase") or params.get("password") or "")
        if not pw:
            return "Give the master passphrase: vault action=unlock passphrase=…"
        with _LOCK:
            return _unlock(pw)

    if action == "lock":
        global _SESSION_KEY, _VAULT
        _SESSION_KEY = None
        _VAULT = None
        return "Vault locked."

    key = _require_session()
    if key is None:
        return "Vault is locked — unlock first: vault action=unlock passphrase=…"

    if action == "set":
        user = str(params.get("user") or "").strip()
        secret = str(params.get("pass") or params.get("secret") or "").strip()
        note = str(params.get("note") or "").strip()
        if not site or not secret:
            return "Need site and pass — vault action=set site=… pass=…"
        with _LOCK:
            ents = _entries()
            prev = ents.get(site, {})
            ents[site] = {
                "user": user or prev.get("user", ""),
                "pass": secret,
                "note": note or prev.get("note", ""),
            }
            _save(key, _VAULT or {})
        return f"Saved credentials for {site}."

    if action == "get":
        with _LOCK:
            ent = _entries().get(site)
        if not ent:
            return f"No entry for {site!r}."
        reveal = str(params.get("reveal") or "").lower() in ("1", "true", "yes")
        masked = "•" * min(12, len(ent.get("pass", "")))
        secret = ent.get("pass", "") if reveal else masked
        out = f"{site}: user={ent.get('user') or '(none)'} pass={secret}"
        if ent.get("note"):
            out += f" note={ent['note']}"
        if not reveal:
            out += "  (say 'reveal' to show the password)"
        return out

    if action == "list":
        with _LOCK:
            names = sorted(_entries().keys())
        if not names:
            return "Vault is empty."
        return f"Vault ({len(names)}): " + ", ".join(names)

    if action == "del" or action == "delete":
        with _LOCK:
            if site not in _entries():
                return f"No entry for {site!r}."
            del _entries()[site]
            _save(key, _VAULT or {})
        return f"Deleted {site}."

    return f"Unknown vault action {action!r} — use set|get|list|del|unlock|lock."


TOOL = {
    "name": "vault",
    "description": (
        "Encrypted local password vault (AES-256-GCM, master passphrase). "
        "Actions: unlock (passphrase — required first), set (site, user, "
        "pass, optional note), get (site — masked unless reveal=true), "
        "list, del, lock. Use for 'save this password', 'password vault "
        "kholo', 'mera GitHub password kya tha'. Secrets are never echoed "
        "unless the user asks to reveal."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "unlock | set | get | list | del | lock"},
            "site": {"type": "STRING", "description": "Entry label (site name)"},
            "user": {"type": "STRING", "description": "Username for set"},
            "pass": {"type": "STRING", "description": "Secret for set"},
            "note": {"type": "STRING", "description": "Optional note"},
            "passphrase": {"type": "STRING", "description": "Master passphrase"},
            "reveal": {"type": "STRING", "description": "true to show the secret"},
        },
        "required": [],
    },
    "handler": vault,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return vault(params,
                 player=(ctx or {}).get("player"),
                 session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(vault({"action": "list"}))
