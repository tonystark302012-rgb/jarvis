#!/usr/bin/env python3
"""
count_tools — the real tool-declaration numbers, measured not remembered.

The README used to carry a skill count and a character total that the
codebase had long outgrown ("eighteen skills… 12,907 characters" against a
tree with 76 declarations and ~72 KB of JSON). Both numbers had nothing in
the repo that could regenerate them, which is exactly why they drifted.

This script is that missing source. Run it after adding or removing an
action and update the README from its output:

    python tools/count_tools.py

Exit code is always 0 — this measures, it does not assert.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def files_declaring_tool(actions_dir: Path) -> tuple[list[str], list[str]]:
    """(with TOOL, without) — a static count that needs no imports.

    A file counts as declaring a tool if it either assigns `TOOL` at module
    level or re-exports one (`from memory.graph import TOOL`, which is how
    `actions/graph.py` ships the memory-graph tool). Without the second case
    the file count and the registry count disagree by one, which is the kind
    of off-by-one that makes people distrust the whole table.
    """
    with_tool, without = [], []
    for f in sorted(actions_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            without.append(f.name)
            continue
        assigns = any(
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "TOOL" for t in n.targets)
            for n in tree.body
        )
        reexports = any(
            isinstance(n, ast.ImportFrom)
            and any(a.name == "TOOL" for a in n.names)
            for n in tree.body
        )
        (with_tool if (assigns or reexports) else without).append(f.name)
    return with_tool, without


def main() -> None:
    actions_dir = ROOT / "actions"
    with_tool, without = files_declaring_tool(actions_dir)

    print("── actions/ — the bundled skills ─────────────────────────────")
    print(f"  files scanned          : {len(with_tool) + len(without)}")
    print(f"  declare a TOOL dict    : {len(with_tool)}")
    if without:
        print(f"  support modules (no TOOL): {len(without)} — {', '.join(without)}")

    # The number the README quotes, measured the way the model receives it.
    try:
        from core.action_loader import discover_actions
        reg = discover_actions(actions_dir)
        decls = reg.get_tool_declarations()
        payload = json.dumps(decls)
        print("\n── what is actually sent on every connection ─────────────────")
        print(f"  tool declarations      : {len(decls)}")
        print(f"  declaration JSON       : {len(payload):,} characters "
              f"(~{len(payload) // 4:,} tokens)")
        prompt = (ROOT / "core" / "prompt.txt")
        if prompt.exists():
            p = len(prompt.read_text(encoding="utf-8"))
            print(f"  core/prompt.txt        : {p:,} characters (~{p // 4:,} tokens)")
            print(f"  static total           : ~{(len(payload) + p) // 4:,} tokens "
                  f"before a word is spoken")
        biggest = sorted(((len(json.dumps(d)), d.get("name", "?")) for d in decls),
                         reverse=True)[:5]
        print("  largest declarations   : "
              + ", ".join(f"{n} ({s:,}ch)" for s, n in biggest))
    except Exception as e:                       # noqa: BLE001 - measurement tool
        print(f"\n  (live registry unavailable: {e})")

    print("\nUpdate README.md from these numbers — do not retype them by hand.")


if __name__ == "__main__":
    main()
