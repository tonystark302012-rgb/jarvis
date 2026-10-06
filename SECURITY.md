# Security Policy

MARK LV (JARVIS) is a desktop voice assistant that, by design, can do things a
plain chatbot cannot: read your screen, run shell commands, move your mouse,
hold a phone dashboard open on your LAN, and keep a vault of passwords. That
makes its threat model worth stating out loud rather than leaving implied.

## Reporting a vulnerability

Open a **private** report: GitHub → the repository → **Security** tab →
*Report a vulnerability*. Please do not open a public issue for anything
exploitable until there is a fix.

Include what you have: the version (`python main.py --version`), your OS, what
you did, what happened, and a proof of concept if you have one. A diagnostics
bundle is useful and safe to attach — `python main.py --diagnostics` writes a zip
whose config is **redacted** (`core/logging_setup.redact()` replaces anything
matching key/secret/token/password before it is written). Read it before you send
it anyway; you know your own machine.

Expect an acknowledgement within a few days. This is a personal project, not a
company with an on-call rotation — an honest answer about timing beats a promise
that gets broken.

## What is and is not in scope

**In scope** — anything that gets an attacker past a gate this project claims to
have: the confirmation gate on irreversible actions, privacy mode's block on
cloud-facing tools, the terminal sandbox, the vault's encryption, the dashboard's
PIN/auth, the audit chain's integrity, plugin crash isolation, or secret
redaction.

**Out of scope** — the assistant doing what you told it to do. If you ask for a
file to be deleted, it will be deleted. If you enable a plugin you wrote, its
code runs with your privileges. "The model can be prompt-injected by a web page
it was asked to read" is worth reporting when it defeats a gate; it is not a
vulnerability when the result is the model summarising the wrong paragraph.

## The gates, and what they actually guarantee

| Gate | Where | What it guarantees |
|---|---|---|
| **Confirmation gate** | `core/confirm.py` | Irreversible actions need a token **issued by the UI**, not by the model. A prompt-injected model cannot mint one. |
| **Privacy mode** | `core/privacy.py` | Cloud-facing tools are refused at the dispatch point, so it does not depend on the model obeying instructions. |
| **Terminal sandbox** | `actions/terminal.py` | `argv`-only (no shell string), command allowlist, repo-confined paths. It is a brake, not a jail — see below. |
| **Vault** | `actions/vault.py` | AES-256-GCM; the master passphrase is never stored. Losing it means losing the data, by design. |
| **Dashboard** | `dashboard/server.py` | PIN login, per-token TTL, `revoke_devices` clears sessions *and* tokens, self-signed TLS, constant-time comparisons. |
| **Audit chain** | `core/audit_chain.py` | Every tool call is appended with a hash link to the previous entry, so a log that was edited stops verifying. |
| **Plugin isolation** | `core/plugin_loader.py` | A plugin that raises is disabled and reported; it cannot take the app down. It is **not** a security sandbox — plugin code is arbitrary Python with your privileges. |

Honest limits:

- **The terminal sandbox is not a security boundary.** It is an allowlist that
  stops accidents and prompt-injected one-liners. Anything that can run an
  allowlisted command may be able to reach further than the allowlist intends —
  treat it as a seatbelt, not a cage. Don't hand this app to an untrusted user
  and call it isolation.
- **The model is not a security boundary, either.** It is the thing being
  defended against. Every gate above is enforced in Python at the dispatch
  point, never by prompt wording.
- **The dashboard binds 0.0.0.0** so your phone can reach it. That is a LAN
  service: use it on a network you trust, keep the PIN out of the microphone's
  reach, and revoke devices from the dashboard when you are done with one.
- **Audio is always listening when the session is open.** Mute from the HUD, or
  use push-to-talk; wake-word detection, when enabled, is local and offline.
- **Plugin and action files are code.** `plugins/*.py` is dropped in and
  imported. Review before you drop.

## Secrets: where they live, what is never committed

- `config/api_keys.json` — your Gemini key and settings. Git-ignored, listed
  first in `.gitignore`, and there is a pre-commit hook that refuses to commit
  it (`.pre-commit-config.yaml`).
- `config/certs/` — the dashboard's self-signed TLS pair. Git-ignored.
- `config/whatsapp_web/` — a linked session is account credentials. Git-ignored.
- `~/.jarvis/logs/` — the log file, outside the checkout on purpose. Diagnostics
  export redacts it.

If you have committed a key by accident: rotate it first, then clean history.
Rotating is the part that matters; a rewritten history does not un-leak a key
that was already pushed.

## Supported versions

The `main` branch and the latest tagged release. Fixes land on `main`; older
snapshots are not back-patched.
