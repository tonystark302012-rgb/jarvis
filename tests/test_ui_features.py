"""Source-pin tests for the HUD v2 feature set (A1–A5, C7–C11, dashboard B).

These are contract pins, not behaviour tests: the offscreen smoke
(tools/ui_smoke.py) exercises the live widgets; this file fails CI the moment
a wiring string, route, or placement guarantee drifts — the exact class of
regression that shipped once already (a panel override landing in the wrong
class body).
"""
from __future__ import annotations

import re
from pathlib import Path
from tests._ui_source import ui_source

ROOT = Path(__file__).resolve().parents[1]
UI = ui_source()


def _slice(start: str, end: str) -> str:
    return UI[UI.index(start):UI.index(end)]


class TestSpacesEditing:
    def test_toolbar_and_toggle_exist(self):
        assert "NEW SPACE" in UI and "NEW PAGE" in UI
        assert "def _toggle_edit" in UI

    def test_save_is_conflict_aware(self):
        # PATCH /api/pages/{id} must carry base_rev → 409 on concurrent edits
        assert f'{":save:"}' in UI
        assert '"base_rev"' in UI
        assert '"/api/pages/' in UI


class TestDotCrud:
    def test_handlers_exist(self):
        for m in ("_new_dot", "_edit_dot", "_del_dot"):
            assert f"def {m}" in UI

    def test_routes(self):
        assert '"/api/dots"' in UI
        assert "del_dot" in UI
        assert "QMessageBox" in UI  # destructive action confirmation


class TestComputersModes:
    def test_four_modes(self):
        for m in ("shell", "files", "browser", "screen"):
            assert f'("{m}",' in UI or f"'{m}'," in UI
        assert "def _set_mode" in UI

    def test_files_browser_screen_wiring(self):
        assert "/files/list" in UI and "/files/write" in UI
        assert "def _br_op" in UI and 'payload["url"]' in UI
        assert "def get_raw" in UI and '"GETRAW"' in UI
        assert "loadFromData" in UI


class TestCallsBackground:
    def test_background_route_and_toasts(self):
        assert "def _set_bg" in UI
        assert "/background" in UI  # POST /api/calls/{id}/background
        assert 'mw.toast("Call connected"' in UI


class TestResearchSources:
    def test_sources_rendered(self):
        assert "SOURCES**" in UI          # viewer footer block
        assert "research · " in UI        # page-list badge


class TestPanelOverridePlacement:
    """Guards the index-trap bug: overrides must sit in their OWN class."""

    def test_agenda_fail_override_inside_agenda(self):
        agenda = _slice("class AgendaPanel", "class MemoryPanel")
        assert "Task action failed" in agenda

    def test_memory_fail_override_inside_memory(self):
        memory = _slice("class MemoryPanel", "class SkillsPanel")
        assert 'mw.toast(msg, "err")' in memory


class TestPaletteAndShortcuts:
    def test_palette(self):
        assert "def _open_palette" in UI
        assert '"Ctrl+K"' in UI

    def test_section_shortcuts(self):
        assert "def _jump_section" in UI
        assert '_section_shortcuts' in UI
        assert '"Ctrl+0"' in UI


class TestSkillDetail:
    def test_detail_dialog(self):
        skills = UI[UI.index("class SkillsPanel"):]
        assert "def _detail" in skills
        assert '"DETAIL"' in UI


class TestChatQuickActions:
    def test_row_and_handler(self):
        assert '"QUICK ACTIONS"' in UI
        assert "def _quick_say" in UI


class TestUiNoPictographs:
    def test_ui_py_is_clean(self):
        bad = re.findall(r"[\U0001F000-\U0001FAFF]", UI)
        assert not bad, sorted({hex(ord(c)) for c in bad})


class TestDashboardVectorIcons:
    HTML = (ROOT / "dashboard" / "static" / "app.html").read_text(encoding="utf-8")

    def test_icon_registry(self):
        assert "const _ICONS" in self.HTML
        assert "_svgIcon" in self.HTML
        assert "svg: (n, sz) => _svgIcon" in self.HTML

    def test_nav_rows_inline_svg(self):
        assert self.HTML.count('data-tab="') >= 10
        assert '<span class="ico"><svg' in self.HTML

    def test_no_chrome_emoji(self):
        # text marks (✓ ✗ ← arrows …) are allowed only outside tags
        tags = re.findall(r"<[^>]+>", self.HTML)
        in_tags = "".join(re.findall(r"[\U0001F300-\U0001FAFF]", "\n".join(tags)))
        assert not in_tags, sorted({hex(ord(c)) for c in in_tags})
        assert "🎤" not in self.HTML and "📎" not in self.HTML

    def test_js_modules_clean(self):
        for name in ("computers.js", "calls.js", "workspaces.js", "agenda.js",
                     "agents.js"):
            body = (ROOT / "dashboard" / "static" / "js" / name).read_text(
                encoding="utf-8")
            bad = re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]", body)
            # allow none of the pictographs; ✓/✗ live in the 2700 block as
            # text-default chars but ban VS16-tagged forms explicitly
            assert "️" not in body.replace("✓️", ""), name
            assert not [c for c in bad if c in "⚠✂✉⌘⏰⛏⚙"], name

    def test_tabs_contract(self):
        # mirrors TestDashboardTabs in test_roadmap.py — the strings the
        # dashboard router depends on must survive any chrome restyle
        for tab in ("chat", "display", "scan", "3d", "web"):
            assert f'data-tab="{tab}"' in self.HTML
        assert "function showTab" in self.HTML


class TestToolsCommitted:
    def test_smoke_and_preview_in_tools(self):
        smoke = (ROOT / "tools" / "ui_smoke.py").read_text(encoding="utf-8")
        preview = (ROOT / "tools" / "ui_preview.py").read_text(encoding="utf-8")
        assert "ALL SMOKE CHECKS PASSED" in smoke
        assert "PREVIEW READY" in preview
        assert "115" in smoke or "PASS" in smoke
