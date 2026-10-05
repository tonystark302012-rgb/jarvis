# dots/blocks.py
"""Page content = ordered block list (JSON canonical). Markdown is a view
and a source-mode import path. Deterministic both ways — revisions diff
against the JSON, so round-tripping must be exact for canonical forms.

Block shapes (v1, mirrors the slash menu):
  {"type": "text",      "text": str}
  {"type": "bullet",    "text": str}
  {"type": "numbered",  "text": str}
  {"type": "checklist", "text": str, "checked": bool}
  {"type": "quote",     "text": str}
  {"type": "code",      "text": str, "lang": str}
  {"type": "divider"}
  {"type": "table",     "rows": [[str, ...], ...], "header": bool}
"""
from __future__ import annotations

import json
import re

_TYPES = {"text", "bullet", "numbered", "checklist", "quote", "code",
          "divider", "table"}


def validate_blocks(obj) -> tuple[list | None, str | None]:
    """Accept a list of blocks (or a JSON string of one) → (blocks, None)
    or (None, honest reason). Unknown shapes are rejected, not guessed."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            return None, f"content_json is not valid JSON: {e}"
    if not isinstance(obj, list):
        return None, "content_json must be a list of blocks"
    for i, b in enumerate(obj):
        if not isinstance(b, dict):
            return None, f"block {i} is not an object"
        t = b.get("type")
        if t not in _TYPES:
            return None, f"block {i} has unknown type {t!r}"
        if t in ("text", "bullet", "numbered", "quote") and \
                not isinstance(b.get("text"), str):
            return None, f"block {i} ({t}) needs string text"
        if t == "checklist" and not isinstance(b.get("text"), str):
            return None, f"block {i} (checklist) needs string text"
        if t == "code" and not isinstance(b.get("text"), str):
            return None, f"block {i} (code) needs string text"
        if t == "table":
            rows = b.get("rows")
            if not isinstance(rows, list) or not rows or \
                    not all(isinstance(r, list) and
                            all(isinstance(c, str) for c in r)
                            for r in rows):
                return None, f"block {i} (table) needs rows: list[list[str]]"
    return obj, None


def blocks_to_md(blocks: list) -> str:
    """Canonical blocks → markdown source. Stable, no HTML."""
    out: list[str] = []
    for b in blocks or []:
        t = b.get("type")
        if t == "text":
            out.append(str(b.get("text") or ""))
        elif t == "bullet":
            out.append(f"- {b.get('text') or ''}")
        elif t == "numbered":
            out.append(f"1. {b.get('text') or ''}")   # markdown renumbers
        elif t == "checklist":
            mark = "x" if b.get("checked") else " "
            out.append(f"- [{mark}] {b.get('text') or ''}")
        elif t == "quote":
            out.append(f"> {b.get('text') or ''}")
        elif t == "code":
            # ONE item: the block joiner must not inject blank lines inside
            # a fence or the parser would absorb them into the code text
            lang = str(b.get("lang") or "")
            out.append(f"```{lang}\n{b.get('text') or ''}\n```")
        elif t == "divider":
            out.append("---")
        elif t == "table":
            rows = b.get("rows") or []
            header = bool(b.get("header"))
            tbl: list[str] = []
            for i, row in enumerate(rows):
                cells = [str(c).replace("|", "\\|") for c in row]
                tbl.append("| " + " | ".join(cells) + " |")
                if header and i == 0:
                    tbl.append("| " + " | ".join(["---"] * len(row)) + " |")
            out.append("\n".join(tbl))       # ONE item, rows stay adjacent
    return "\n\n".join(out)


def md_to_blocks(md: str) -> list:
    """Markdown source → blocks (import path for source mode). Best-effort
    by design: unknown syntax degrades to text, never raises."""
    blocks: list[dict] = []
    lines = (md or "").replace("\r\n", "\n").split("\n")
    i = 0
    para: list[str] = []
    table_buf: list[str] = []

    def flush_para():
        if para:
            blocks.append({"type": "text", "text": "\n".join(para)})
            para.clear()

    def flush_table():
        if not table_buf:
            return
        rows = []
        for ln in table_buf:
            parts = re.split(r"(?<!\\)\|", ln.strip().strip("|"))
            cells = [c.strip().replace("\\|", "|") for c in parts]
            rows.append(cells)
        # a separator row like |---|---| becomes empty cells (drop it)
        rows = [r for r in rows if any(c.strip("- ") for c in r)]
        if rows:
            blocks.append({"type": "table", "rows": rows, "header": True})
        table_buf.clear()

    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()

        if table_buf and stripped.startswith("|"):
            table_buf.append(ln)
            i += 1
            continue
        flush_table()

        if stripped.startswith("```"):
            flush_para()
            lang = stripped[3:].strip()
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1                       # closing fence (or EOF)
            blocks.append({"type": "code", "text": "\n".join(body),
                           "lang": lang})
            continue

        if not stripped:
            flush_para()
            i += 1
            continue

        if stripped in ("---", "***", "___"):
            flush_para()
            blocks.append({"type": "divider"})
        elif stripped.startswith("> "):
            flush_para()
            blocks.append({"type": "quote", "text": stripped[2:]})
        elif stripped.startswith(">"):
            flush_para()
            blocks.append({"type": "quote", "text": stripped[1:].lstrip()})
        elif re.match(r"^- \[[ xX]\] ", stripped):
            flush_para()
            checked = stripped[3].lower() == "x"
            blocks.append({"type": "checklist",
                           "text": stripped[6:], "checked": checked})
        elif stripped.startswith(("- ", "* ")):
            flush_para()
            blocks.append({"type": "bullet", "text": stripped[2:]})
        elif re.match(r"^\d+\.\s+", stripped):
            flush_para()
            blocks.append({"type": "numbered",
                           "text": re.sub(r"^\d+\.\s+", "", stripped)})
        elif stripped.startswith("|"):
            flush_para()
            table_buf.append(ln)
        else:
            para.append(ln)
        i += 1

    flush_para()
    flush_table()
    return blocks
