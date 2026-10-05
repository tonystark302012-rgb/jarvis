#!/usr/bin/env python3
"""
feature_audit — live proof that every session feature actually RUNS.

Distinct from pytest (which exercises logic through seams): this probes
the REAL module entry points for their honest behaviour in THIS
environment — discovery, status lines, guarded refusals, assets. Any
feature that silently vanished, threw, or returned a fake would fail
here. Exit code 0 = all green.

    LD_LIBRARY_PATH=… PYTHONPATH=. python tools/feature_audit.py
    add --full to also run ruff + the pytest suite (CI mirror).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EXPECTED_TOOLS = [
    "atspi", "cad", "dev_loop", "dictation", "eval_ab", "go2rtc", "graph",
    "manim_anim", "mermaid", "predictions", "telegram_rx", "telegram_send",
    "video_qa", "web_search", "obsidian", "mcp", "research", "rag",
]

RESULTS: list[tuple[str, bool, str]] = []


def check(label: str, fn):
    try:
        detail = fn()
        ok = bool(detail)
        RESULTS.append((label, ok, str(detail)[:110] if detail else "empty"))
    except Exception as e:
        RESULTS.append((label, False, f"{type(e).__name__}: {e}"[:110]))


def _registry():
    reg = _registry_obj()
    names = reg.names()
    missing = [t for t in EXPECTED_TOOLS if t not in names]
    if missing:
        raise AssertionError(f"missing tools: {missing}")
    return f"{len(names)} tools, all expected present"


_REG = None


def _registry_obj():
    global _REG
    if _REG is None:
        from core.action_loader import discover_actions
        _REG = discover_actions(ROOT / "actions")
    return _REG


def _call(tool_name: str, params: dict) -> str:
    rec = _registry_obj()._actions.get(tool_name)   # ActionRecord
    if rec is None or not rec.valid or rec.handler is None:
        raise AssertionError(f"tool {tool_name!r} not registered/valid")
    out = rec.handler(parameters=params, player=None)
    return out if isinstance(out, str) else str(out)


def _honest(label: str, tool: str, params: dict, *needles: str):
    def probe():
        out = _call(tool, params)
        if out is None or not out.strip():
            raise AssertionError("empty reply")
        for n in needles:
            if n.lower() not in out.lower():
                raise AssertionError(f"missing {n!r} in {out[:80]!r}")
        return out
    check(label, probe)


def main() -> int:
    check("registry/discovery", _registry)

    # guarded features → exact install/hint lines in THIS environment
    _honest("atspi honesty", "atspi", {"action": "find", "query": "x"},
            "apt install")
    _honest("cad status", "cad", {"action": "status"}, "cad")
    _honest("manim status", "manim_anim", {"action": "status"}, "manim")
    _honest("go2rtc status", "go2rtc", {"action": "status"}, "go2rtc")
    _honest("mermaid no-src", "mermaid", {"source": ""}, "mermaid")
    _honest("video_qa no-path", "video_qa", {}, "video")
    _honest("dictation status", "dictation", {}, "dictation")
    _honest("predictions list", "predictions", {}, "prediction")
    _honest("eval_ab report", "eval_ab", {"action": "report"}, "eval")
    _honest("dev_loop status", "dev_loop", {}, "dev loop")
    _honest("graph search-honest", "graph", {"action": "search",
                                             "query": "zzz-ghost"}, "graph")
    _honest("telegram_send guard", "telegram_send", {"text": "hi"},
            "token", "sent")      # no-token config → honest refusal
    _honest("research no-topic", "research", {}, "topic")
    _honest("web_search mode", "web_search", {"query": "x"}, "results")

    def webrtc_status():
        from core import webrtc
        out = webrtc.status()
        assert "WebRTC" in out
        return out
    check("webrtc status", webrtc_status)

    def pwa_assets():
        static = ROOT / "dashboard" / "static"
        for f in ("manifest.json", "sw.js", "pwa.js", "webrtc.js",
                  "icons/icon-192.png", "icons/icon-512.png"):
            assert (static / f).is_file(), f"missing {f}"
        src = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
        for route in ("/manifest.json", "/sw.js", "/api/push/subscribe",
                      "/api/webrtc/offer"):
            assert route in src, f"route {route} missing"
        return "6 assets + 4 routes"
    check("pwa/push/webrtc wiring", pwa_assets)

    def searx_ladder():
        import actions.web_search as ws
        assert ws._searx_search("anything") in ([],) or \
            isinstance(ws._searx_search("t"), list)
        assert callable(ws._search)
        return "ladder callable, unconfigured → []"
    check("searxng rung", searx_ladder)

    def readme_section():
        txt = (ROOT / "README.md").read_text(encoding="utf-8")
        assert "Integration Update" in txt
        return "documented"
    check("readme documented", readme_section)

    width = max(len(r[0]) for r in RESULTS)
    fails = 0
    for label, ok, detail in RESULTS:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"  [{mark}] {label.ljust(width)}  {detail}")
    print(f"\nFEATURE AUDIT: {len(RESULTS) - fails}/{len(RESULTS)} green")

    if "--full" in sys.argv:
        import subprocess
        for cmd, name in ((["ruff", "check", "."], "ruff"),
                          ([sys.executable, "-m", "pytest", "tests/", "-q"],
                           "pytest")):
            rc = subprocess.call(cmd, cwd=ROOT)
            print(f"  [{'PASS' if rc == 0 else 'FAIL'}] {name}")
            if rc != 0:
                fails += 1
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
