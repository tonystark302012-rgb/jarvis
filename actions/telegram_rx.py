"""
telegram_rx — receive Telegram commands and run them like typed HUD input.

Report #1 Tier-A #1: send_message already SENDS; this closes the loop —
your phone becomes a remote control. Messages are pulled with the plain
Bot API `getUpdates` long-poll (stdlib requests, no heavy SDK) and fed
into the SAME `_on_text_command` path the HUD text box uses, so presence,
wake gating, phrase-rules and the session all behave identically.

SAFETY (non-negotiable for a remote control):
  * `telegram_allowed_chats` MUST list the chat ids allowed to command
    JARVIS — with no allowlist the daemon refuses to start (an open
    relay would let anyone on the internet drive this machine).
  * token lives in config/api_keys.json (`telegram_bot_token`) or the
    TELEGRAM_BOT_TOKEN env var — never in code.
  * unknown chats are counted and dropped silently (no reply → no
    probing surface).

One-server rule intact: this is a client of Telegram's API, it opens NO
listener port.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

_POLL_TIMEOUT = 25
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_DISPATCH: Callable[[str], None] | None = None
_OFFSET = 0
_STATE: dict = {"running": False, "blocked": 0, "handled": 0,
                "last_error": "", "token": ""}


def set_dispatch(fn: Callable[[str], None]) -> None:
    """main injects its text-command entry point (same as the HUD box)."""
    global _DISPATCH
    _DISPATCH = fn


def _settings() -> tuple[str, list[str]]:
    """(token, allowlisted chat ids) from config, then env."""
    token = ""
    allowed: list[str] = []
    try:
        from config import get_base_dir
        import json as _json
        data = _json.loads(
            (get_base_dir() / "config" / "api_keys.json")
            .read_text(encoding="utf-8"))
        token = str(data.get("telegram_bot_token") or "")
        raw = data.get("telegram_allowed_chats") or []
        if isinstance(raw, str):
            raw = [x for x in raw.replace(";", ",").split(",")]
        allowed = [str(x).strip() for x in raw if str(x).strip()]
    except Exception:
        pass
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", token) or token
    env_chats = os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
    if env_chats and not allowed:
        allowed = [x.strip() for x in env_chats.replace(";", ",").split(",")
                   if x.strip()]
    return token, allowed


def _http_get(url: str, params: dict, timeout: float):
    """Seam for tests — real long-poll via requests."""
    import requests
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _one_round() -> None:
    global _OFFSET
    token, allowed = _settings()
    allow_set = set(allowed)
    data = _http_get(
        f"https://api.telegram.org/bot{token}/getUpdates",
        {"offset": _OFFSET, "timeout": _POLL_TIMEOUT,
         "allowed_updates": ["message"]},
        timeout=_POLL_TIMEOUT + 10)
    for upd in (data.get("result") or []):
        _OFFSET = int(upd.get("update_id", 0)) + 1
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not text or not chat_id:
            continue
        if chat_id not in allow_set:
            _STATE["blocked"] += 1
            continue
        fn = _DISPATCH
        if fn is None:
            _STATE["last_error"] = "no dispatch injected"
            continue
        try:
            fn(text)
            _STATE["handled"] += 1
        except Exception as e:
            _STATE["last_error"] = str(e)[:120]


def _loop() -> None:
    _STATE["running"] = True
    while not _STOP.is_set():
        try:
            _one_round()
        except Exception as e:                # network blip → back off
            _STATE["last_error"] = str(e)[:120]
            time.sleep(3)
    _STATE["running"] = False


def start(parameters_token: str = "") -> str:
    global _THREAD
    token, allowed = _settings()
    token = parameters_token or token
    if not token:
        return ("No Telegram bot token — set `telegram_bot_token` in "
                "config/api_keys.json (or TELEGRAM_BOT_TOKEN env). "
                "Create one with @BotFather.")
    if not allowed:
        return ("Refusing to start without `telegram_allowed_chats` — an "
                "open Telegram relay would let anyone command this "
                "machine. Add your chat id (ask @userinfobot) to "
                "config/api_keys.json.")
    if _THREAD and _THREAD.is_alive():
        return "Telegram receiver already running."
    _STOP.clear()
    _STATE["token"] = token[:8] + "…" if len(token) > 8 else "set"
    _THREAD = threading.Thread(target=_loop, daemon=True,
                               name="telegram-rx")
    _THREAD.start()
    return (f"Telegram receiver on — allowlisted {len(allowed)} chat(s); "
            "text your bot and JARVIS treats it like the HUD box.")


def stop() -> str:
    _STOP.set()
    _STATE["running"] = False
    return "Telegram receiver stopped."


def autostart() -> str:
    """Called once from main at boot: silent unless misconfigured."""
    token, allowed = _settings()
    if not token or not allowed:
        return ""
    if _THREAD and _THREAD.is_alive():
        return ""
    try:
        return start()
    except Exception:
        return ""


def telegram_rx(parameters: dict | None = None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()
    if action == "start":
        return start(str(params.get("token", "") or ""))
    if action == "stop":
        return stop()
    token, allowed = _settings()
    state = ("running" if _STATE["running"] else "stopped")
    parts = [f"Telegram receiver: {state}.",
             f"token: {'set' if token else 'missing'}",
             f"allowlist: {len(allowed)} chat(s)"]
    if _STATE["handled"] or _STATE["blocked"]:
        parts.append(f"handled {_STATE['handled']}, "
                     f"blocked {_STATE['blocked']}")
    if _STATE["last_error"]:
        parts.append(f"last error: {_STATE['last_error']}")
    if not token:
        parts.append("Set telegram_bot_token (@BotFather) to enable.")
    elif not allowed:
        parts.append("Set telegram_allowed_chats — receiver refuses an "
                     "open relay.")
    return " ".join(parts)


TOOL = {
    "name": "telegram_rx",
    "description": (
        "Telegram REMOTE CONTROL (receive side — send_message is the "
        "send side). Long-polls the Bot API and feeds allowlisted "
        "chat messages into the same path as the HUD text box (presence, "
        "wake gate, phrase rules, session). Actions: start | stop | "
        "status. Needs `telegram_bot_token` (@BotFather) and "
        "`telegram_allowed_chats` (your chat id) in config/api_keys.json "
        "— refuses to run without an allowlist. Use when the user says "
        "'telegram se control karo', 'start telegram receiver', "
        "'phone remote'."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "start | stop | status."},
            "token": {"type": "STRING",
                      "description": "Override token (rare)."},
        },
        "required": [],
    },
    "handler": telegram_rx,
}
