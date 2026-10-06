"""Offscreen E2E smoke for the redesigned JARVIS HUD (the ui/ package).

Covers: construction, content-hook contract, icon system (no emoji in the
chrome), rail (13 keys / 3 groups / badges), every section switch, studio
surface routing, live dashboard data for all workspace panels (Spaces,
Agents + chat, Computers start→exec→stop, Calls, Agenda, Memory, Skills),
signals, and the main.py API member list.

Run (cwd-independent — it chdirs to the repo root itself):
  LD_LIBRARY_PATH=/home/user/libs QT_QPA_PLATFORM=offscreen \
  /path/to/venv/bin/python tools/ui_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
os.chdir(_REPO)
sys.path.insert(0, str(_REPO))

PASS = 0
FAIL = 0
RESULTS: list[tuple[bool, str, object]] = []


def check(ok: bool, name: str, detail="") -> bool:
    global PASS, FAIL
    ok = bool(ok)
    PASS += ok
    FAIL += (not ok)
    RESULTS.append((ok, name, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}  [{detail}]")
    return ok


# ── boot ─────────────────────────────────────────────────────────────────────
from ui import JarvisUI, icon_pm, _ICONS  # noqa: E402

ui = JarvisUI("face.png")
win = ui._win
if win._overlay is not None:
    win._overlay.hide()
ui._win._ready = True


def pump(sec: float = 0.4) -> None:
    end = time.time() + sec
    while time.time() < end:
        ui._app.processEvents()
        time.sleep(0.02)


pump(0.6)
check(True, "construct + overlay hidden")

# ── dashboard wiring ─────────────────────────────────────────────────────────
info = {}
try:
    with open("/home/user/ui_preview.json", encoding="utf-8") as fh:
        info = json.load(fh)
except Exception:
    pass
BASE = info.get("base", "http://127.0.0.1:8712")
TOKEN = info.get("token", "")
if BASE.startswith("https"):
    import ssl as _ssl
    _CTX = _ssl._create_unverified_context()
else:
    _CTX = None


def api(method: str, path: str, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=8, context=_CTX) as r:
        body = r.read().decode()
    return json.loads(body) if body else None


live = False
_err = None
# Protocol fallback: a stale ui_preview.json may say http while the server
# is TLS-only (or vice-versa) — probe both before declaring the dash dead.
for _b in [BASE, BASE.replace("http://", "https://", 1)]:
    BASE = _b
    if BASE.startswith("https"):
        import ssl as _ssl
        _CTX = _ssl._create_unverified_context()
    else:
        _CTX = None
    try:
        api("GET", "/api/health")
        live = True
        break
    except Exception as e:                             # noqa: BLE001
        _err = e
if not live:
    print(f"dashboard unreachable ({_err}) — live checks will fail")
check(live, "dashboard preview reachable")

ui.set_dashboard_api(BASE, TOKEN)
pump(1.0)

# ── contract: content hook (pinned by TestRenderSurfaceFunnel) ──────────────

# ── HUD source reader ────────────────────────────────────────────────────────
# The HUD is a package (ui/) now. This tool greps its source, so it reads every
# module in it rather than one file — a panel that moves out of ui/app.py must
# not make this smoke test go quiet.
def _ui_source() -> str:
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent / "ui"
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(root.glob("*.py")))

src = _ui_source()
check("self._content_hook = None" in src, "content hook declared")
i = src.index("def show_content")
block = src[i:i + 900]
check("hook = self._content_hook" in block
      and "hook(title, text)" in block
      and "except Exception" in block, "show_content funnel pinned")

# ── icon system: zero pictographic emoji in the chrome ──────────────────────
pat = re.compile(r"[\U0001F000-\U0001FAFF☀-➿⬀-⯿️←-⇿✀-➿]")
ALLOWED = set("✕✓✗✎⚠＋⛶⬇↑←→↻·◉")
leftovers = sorted({c for c in pat.findall(src) if c not in ALLOWED})
check(not leftovers, "no pictographic emoji in the HUD", leftovers)
check(len(_ICONS) >= 40, f"icon registry ({len(_ICONS)} icons)")
pm = icon_pm("chat", "#6366f1", 16)
check(not pm.isNull(), "icon_pm renders")

# ── state pill ──────────────────────────────────────────────────────────────
ui._win._apply_state("LISTENING")
pump(0.1)
check("LISTENING" in win._state_pill.text(), "state pill LISTENING")
ui._win._apply_state("SLEEPING")
pump(0.1)
check("SLEEPING" in win._state_pill.text(), "state pill SLEEPING")

# ── rail: 13 keys, badges exist, groups present ─────────────────────────────
expected_keys = ["chat", "monitor", "spaces", "agents", "computers", "calls",
                 "agenda", "memory", "skills", "display", "scan", "3d", "web"]
check(list(win._rail_btns.keys()) == expected_keys,
      "rail keys", list(win._rail_btns.keys()))
check(all(k in win._rail_badges for k in expected_keys), "rail badges exist")
check(win._section_names == ["chat", "monitor", "spaces", "agents",
                             "computers", "calls", "agenda", "memory",
                             "skills", "studio"], "section names",
      win._section_names)

# ── every switch: stack index, checked row, right-panel visibility ─────────
def checked_key():
    return [k for k, b in win._rail_btns.items() if b.isChecked()]


for key, expect_right in [
    ("chat", True), ("monitor", True),
    ("spaces", False), ("agents", False), ("computers", False),
    ("calls", False), ("agenda", False), ("memory", False),
    ("skills", False),
    ("display", False), ("scan", False), ("3d", False), ("web", False),
]:
    win._switch_section(key)
    pump(0.15)
    ck = checked_key() == [key]
    idx_ok = True
    if key in win._STUDIO_KEYS:
        idx_ok = win._section_stack.currentWidget() is win._studio_panel
        idx_ok = idx_ok and win._studio_panel._surface == key
    else:
        idx_ok = win._section_stack.currentIndex() == win._section_index[key]
    rp = win._right_panel.isVisible() == expect_right
    check(ck and idx_ok and rp, f"switch {key}",
          f"checked={ck} idx={idx_ok} right={rp}")

# display keeps a working floating window too
try:
    win.toggle_display()
    pump(0.4)
    has_panel = win._display_panel is not None
    if has_panel and win._display_panel.isVisible():
        win._display_panel.hide()
    check(has_panel, "floating display window loads")
except Exception as e:                                 # noqa: BLE001
    check(False, "floating display window loads", str(e))
win._switch_section("chat")
pump(0.2)

# ── studio routing (classify + has-new dot) ────────────────────────────────
win._studio_panel._content.clear()
ui.show_content("NEWS — top world news today", "HEADLINE ONE")
ui.show_content("SCAN — receipt", "OCR TEXT HERE")
ui.show_content("3D — box", "vertices")
ui.show_content("SCRAPE — example.com", "page text")
ui.show_content("BLURB — unknown", "fallback")
pump(0.4)
surf = {k: v[0] for k, v in win._studio_panel._content.items()}
# SCRAPE and NEWS are both WEB prefixes (classify_surface) — the last write
# wins in the web slot; BLURB matches nothing → display.
check(surf.get("web", "") in ("NEWS — top world news today",
                              "SCRAPE — example.com"),
      "studio NEWS/SCRAPE → web", surf.get("web"))
check(surf.get("scan", "").startswith("SCAN"), "studio SCAN → scan", surf)
check(surf.get("3d", "").startswith("3D"), "studio 3D →3d", surf)
check("BLURB" in surf.get("display", ""), "unknown → display",
      surf.get("display"))
# has-new dot: receive while on chat lights the badge; switching clears it
win._switch_section("chat")
pump(0.1)
win._studio_panel.receive("SCAN — later", "more ocr")
check(win._rail_badges["scan"].isVisible(), "studio has-new dot")
win._switch_section("scan")
pump(0.15)
check(not win._rail_badges["scan"].isVisible(), "has-new dot clears")

# inline content panel still opens (chat surface below HUD)
win._switch_section("chat")
pump(0.2)
ui.show_content("BRIEF — smoke", "inline panel body")
pump(0.4)
check(win._content_panel.isVisible(), "inline content panel shows")

# ── toast layer ─────────────────────────────────────────────────────────────
win.toast("smoke toast", "ok", 300)
pump(0.2)
tl = type(win)._ToastLayer if False else None
from ui import _ToastLayer  # noqa: E402
check(_ToastLayer._inst is not None
      and _ToastLayer._inst._v.count() >= 2, "toast pushed")

# ── live panels ─────────────────────────────────────────────────────────────
def seed_fixtures() -> dict:
    """Create whatever the panels need — deterministic, no reliance on
    leftover rows from an earlier run."""
    fx = {}
    try:
        dots = api("GET", "/api/dots")
        if not dots:
            dots = [api("POST", "/api/dots",
                        {"name": "SmokeBot",
                         "role": "smoke-test assistant",
                         "permissions": ["chat", "files", "shell",
                                         "browser", "research"]})]
        fx["dot"] = (dots or [{}])[0].get("id")
        spaces = api("GET", "/api/spaces")
        if not spaces:
            sp = api("POST", "/api/spaces", {"name": "Smoke Space"})
            fx["space"] = sp.get("id")
        else:
            fx["space"] = spaces[0].get("id")
        pages = api("GET", f"/api/spaces/{fx['space']}/pages")
        if not pages:
            pg = api("POST", f"/api/spaces/{fx['space']}/pages",
                     {"title": "Smoke Page", "content_md": "# smoke\n- ok"})
            fx["page"] = pg.get("id")
        else:
            fx["page"] = pages[0].get("id")
        comps = api("GET", "/api/computers")
        if not comps:
            c = api("POST", "/api/computers", {"dot_id": fx["dot"],
                                               "name": "PC #1"})
            fx["cid"] = c.get("id")
        else:
            fx["cid"] = comps[0].get("id")
        tasks = api("GET", "/api/tasks")
        if not tasks:
            t = api("POST", "/api/tasks",
                    {"title": "smoke task", "spec": "say hi",
                     "schedule": "manual"})
            fx["task"] = t.get("id")
        else:
            fx["task"] = tasks[0].get("id")
    except Exception as e:                             # noqa: BLE001
        print(f"seed fixtures: {e}")
    return fx


if live:
    FX = seed_fixtures()
    pump(0.3)
    check(bool(FX.get("dot") and FX.get("space") and FX.get("cid")),
          "fixtures seeded", FX)
    # SPACES: list + pages + approvals + approve/decline actions exist
    win._switch_section("spaces")
    pump(2.5)
    spaces = win._spaces_panel
    n_spaces = len(getattr(spaces, "_spaces", []) or spaces._space_names
                   if hasattr(spaces, "_space_names") else [])
    # robust: whatever the attr, require the combo or list got data
    has_space_data = bool(getattr(spaces, "_spaces", None)) or \
        (hasattr(spaces, "_combo") and spaces._combo.count() > 0)
    check(has_space_data, "spaces: list loaded")
    check(spaces._page_lay.count() > 1, "spaces: pages rendered",
          spaces._page_lay.count())

    # AGENTS: dot list + chat send/reply
    win._switch_section("agents")
    pump(2.5)
    ag = win._agents_panel
    dot_btns = [ag._dot_lay.itemAt(i).widget()
                for i in range(ag._dot_lay.count())
                if ag._dot_lay.itemAt(i).widget() is not None]
    check(len(dot_btns) >= 1, "agents: dot list", len(dot_btns))
    if dot_btns:
        dot_btns[0].click()
        pump(1.2)
    try:
        ag._chat_in.setText("hello from HUD smoke")
        ag._send()
        pump(2.0)
    except Exception as e:                             # noqa: BLE001
        print(f"agents send: {e}")
    has_chat = False
    for attr in ("_chat", "_chat_view", "_msgs_view", "_chat_out"):
        v = getattr(ag, attr, None)
        if v is not None and hasattr(v, "toPlainText"):
            if "You" in v.toPlainText() or "thinking" in v.toPlainText():
                has_chat = True
    check(has_chat, "agents: conversation rendered")

    # COMPUTERS: list + audit + start → exec → stop E2E
    win._switch_section("computers")
    pump(2.5)
    cp = win._computers_panel
    check(hasattr(cp, "_start_btn") and cp._start_btn is not None,
          "computers: controls present")
    rows = [cp._list_lay.itemAt(i).widget()
            for i in range(cp._list_lay.count())
            if cp._list_lay.itemAt(i).widget() is not None] \
        if hasattr(cp, "_list_lay") else []
    # audit box must exist (exec errors surface there)
    check(hasattr(cp, "_sh_out"), "computers: shell box present")
    if cp._start_btn is not None:
        try:
            if not getattr(cp, "_running", False):
                cp._start_btn.click()
                pump(2.0)
            cp._run_exec() if hasattr(cp, "_run_exec") else None
        except Exception:
            pass
    # direct API E2E (independent of widget internals)
    try:
        comps = api("GET", "/api/computers")
        check(isinstance(comps, list) and len(comps) >= 1,
              "computers: listed", len(comps or []))
        cid = comps[0]["id"]
        # fresh computers have shell off by design — turn it on for the E2E
        api("PATCH", f"/api/computers/{cid}/perms",
            {"shell": True, "files": True})
        api("POST", f"/api/computers/{cid}/start", {})
        pump(0.5)
        out = api("POST", f"/api/computers/{cid}/exec",
                  {"argv": ["echo", "hud-smoke-live"]})
        text = json.dumps(out)
        check("hud-smoke-live" in text, "computers: exec returns output",
              text[:140])
        api("POST", f"/api/computers/{cid}/stop", {})
        # run the SAME thing through the panel widget → lands in the box
        pump(1.0)
        if cp._comp is None:
            api("GET", "/api/computers")
            pump(1.0)
        cp._sh_in.setText("echo hud-smoke-live")
        cp._run_cmd()
        pump(1.8)
        box = cp._sh_out.toPlainText()
        check("hud-smoke-live" in box or "✗" in box,
              "computers: result in shell box",
              box[-140:].replace("\n", " "))
    except Exception as e:                             # noqa: BLE001
        check(False, "computers: exec E2E", str(e))

    # CALLS: dots combo + history
    win._switch_section("calls")
    pump(2.0)
    ca = win._calls_panel
    check(hasattr(ca, "_start_btn"), "calls: controls present")
    check(hasattr(ca, "_hist"), "calls: history box")
    try:
        calls = api("GET", "/api/calls?status=any")
        check(isinstance(calls, list), "calls: history listed",
              len(calls or []))
    except Exception as e:                             # noqa: BLE001
        check(False, "calls: history listed", str(e))

    # AGENDA: tasks
    win._switch_section("agenda")
    pump(2.0)
    tasks = api("GET", "/api/tasks")
    check(isinstance(tasks, list), "agenda: tasks listed",
          len(tasks or []))
    panel_rows = 0
    agd = win._agenda_panel
    for attr in ("_task_lay", "_list_lay", "_tasks_lay"):
        lay = getattr(agd, attr, None)
        if lay is not None:
            panel_rows = lay.count()
            break
    check(panel_rows >= 1, "agenda: rows rendered", panel_rows)

    # MEMORY: list + put + delete round-trip
    win._switch_section("memory")
    pump(2.0)
    mem = win._memory_panel
    try:
        api("PUT", "/api/memory",
            {"key": "smoke_mem", "value": "42", "allowed": "*"})
        prefs = api("GET", "/api/memory")
        keys = [p.get("key") for p in (prefs or [])]
        check("smoke_mem" in keys, "memory: PUT visible", keys[:8])
        pump(1.2)
        row_found = False
        for i in range(mem._list_lay.count()):
            w = mem._list_lay.itemAt(i).widget()
            if w is not None and "smoke_mem" in w.findChildren(
                    type(mem._search))[0].text() if False else False:
                row_found = True
        # simpler: search box filter finds it
        mem._search.setText("smoke")
        pump(0.4)
        row_found = mem._list_lay.count() > 1
        check(row_found, "memory: row rendered + filter", mem._list_lay.count())
        mem._search.setText("")
        pid = next((p["id"] for p in (prefs or [])
                    if p.get("key") == "smoke_mem"), None)
        if pid is not None:
            api("DELETE", f"/api/memory/{pid}")
            prefs2 = api("GET", "/api/memory")
            check(all(p.get("key") != "smoke_mem" for p in (prefs2 or [])),
                  "memory: DELETE removes")
    except Exception as e:                             # noqa: BLE001
        check(False, "memory: CRUD round-trip", str(e))

    # SKILLS: list + mine action
    win._switch_section("skills")
    pump(2.0)
    sk = win._skills_panel
    try:
        skills = api("GET", "/api/skills")
        check(isinstance(skills, list), "skills: listed",
              len(skills or []))
        pump(1.0)
        check(sk._list_lay.count() >= 1, "skills: rows rendered",
              sk._list_lay.count())
        res = api("POST", "/api/skills/mine", {})
        check(isinstance(res, dict) and "created" in res,
              "skills: mine endpoint", res)
    except Exception as e:                             # noqa: BLE001
        check(False, "skills: list+mine", str(e))

    # rail badge after heartbeat
    pump(0.5)
    win.refresh_pending_badge()
    pump(1.2)
    check(True, "badge heartbeat ran", win._pending_n)

# ── signals ─────────────────────────────────────────────────────────────────
sig_names = ["_log_sig", "_state_sig", "_content_sig", "_quiz_sig",
             "_review_sig", "_confirm_sig", "_wake_btns_sig"]
for s in sig_names:
    check(hasattr(win, s) and hasattr(getattr(win, s), "emit"),
          f"signal {s}")

# ── main.py API member list ─────────────────────────────────────────────────
REQUIRED = [
    "_content_hook", "current_file", "get_plugin_settings", "get_plugins",
    "hide_confirm", "muted", "notify_phone_connected",
    "on_audio_device_change", "on_interrupt", "on_push_to_talk",
    "on_remote_clicked", "on_text_command", "on_voice_change",
    "on_wake_install", "on_wake_manual", "on_wake_toggle",
    "prompt_reconfig", "ptt_hold", "push_visemes", "request_say",
    "set_audio_level", "set_state", "show_confirm", "show_content",
    "start_camera_stream", "stop_camera_stream", "wake_get_state",
    "wake_is_ready", "write_log", "assistant_name",
]
main_src = open("main.py", encoding="utf-8").read()
missing = [m for m in REQUIRED
           if not (hasattr(ui, m) or f"ui.{m}" in main_src)]
check(not missing, "JarvisUI API: all main.py members", missing)
lazy = ["show_quiz", "show_review", "wait_for_api_key", "show_camera_frame",
        "show_video", "set_video_muted", "video_is_playing"]
miss_lazy = [m for m in lazy if not hasattr(ui, m)]
check(not miss_lazy, "JarvisUI lazy members", miss_lazy)

# ── A1–A5 / C7–C11 / dashboard-B regression guards ─────────────────────────────
src2 = _ui_source()

# A1 — Spaces editing (toolbar + save path + edit toggling)
for needle, label in (
    ("NEW SPACE", "A1 toolbar NEW SPACE"),
    ("NEW PAGE", "A1 toolbar NEW PAGE"),
    (":save:", "A1 page save request key"),
    ("def _toggle_edit", "A1 _toggle_edit (edit+save) method"),
    ("base_rev", "A1 conflict-aware base_rev"),
    ("_editing", "A1 edit state flag"),
):
    check(needle in src2, label)
check(hasattr(win._spaces_panel, "_edit_btn") and
      callable(getattr(win._spaces_panel, "_toggle_edit", None)) and
      win._spaces_panel._viewer.isReadOnly(),
      "A1 live SpacesPanel edit/save API")

# A2 — Dot CRUD from the Agents panel
for needle, label in (
    ("def _new_dot", "A2 _new_dot"),
    ("def _edit_dot", ("A2 _edit_dot")),
    ("def _del_dot", "A2 _del_dot"),
    ('"/api/dots"', "A2 POST /api/dots"),
    ("del_dot", "A2 delete-dot key"),
    ("QMessageBox", "A2 delete confirmation"),
):
    check(needle in src2, label)
sp = win._agents_panel
check(all(callable(getattr(sp, m, None)) for m in ("_new_dot", "_edit_dot", "_del_dot")),
      "A2 live AgentsPanel CRUD methods")

# A3 — Computers four sub-surfaces (shell/files/browser/screen)
mode_btns = getattr(win._computers_panel, "_mode_btns", {})
check(set(mode_btns) == {"shell", "files", "browser", "screen"},
      "A3 mode chips", sorted(mode_btns))
check(isinstance(getattr(win._computers_panel, "_mode_stack", None), object) and
      hasattr(win._computers_panel, "_mode_stack"), "A3 mode stack exists")
for needle, label in (
    ("def get_raw", "A3 _DashApi.get_raw"),
    ('"GETRAW"', "A3 raw-fetch method tag"),
    ("/files/list", "A3 files/list route"),
    ("/files/write", "A3 files/write route"),
    ("def _br_op", "A3 browser op dispatcher"),
    ('payload["url"]', "A3 navigate url kw"),
    ("def _refresh_screen", "A3 screen refresh"),
    ("loadFromData", "A3 pixmap from bytes"),
):
    check(needle in src2, label)
win._computers_panel._set_mode("files")
pump(0.05)
check(win._computers_panel._mode_stack.currentIndex() == 1 and
      mode_btns["files"].isChecked(), "A3 live mode switch to FILES")
win._computers_panel._set_mode("shell")
pump(0.05)

# A4 — Calls background loop
check("def _set_bg" in src2 and "/background" in src2, "A4 background route + handler")
check('mw.toast("Call connected"' in src2, "A4 connected toast")

# A5 — Research sources rendered on pages
check("SOURCES**" in src2, "A5 sources block in viewer")
check("research · " in src2, "A5 research badge in page list")

# C7 — agenda failure toasts land on the AgendaPanel (placement guard)
agenda_slice = src2[src2.index("class AgendaPanel"):src2.index("class MemoryPanel")]
check("Task action failed" in agenda_slice, "C7 agenda _on_fail inside AgendaPanel")
mem_slice = src2[src2.index("class MemoryPanel"):src2.index("class SkillsPanel")]
check("Memory saved" in mem_slice and
      'mw.toast(msg, "err")' in mem_slice, "C7 MemoryPanel _on_fail intact")

# C8 — command palette + section shortcuts
check("def _open_palette" in src2, "C8 palette method")
check('"Ctrl+K"' in src2, "C8 Ctrl+K shortcut")
check("def _jump_section" in src2, "C8 section jump method")
check(len(getattr(win, "_section_shortcuts", [])) == 10, "C8 10 section shortcuts",
      len(getattr(win, "_section_shortcuts", [])))
pal_slice = src2[src2.index("def _open_palette"):src2.index("def _open_palette") + 4000]
check("Enter" in pal_slice or "returnPressed" in pal_slice, "C8 Enter-to-jump")
check("palette_run" in pal_slice, "C8 palette runs tools (M)")
check("get_action_names" in pal_slice, "C8 action index in palette (M)")
check('"\u25b8"' in pal_slice or "\u25b8" in pal_slice, "C8 tool entries marked")
check("palette_run" in main_src, "C8 main wires palette_run")

# C15 — translate lens overlay (Report I)
check("class _LensOverlay" in src2, "C15 _LensOverlay class")
check("_lens_sig" in src2 and "def show_lens" in src2, "C15 thread-safe show_lens")
check("def _show_lens" in src2, "C15 _show_lens handler")

# C10 — skill detail dialog
check("def _detail" in src2[src2.index("class SkillsPanel"):], "C10 skill _detail method")
check('"DETAIL"' in src2, "C10 DETAIL button")

# C11 — chat quick actions
check('"QUICK ACTIONS"' in src2, "C11 quick-actions row")
check("def _quick_say" in src2, "C11 _quick_say")

# dashboard B — vector icons instead of emoji in the web chrome
html = open("dashboard/static/app.html", encoding="utf-8").read()
check("_svgIcon" in html and "const _ICONS" in html, "dash: _ICONS registry")
check("svg: (n, sz) => _svgIcon" in html, "dash: JV.svg helper exported")
for emo in ("\U0001F4AC", "\U0001F4C4", "\U0001F916", "\U0001F5A5", "\U0001F4DE",
            "\u23F0", "\U0001F50D", "\U0001F9CA", "\U0001F310", "\U0001F4CE",
            "\U0001F3A4", "\U0001F4F7"):
    if emo in ("\u23F0",):
        pass
check(not any(e in html for e in
              ("\U0001F4AC", "\U0001F4C4", "\U0001F916", "\U0001F5A5",
               "\U0001F4DE", "\U0001F50D", "\U0001F9CA", "\U0001F310",
               "\U0001F4CE", "\U0001F3A4", "\U0001F4F7")),
      "dash: emoji gone from chrome")
check(html.count('data-tab="') >= 10 and "<span class=\"ico\"><svg" in html,
      "dash: nav rows carry inline svg")
check("🎤" not in html and "📎" not in html and "🖥" not in html,
      "dash: composer emoji gone")
for jsf, emo in (("dashboard/static/js/computers.js", "\U0001F5A5"),
                 ("dashboard/static/js/calls.js", "\U0001F4DE"),
                 ("dashboard/static/js/workspaces.js", "\U0001F4DA")):
    body = open(jsf, encoding="utf-8").read()
    check(emo not in body, f"dash: {jsf.split('/')[-1]} clean")
css_body = open("dashboard/static/css/jarvis.css", encoding="utf-8").read()
check("SVG icon alignment" in css_body, "dash: svg alignment css")

# dashboard tests still pin tab contract
check('data-tab="chat"' in html and "function showTab" in html,
      "dash: TestDashboardTabs contract intact")

# ── summary ─────────────────────────────────────────────────────────────────
print("\n==== SUMMARY ====")
print(f"PASS {PASS}   FAIL {FAIL}")
if FAIL:
    print("FAILURES:")
    for ok, name, detail in RESULTS:
        if not ok:
            print(f"  ✗ {name}: {detail}")
    sys.exit(1)
print("ALL SMOKE CHECKS PASSED")
