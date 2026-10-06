#!/usr/bin/env python3
"""Write requirements.lock from the environment the dev requirements resolved to.

Why not pip-tools or uv: both are good, and both are another dependency to keep
current in a project that already carries 60. This does the part that matters
here — record *every* version actually installed, including the transitive ones
nobody wrote down — using pip's own freeze, which is the resolver's own answer
rather than a second guess at it.

    python tools/make_lock.py            # after `pip install -r requirements-dev.txt`
    make lock

The output is not an environment file to install blindly: it is the record of
what CI and contributors were running on the day it was generated, so a
"works on my machine" can be compared instead of argued about. Regenerate it
when `requirements-dev.txt` changes.

Runtime dependencies are NOT locked here. `requirements.txt` uses per-OS markers
and is resolved fresh on each machine on purpose — pinning a Windows wheel set
into the repo would be wrong for every Linux user.
"""
from __future__ import annotations

import datetime as dt
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEV_REQ = ROOT / "requirements-dev.txt"
LOCK = ROOT / "requirements.lock"

HEADER = """\
# ── requirements.lock — generated, do not edit by hand ────────────────────────
#
# The resolved dependency set of requirements-dev.txt (test/lint only), pinned
# exactly, including transitive packages. It answers "which versions was this
# green against?".
#
#   regenerate:  make lock
#   inspect:     git diff requirements.lock
#
# Plain `pip install -r requirements.lock` works, but it pins platform-specific
# packages as they resolved on ONE machine — the header records which. For
# day-to-day work use requirements-dev.txt, which resolves per platform.
"""


def main() -> int:
    if not DEV_REQ.exists():
        print(f"missing {DEV_REQ}", file=sys.stderr)
        return 1
    out = subprocess.run([sys.executable, "-m", "pip", "freeze",
                          "--exclude-editable"], capture_output=True, text=True,
                         timeout=300, check=False)
    if out.returncode != 0:
        print(out.stderr[-500:], file=sys.stderr)
        return 1
    pins = [ln for ln in out.stdout.splitlines()
            if ln.strip() and not ln.startswith("#")]
    pins.sort(key=str.lower)

    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    generated = (f"# generated   : {stamp} · python {py} · "
                 f"{platform.system()} {platform.machine()}\n"
                 f"# package count: {len(pins)}\n")
    LOCK.write_text(HEADER + generated + "\n" + "\n".join(pins) + "\n",
                    encoding="utf-8")
    print(f"wrote {LOCK.relative_to(ROOT)} — {len(pins)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
