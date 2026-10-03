"""
Gmail via IMAP/SMTP — free, no Google Cloud project, no OAuth dance.

Setup (one time): enable 2FA on the Google account, create an
[App Password](https://myaccount.google.com/apppasswords), then either
say it to JARVIS once or put it in config/api_keys.json:

    {"gmail_address": "you@gmail.com", "gmail_app_password": "abcd efgh ijkl mnop"}

Everything below uses only the standard library (imaplib, smtplib, email)
against gmail's public endpoints — nothing to install, nothing to pay.

Actions:
  unread   — newest unread mail (default)
  list     — newest mail, optionally filtered by search text
  read     — full body of one message (by index from list/unread)
  send     — SMTP send (587 STARTTLS) with confirmation string in body
  search   — IMAP SEARCH across all mail
  setup    — store address + app password into api_keys.json
"""
import email
import email.header
import email.message
import email.parser
import email.policy
import imaplib
import json
import re
import smtplib
from pathlib import Path

PLUGIN = {
    "name": "gmail",
    "description": (
        "Read and send email through Gmail (IMAP/SMTP with an app "
        "password — set up once via gmail action=setup). Actions: unread "
        "(default, newest unread), list (recent mail, optional search), "
        "read (full body by number), send (to/subject/body), search "
        "(query across all mail), setup (store address + app password). "
        "Use for 'check my email', 'read the latest mail', 'send mail to X'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING",
                       "description": "unread | list | read | send | search | setup"},
            "search": {"type": "STRING",
                       "description": "Filter text for list, full query for search"},
            "index": {"type": "INTEGER",
                      "description": "Message number from list/unread (read action)"},
            "to": {"type": "STRING", "description": "Recipient for send"},
            "subject": {"type": "STRING", "description": "Subject for send"},
            "body": {"type": "STRING",
                     "description": "Body for send; also stores setup fields as JSON"},
            "limit": {"type": "INTEGER", "description": "How many messages — default 5"},
            "address": {"type": "STRING", "description": "Gmail address for setup"},
            "app_password": {"type": "STRING",
                             "description": "Google app password for setup"},
        },
        "required": [],
    },
}

# ── config ───────────────────────────────────────────────────────────────────

def _cfg_path() -> Path:
    from config import get_base_dir
    return get_base_dir() / "config" / "api_keys.json"


def _cfg() -> dict:
    try:
        return json.loads(_cfg_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cfg(updates: dict) -> None:
    p = _cfg_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.update(updates)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _creds() -> tuple[str, str]:
    c = _cfg()
    return (str(c.get("gmail_address") or "").strip(),
            str(c.get("gmail_app_password") or "").strip())


def _need_setup() -> str | None:
    addr, pw = _creds()
    if not addr or not pw:
        return ("Gmail isn't set up yet. Say 'gmail setup with <address> "
                "and <app password>' — the app password comes from "
                "myaccount.google.com/apppasswords (needs 2FA on).")
    return None


# ── helpers ──────────────────────────────────────────────────────────────────

def _decode(v) -> str:
    if not v:
        return ""
    parts = email.header.decode_header(v)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                text = text.decode(enc or "utf-8", errors="replace")
            except (LookupError, ValueError):
                text = text.decode("utf-8", errors="replace")
        out.append(text)
    return "".join(out)


def _body(msg) -> str:
    """Plain-text body — prefers text/plain, falls back to stripped HTML."""
    try:
        if msg.is_multipart():
            plain, html = "", ""
            for part in msg.walk():
                ct = part.get_content_type()
                if part.get_content_maintype() == "multipart":
                    continue
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if ct == "text/plain" and not plain:
                    plain = text
                elif ct == "text/html" and not html:
                    html = text
            body = plain or html
        else:
            payload = msg.get_payload(decode=True) or b""
            body = payload.decode(msg.get_content_charset() or "utf-8",
                                  errors="replace")
    except Exception:
        body = str(msg.get_payload() or "")
    body = body.strip()
    if not plain_is_plain(msg) and "<" in body and ">" in body:
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
    return body


def plain_is_plain(msg) -> bool:
    return not msg.is_multipart() and msg.get_content_type() == "text/plain"


class _Conn:
    """IMAP connection helper — one place for select/search/fetch."""

    def __init__(self):
        self.imap = imaplib.IMAP4_SSL("imap.gmail.com", 993)

    def __enter__(self):
        addr, pw = _creds()
        self.imap.login(addr, pw)
        return self

    def __exit__(self, *exc):
        try:
            self.imap.logout()
        except Exception:
            pass

    def search(self, charset, *criteria) -> list[bytes]:
        typ, data = self.imap.search(charset, *criteria)
        if typ != "OK":
            return []
        return (data[0] or b"").split()

    def fetch(self, num: bytes):
        typ, data = self.imap.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not data or data[0] is None:
            return None
        raw = data[0]
        if isinstance(raw, tuple):
            raw = raw[1]
        return email.message_from_bytes(raw, policy=email.policy.default)

    @staticmethod
    def summarize(msg, n: int) -> dict:
        return {
            "n": n,
            "from": _decode(msg.get("From")),
            "subject": _decode(msg.get("Subject")),
            "date": str(msg.get("Date") or ""),
        }


def _fmt_msg(summary: dict) -> str:
    return (f"{summary['n']}. [{summary['date']}] {summary['from']} — "
            f"{summary['subject']}")


# ── actions ──────────────────────────────────────────────────────────────────

def _list(kind: str, search: str, limit: int) -> str:
    need = _need_setup()
    if need:
        return need
    limit = max(1, min(25, int(limit or 5)))
    try:
        with _Conn() as c:
            c.imap.select("[Gmail]/All Mail" if search else "INBOX", readonly=True)
            if kind == "unread":
                nums = c.search(None, "UNSEEN")
                nums = nums[-limit:][::-1] if nums else []
            else:
                nums = c.search(None, "ALL")
                nums = nums[-limit:][::-1] if nums else []
            if not nums:
                return "No unread mail." if kind == "unread" else "No mail found."
            out = []
            for n in nums[:limit]:
                msg = c.fetch(n)
                if msg is None:
                    continue
                s = c.summarize(msg, 0)
                if search:
                    hay = (s["subject"] + " " + s["from"]).lower()
                    if search.lower() not in hay:
                        continue
                out.append((n.decode(), s))
        if not out:
            return f"No messages matching {search!r}."
        lines = [f"{i + 1}. [{s['date']}] {s['from']} — {s['subject']}"
                 for i, (num, s) in enumerate(out)]
        header = ("Unread mail" if kind == "unread" else "Recent mail")
        body = "\n".join(lines)
        keys = ", ".join(num for num, _ in out)
        return (f"{header} ({len(out)}):\n{body}\n"
                f"Message ids: {keys}. Say 'read <id>' for the full body.")
    except imaplib.IMAP4.error as e:
        return f"Gmail IMAP error: {e}"
    except Exception as e:
        return f"Gmail list failed: {e}"


def _read(index) -> str:
    need = _need_setup()
    if need:
        return need
    idx = str(index or "").strip()
    if not idx:
        return "Give the message id — read <id> (ids are listed with unread/list)."
    try:
        with _Conn() as c:
            c.imap.select("INBOX", readonly=True)
            msg = c.fetch(idx.encode() if idx.isdigit() else idx.encode())
            if msg is None:
                # fall back: id may be from "All Mail" search
                c.imap.select("[Gmail]/All Mail", readonly=True)
                msg = c.fetch(idx.encode())
            if msg is None:
                return f"No message #{idx} — list again to refresh ids."
            body = _body(msg)
            head = (f"From: {_decode(msg.get('From'))}\n"
                    f"To: {_decode(msg.get('To'))}\n"
                    f"Subject: {_decode(msg.get('Subject'))}\n"
                    f"Date: {msg.get('Date')}\n\n")
            return head + (body[:6000] +
                           ("\n…(truncated)" if len(body) > 6000 else ""))
    except Exception as e:
        return f"Gmail read failed: {e}"


def _send(to: str, subject: str, body: str) -> str:
    need = _need_setup()
    if need:
        return need
    to = str(to or "").strip()
    if not to:
        return "Who should I send to? Give the address with the send action."
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
        return f"That doesn't look like an email address: {to!r}"
    addr, pw = _creds()
    msg = email.message.EmailMessage()
    msg["From"] = addr
    msg["To"] = to
    msg["Subject"] = str(subject or "(no subject)")
    msg.set_content(str(body or ""))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as sm:
            sm.ehlo()
            sm.starttls()
            sm.ehlo()
            sm.login(addr, pw)
            sm.send_message(msg)
        return f"Mail sent to {to}: {msg['Subject']}"
    except smtplib.SMTPAuthenticationError:
        return ("Gmail SMTP auth failed — the app password is wrong or "
                "revoked. Create a new one at myaccount.google.com/"
                "apppasswords and run gmail setup again.")
    except Exception as e:
        return f"Gmail send failed: {e}"


def _search(query: str, limit: int) -> str:
    need = _need_setup()
    if need:
        return need
    query = str(query or "").strip()
    if not query:
        return "What should I search for?"
    limit = max(1, min(25, int(limit or 5)))
    try:
        with _Conn() as c:
            c.imap.select("[Gmail]/All Mail", readonly=True)
            # IMAP TEXT search is server-side and free-form safe
            nums = c.search(None, "TEXT", f'"{query}"')
            nums = nums[-limit:][::-1] if nums else []
            if not nums:
                return f"No mail matching {query!r}."
            out = []
            for n in nums:
                msg = c.fetch(n)
                if msg is None:
                    continue
                s = c.summarize(msg, 0)
                out.append((n.decode(), s))
        lines = [f"{num}: [{s['date']}] {s['from']} — {s['subject']}"
                 for num, s in out]
        return f"Matches for {query!r} ({len(out)}):\n" + "\n".join(lines)
    except Exception as e:
        return f"Gmail search failed: {e}"


def _setup(body: str, address: str, app_password: str) -> str:
    addr = str(address or "").strip()
    pw = str(app_password or "").strip()
    if not addr and body:
        # allow JSON in body: {"gmail_address": "...", "gmail_app_password": "..."}
        try:
            data = json.loads(body)
            addr = str(data.get("gmail_address") or data.get("address") or addr)
            pw = str(data.get("gmail_app_password") or
                     data.get("app_password") or pw)
        except (ValueError, AttributeError):
            m = re.match(r"\s*(\S+@\S+\.\S+)\s+(.+)$", body, re.S)
            if m:
                addr, pw = m.group(1), m.group(2).strip()
    if not addr or not pw:
        return ("Setup needs the address and the app password: "
                "gmail setup, address=you@gmail.com, app_password=xxxx")
    if "@" not in addr:
        return f"That doesn't look like a Gmail address: {addr!r}"
    _save_cfg({"gmail_address": addr, "gmail_app_password": pw})
    # verify the pair against IMAP so setup can't silently fail later
    try:
        with _Conn() as c:
            c.imap.select("INBOX", readonly=True)
    except Exception as e:
        return (f"Saved, but login check failed: {e}. Check the app "
                "password (spaces are fine — we send it as typed).")
    return f"Gmail set up for {addr} — login verified. You can now ask me to check mail."


# ── entry ────────────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "unread").lower().strip()
    try:
        if action == "list":
            return _list("list", str(params.get("search") or ""),
                         params.get("limit"))
        if action == "read":
            return _read(params.get("index"))
        if action == "send":
            return _send(str(params.get("to") or ""),
                         str(params.get("subject") or ""),
                         str(params.get("body") or ""))
        if action == "search":
            return _search(str(params.get("search") or ""),
                           params.get("limit"))
        if action == "setup":
            return _setup(str(params.get("body") or ""),
                          str(params.get("address") or ""),
                          str(params.get("app_password") or ""))
        if action == "unread":
            return _list("unread", "", params.get("limit"))
        return f"Unknown gmail action {action!r} — use unread|list|read|send|search|setup."
    except Exception as e:                       # never raise to the loader
        return f"Gmail failed: {e}"
