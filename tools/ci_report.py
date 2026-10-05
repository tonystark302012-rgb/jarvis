#!/usr/bin/env python3
"""Turn a pytest failure into GitHub Actions annotations.

CI log blobs (results-receiver / objects CDN) are not always reachable from
developer machines, and job logs expire — so every red run must leave its
failing test names and messages somewhere queryable with plain
``gh api .../check-runs/{id}/annotations``.

Usage (workflow calls this only after the Tests step failed):

    python tools/ci_report.py junit.xml pytest.log

* Reads the JUnit XML pytest writes (``--junitxml=junit.xml``) and emits one
  ``::error title=...::...`` line per failed/errored testcase (capped — GitHub
  allows a limited number of annotations per job), message trimmed.
* Falls back to the raw log: short-summary ``FAILED``/``ERROR`` lines, or the
  last 40 lines when even collection exploded before JUnit was written.

Exit code is always 0: this runs in an ``if: failure()`` step and must not
mask the original pytest status.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET

MAX_ANNOTATIONS = 8
TRIM = 600


def _ann(title: str, message: str) -> None:
    msg = re.sub(r"\s+", " ", message).strip()[:TRIM]
    # Newlines/colons are folded away by _ann's caller-side sanitising; a
    # workflow command must stay on ONE line.
    print(f"::error title={title}::{msg}")


def from_junit(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return 0
    seen = 0
    for case in root.iter("testcase"):
        for child in case:
            if child.tag not in ("failure", "error"):
                continue
            if seen >= MAX_ANNOTATIONS:
                return seen
            seen += 1
            cls = case.get("classname") or "pytest"
            name = case.get("name") or "?"
            detail = child.get("message") or (child.text or "")
            _ann(f"{cls}::{name}", detail or child.tag)
    return seen


def from_log(path: str) -> int:
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return 0
    lines = [
        ln for ln in text.splitlines()
        if re.match(r"^(FAILED|ERROR) ", ln)
    ]
    if not lines:  # collection/import explosion — no short summary
        tail = text.splitlines()[-40:]
        return _emit_block(tail)
    seen = 0
    for ln in lines:
        if seen >= MAX_ANNOTATIONS:
            break
        seen += 1
        _ann("pytest", ln)
    return seen


def _emit_block(lines: list[str]) -> int:
    if not lines:
        return 0
    _ann("pytest (no junit/log summary)", "\n".join(lines))
    return 1


def main(argv: list[str]) -> int:
    junit = argv[1] if len(argv) > 1 else "junit.xml"
    log = argv[2] if len(argv) > 2 else "pytest.log"
    count = from_junit(junit)
    if count == 0:
        count = from_log(log)
    if count == 0:  # never fail the failure-path step silently
        print("::error title=ci_report::no junit.xml or pytest.log found "
              "— Tests step failed before producing output")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
