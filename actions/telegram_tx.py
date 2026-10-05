"""
telegram_tx — send messages OUT through the same bot (Report M: the
"→telegram" half; telegram_rx is the receive half).

Plain Bot API `sendMessage` over HTTPS (stdlib requests) — headless,
unlike send_message.py's desktop-GUI automation which needs a display.

SAFETY (mirrors telegram_rx): the chat must be in `telegram_allowed_chats`
— outbound spam to arbitrary chat ids is refused, same policy as
inbound commands. Token from config/env only, never in code.

One-server rule intact: outbound HTTPS client, NO listener port.
"""
from __future__ import annotations

from actions.telegram_rx import _settings


def _http_post(url: str, data: dict, timeout: float = 15):
    """Seam for tests — real POST via requests."""
    import requests
    r = requests.post(url, data=data, timeout=timeout)
    r.raise_for_status()
    return r.json()


def send(text: str, chat: str = "") -> str:
    """Send one message to an allowlisted chat. Returns an honest status."""
    text = str(text or "").strip()
    if not text:
        return "Nothing to send (empty text)."
    token, allowed = _settings()
    if not token:
        return ("No Telegram bot token — set `telegram_bot_token` in "
                "config/api_keys.json or TELEGRAM_BOT_TOKEN (BotFather "
                "gives one free). Nothing sent.")
    target = str(chat or "").strip() or (allowed[0] if allowed else "")
    if not target:
        return ("No chat to send to — set `telegram_allowed_chats` in "
                "config/api_keys.json (your chat id, via @userinfobot). "
                "Nothing sent.")
    if allowed and target not in allowed:
        return (f"Chat {target} is not in telegram_allowed_chats — "
                "refusing (same policy as inbound commands). Nothing sent.")
    try:
        out = _http_post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": target, "text": text[:4000]})
    except Exception as e:
        return f"Telegram send failed: {e}"
    if isinstance(out, dict) and out.get("ok"):
        return f"Sent to Telegram chat {target} (message_id {out.get('result', {}).get('message_id', '?')})."
    return f"Telegram rejected: {str(out)[:200]}"


TOOL = {
    "name": "telegram_send",
    "description": (
        "Send a message OUT via the Telegram bot (headless Bot API — "
        "unlike send_message which drives the desktop app). Chat must "
        "be in telegram_allowed_chats (safety policy); token from "
        "config. Use for 'send this to my phone/telegram', verdict "
        "pushes, alerts."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {"type": "STRING", "description": "Message to send."},
            "chat": {"type": "STRING",
                     "description": "Optional allowlisted chat id "
                                    "(defaults to the first allowed)."},
        },
        "required": ["text"],
    },
    "handler": lambda parameters=None, player=None, session_memory=None: send(
        (parameters or {}).get("text", ""),
        str((parameters or {}).get("chat", "") or "")),
}
