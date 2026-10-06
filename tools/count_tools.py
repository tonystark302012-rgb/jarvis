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
    with_tool: list[str] = []
    without: list[str] = []
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
        from core import tool_tiers as tt
        reg = discover_actions(actions_dir)
        decls = reg.get_tool_declarations()
        prompt_len = 0
        prompt_path = ROOT / "core" / "prompt.txt"
        if prompt_path.exists():
            prompt_len = len(prompt_path.read_text(encoding="utf-8"))

        print("\n── what is actually sent on every connection ─────────────────")
        print(f"  tool declarations      : {len(decls)}")
        biggest = sorted(((len(json.dumps(d)), d.get("name", "?")) for d in decls),
                         reverse=True)[:5]
        print("  largest declarations   : "
              + ", ".join(f"{n} ({s:,}ch)" for s, n in biggest))

        # The system prompt repeats every tool as a one-line capability, which
        # is the duplicate nobody counts. Measure it the way _describe_tools
        # renders it, not as the raw declaration JSON.
        def capabilities(ds: list[dict]) -> int:
            return sum(len(f"- {d['name']}: {d['description'][:150]}") + 1
                   for d in ds)

        before = len(json.dumps(decls)) + capabilities(decls) + prompt_len

        core, deferred = tt.split_declarations(decls)
        after_decls = core + [tt.router_declaration()]
        after = (len(json.dumps(after_decls))
                 + capabilities(core)
                 + len(tt.hint_for_prompt(deferred))
                 + prompt_len)

        print("\n  ┌─ WITHOUT tiering (what the model used to get)")
        print(f"  │    declarations        : {len(decls)} tools, "
              f"{len(json.dumps(decls)):,} chars")
        print(f"  │    capabilities list   : {capabilities(decls):,} chars "
              f"(duplicate of the same {len(decls)} descriptions)")
        print(f"  └    TOTAL               : {before:,} chars ≈ {before // 4:,} tokens")

        print("\n  ┌─ WITH tiering (current default)")
        print(f"  │    declared now        : {len(core)} tools + router, "
              f"{len(json.dumps(after_decls)):,} chars")
        print(f"  │    deferred            : {len(deferred)} tools, reached via "
              f"`toolbox`")
        print(f"  │    capabilities list   : {capabilities(core):,} chars + "
              f"{len(tt.hint_for_prompt(deferred)):,} for the names")
        print(f"  └    TOTAL               : {after:,} chars ≈ {after // 4:,} tokens")

        saved = before - after
        print(f"\n  SAVED: {saved:,} characters ≈ {saved // 4:,} tokens "
              f"({100 - after * 100 // before}% smaller), every connection.")
        print("  A deferred tool costs one extra round trip — `toolbox "
              "action=search` then `action=run`.")
    except Exception as e:                       # noqa: BLE001 - measurement tool
        print(f"\n  (live registry unavailable: {e})")

    print("\nUpdate README.md from these numbers — do not retype them by hand.")


if __name__ == "__main__":
    main()
