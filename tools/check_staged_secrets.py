#!/usr/bin/env python3
"""Refuse to commit a secret.

`.gitignore` already lists the files that must never be published, and that is
enough for the everyday case — but `git add -f`, a stray `git add .` after a
config file was renamed, or a new credential file nobody thought to ignore all
walk straight past it. This runs as a pre-commit hook and stops the commit
before the secret is in a diff.

Usage:
    python tools/check_staged_secrets.py          # exit 1 if something is staged
    python tools/check_staged_secrets.py --list   # just show what is staged
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# ── Names that are secret by convention ───────────────────────────────────────
NAME_PATTERNS = [
    r"(^|/)api_keys\.json$",
    r"(^|/)\.env($|\.)",
    r"(^|/)certs?/",                       # dashboard TLS private key + cert
    r"(^|/)whatsapp_web/",                 # a linked WhatsApp session is creds
    r"token.*\.json$",
    r"client_secret.*\.json$",
    r"credentials.*\.json$",
    r"\.pem$", r"\.key$", r"\.p12$", r"\.pfx$",
    r"(^|/)id_rsa", r"(^|/)id_ed25519",
]

# ── Content that is secret whatever it is called ──────────────────────────────
# A Gemini key is `AIza` + 35 chars; an OpenAI-style key is `sk-`; a private key
# block is unambiguous. These are checked on staged *content*, so a key pasted
# into a test fixture or a comment is caught too.
CONTENT_PATTERNS = [
    (r"AIza[0-9A-Za-z_\-]{30,}", "a Google/Gemini API key"),
    (r"sk-[A-Za-z0-9]{20,}", "an OpenAI-style secret key"),
    (r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----", "a private key"),
    (r"ghp_[A-Za-z0-9]{20,}", "a GitHub token"),
]

# Files that legitimately contain those shapes: this checker's own patterns, the
# docs that describe them, and the tests that prove the checker works.
ALLOWLIST = {
    "tools/check_staged_secrets.py",
    "tests/test_project_hygiene.py",
    "SECURITY.md",
}

BINARY_SUFFIXES = {".obj", ".png", ".ico", ".wav", ".mp3", ".jpg", ".jpeg",
                   ".zip", ".db", ".sqlite", ".pdf", ".woff", ".woff2"}


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True,
                         timeout=60, check=False)
    return out.stdout


def staged_files() -> list[str]:
    return [f for f in _git("diff", "--cached", "--name-only",
                            "--diff-filter=ACMR").splitlines() if f.strip()]


def staged_content(path: str) -> str:
    """The staged version, not the working copy — what a commit would contain."""
    blob = _git("show", f":{path}")
    return blob


def check(files: list[str]) -> list[str]:
    problems: list[str] = []
    for path in files:
        if path in ALLOWLIST:
            continue
        for pattern in NAME_PATTERNS:
            if re.search(pattern, path):
                problems.append(f"{path}: secret-shaped filename (matches {pattern})")
                break
        if Path(path).suffix.lower() in BINARY_SUFFIXES:
            continue
        text = staged_content(path)
        if not text:
            continue
        for pattern, what in CONTENT_PATTERNS:
            m = re.search(pattern, text)
            if m:
                line = text[:m.start()].count("\n") + 1
                problems.append(f"{path}:{line}: looks like {what}")
    return problems


def main() -> int:
    files = staged_files()
    if "--list" in sys.argv:
        for f in files:
            print(f)
        return 0
    if not files:
        return 0
    problems = check(files)
    if not problems:
        return 0
    print("\n✗ refusing to commit — secret-shaped content:\n", file=sys.stderr)
    for p in problems:
        print(f"    {p}", file=sys.stderr)
    print("\n  If this is a false positive, add the file to ALLOWLIST in\n"
          "  tools/check_staged_secrets.py with a comment saying why.\n"
          "  If it is real: unstage it, rotate the credential, then clean up.\n",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
