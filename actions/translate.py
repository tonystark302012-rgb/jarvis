# actions/translate.py
"""translate — live translate between the user and the world.

WHAT IT DOES
    `text` mode — translate a phrase/sentence to (or from) a target
    language. `live` mode — set a persistent pair (e.g. en↔hi) that the
    voice pipeline can use turn-by-turn; `status`/`off` control it.

ENGINES (all free)
    1. Local Argos (offline, if `argostranslate` + a package are
       installed — the ₹0 offline path from the roadmap).
    2. Gemini free tier (cloud, needs key) — unless privacy mode is ON,
       in which case we say so instead of leaking the sentence.

This is why the tool is in the gate list too: a private conversation
translated through the cloud is a private conversation sent to the cloud.
"""
from __future__ import annotations

from threading import Lock

_LIVE: dict = {}
_LOCK = Lock()

_LANGS = {
    "en": "English", "hi": "Hindi", "mr": "Marathi", "bn": "Bengali",
    "ta": "Tamil", "te": "Telugu", "kn": "Kannada", "ml": "Malayalam",
    "gu": "Gujarati", "pa": "Punjabi", "ur": "Urdu", "es": "Spanish",
    "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese",
    "ru": "Russian", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ar": "Arabic", "tr": "Turkish", "nl": "Dutch", "sv": "Swedish",
}


def _norm_lang(x: str) -> str:
    x = (x or "").strip().lower()[:5]
    base = x.split("-")[0].split("_")[0]
    return base if len(base) >= 2 else ""


def _lang_name(code: str) -> str:
    return _LANGS.get(code, code.upper())


def _argos_available() -> bool:
    try:
        import argostranslate.package  # noqa: F401
        return True
    except ImportError:
        return False


def _argos_translate(text: str, src: str, dst: str) -> str | None:
    try:
        import argostranslate.translate as at
        out = at.translate(text, src, dst)
        return out if out and out != text else None
    except Exception:
        return None


def _gemini_translate(text: str, src: str, dst: str) -> str | None:
    from core import gemini
    if not gemini.api_key():
        return None
    from core import privacy as _privacy
    if _privacy.is_on():
        return None
    resp = gemini.call(
        [f"Translate from {_lang_name(src)} to {_lang_name(dst)}. "
         f"Return ONLY the translation, no quotes, no notes.\n\n"
         f"TEXT: {text}"],
        tier=gemini.FAST, timeout_ms=20_000)
    if resp is None:
        return None
    try:
        out = "".join(p.text for p in resp.candidates[0].content.parts
                      if getattr(p, "text", None)).strip()
        return out or None
    except Exception:
        return None


def _resolve_pair(params: dict) -> tuple[str, str]:
    """Pair from explicit params, live session, or defaults (en↔hi)."""
    src = _norm_lang(str(params.get("source") or params.get("from") or ""))
    dst = _norm_lang(str(params.get("target") or params.get("to") or ""))
    with _LOCK:
        if not src:
            src = _LIVE.get("src", "en")
        if not dst:
            dst = _LIVE.get("dst", "hi")
    if src == dst:
        # pick the other side of the default pair — never leave them equal
        dst = "en" if src != "en" else "hi"
    return src, dst


def translate(parameters: dict | None = None, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "").lower().strip()

    if action == "off":
        with _LOCK:
            _LIVE.clear()
        return "Live translate off."

    if action == "status":
        with _LOCK:
            if not _LIVE:
                return ("Live translate: off. "
                        f"{'Argos local engine ready.' if _argos_available() else ''}")
            return (f"Live translate: {_LIVE['src']} ↔ {_LIVE['dst']} "
                    f"({_lang_name(_LIVE['src'])} ↔ {_lang_name(_LIVE['dst'])}).")

    if action in ("live", "pair"):
        src = _norm_lang(str(params.get("source") or params.get("from") or ""))
        dst = _norm_lang(str(params.get("target") or params.get("to") or ""))
        if not src and not dst:
            return ("Give the pair — e.g. translate action=live from=en to=hi.")
        if not src:
            src = "hi" if dst != "hi" else "en"
        if not dst or dst == src:
            dst = "en" if src != "en" else "hi"
        if dst not in _LANGS:
            dst = "en"
        with _LOCK:
            _LIVE.update({"src": src, "dst": dst})
        return (f"Live translate on: {_lang_name(src)} ↔ {_lang_name(dst)}. "
                "Bolna shuru karo — main har line ka translate de dunga.")

    text = str(params.get("text") or params.get("q") or "").strip()
    if not text:
        if action == "live":
            return translate({"action": "live"}, player, session_memory)
        return ("What should I translate? — translate text='namaste' to=es.")
    if len(text) > 3000:
        text = text[:3000]

    src, dst = _resolve_pair(params)
    if params.get("auto") and src == dst:
        src = "en"

    # engine order: local first (free+private), then Gemini
    out = None
    if _argos_available() and (src, dst) != ("en", "en"):
        out = _argos_translate(text, src, dst)
    if out is None:
        out = _gemini_translate(text, src, dst)
    if out is None:
        from core import privacy as _privacy
        if _privacy.is_on():
            return ("Privacy mode is ON — translating through the cloud "
                    "would send this text out. Say 'privacy off' first, or "
                    "install the offline engine (argostranslate).")
        return ("No translation engine available: install `argostranslate` "
                "for offline translation, or configure a Gemini key.")
    return out


TOOL = {
    "name": "translate",
    "description": (
        "Translate text, or run a persistent live-translate pair. Actions: "
        "text (default — pass `text`, `to=`/`from=` language codes like "
        "en/hi/es), live (set persistent pair, e.g. from=en to=hi), status, "
        "off. Engines: offline argostranslate if installed, else Gemini "
        "free tier (blocked while privacy mode is ON). Use for 'translate "
        "this to Spanish', 'live translate Hindi mein'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {"type": "STRING", "description": "Text to translate"},
            "to": {"type": "STRING", "description": "Target language code"},
            "from": {"type": "STRING", "description": "Source language code"},
            "action": {"type": "STRING", "description": "text | live | status | off"},
        },
        "required": [],
    },
    "handler": translate,
}


def run(params: dict, ctx: dict | None = None) -> str:
    return translate(params,
                     player=(ctx or {}).get("player"),
                     session_memory=(ctx or {}).get("session_memory"))


if __name__ == "__main__":
    print(translate({"action": "status"}))
