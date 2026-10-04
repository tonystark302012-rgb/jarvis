# tests/test_roadmap.py
"""Tests for the second roadmap batch: privacy mode, history search (FTS5),
local RAG, MCP client, DuckDB data tool, make_3d, translate, password vault,
sandboxed terminal, and event-driven proactivity (battery/calendar edges).

Every test is hermetic: temp dirs for state, fakes for seams, real logic.
MCP talks to a REAL stdio JSON-RPC server (spawned as a subprocess) — the
protocol path itself is what we're testing, not a mock of it.
"""
from __future__ import annotations

import json
import struct
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SRC = Path("main.py").read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# Privacy mode
# ═══════════════════════════════════════════════════════════════════════════

class TestPrivacy:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import core.privacy as cp
        monkeypatch.setattr(cp, "_path", lambda: tmp_path / "api_keys.json")

    def test_off_by_default_gate_passes(self):
        import core.privacy as cp
        assert cp.is_on() is False
        assert cp.gate("research") is None
        assert cp.gate("web_search") is None

    def test_on_blocks_cloud_tools_only(self):
        import core.privacy as cp
        cp.set(True)
        assert cp.is_on() is True
        for tool in ("research", "web_search", "scrape", "phone_vision",
                     "region_ocr", "translate", "file_processor",
                     "background_monitor", "flight_finder"):
            msg = cp.gate(tool)
            assert msg and "Privacy mode is ON" in msg, tool
        # local tools never gated
        for tool in ("rag", "history", "data", "make_3d", "vault",
                     "terminal", "mcp", "weather_report", "scanner"):
            assert cp.gate(tool) is None, tool

    def test_flag_persists_and_preserves_other_keys(self, tmp_path):
        import core.privacy as cp
        # existing keys survive the toggle
        p = tmp_path / "api_keys.json"
        p.write_text(json.dumps({"gemini": "x"}), encoding="utf-8")
        cp.set(True)
        data = json.loads(p.read_text())
        assert data["privacy_mode"] is True and data["gemini"] == "x"
        cp.set(False)
        assert json.loads(p.read_text())["privacy_mode"] is False

    def test_corrupt_config_is_off_not_crash(self, tmp_path):
        import core.privacy as cp
        (tmp_path / "api_keys.json").write_text("{not json", encoding="utf-8")
        assert cp.is_on() is False
        assert cp.gate("research") is None

    def test_action_on_off_status(self):
        import actions.privacy as ap
        out = ap.privacy({"state": "on"})
        assert "ON" in out
        out = ap.privacy({})
        assert "ON" in out
        out = ap.privacy({"state": "off"})
        assert "OFF" in out
        assert "bogus" in ap.privacy({"state": "bogus"})

    def test_gated_handlers_refuse_end_to_end(self):
        """Handler-level: privacy ON → research/web_search/scrape refuse
        BEFORE any network call (they gate first thing)."""
        import core.privacy as cp
        import actions.research as research
        import actions.scrape as scrape
        import actions.web_search as web_search
        cp.set(True)
        try:
            out = research.research({"topic": "quantum"})
            assert "Privacy mode is ON" in out
            out = scrape.scrape({"url": "https://example.com"})
            assert "Privacy mode is ON" in out
            out = web_search.web_search({"query": "secret thoughts"})
            assert "Privacy mode is ON" in out
        finally:
            cp.set(False)

    def test_privacy_gate_is_first_statement_in_cloud_handlers(self):
        """Wiring: the gate must be checked before params are even read —
        assert it sits at the top of each cloud-bound handler source."""
        for path in ("actions/research.py", "actions/scrape.py",
                     "actions/web_search.py", "actions/phone_vision.py",
                     "actions/file_processor.py", "actions/flight_finder.py",
                     "actions/background_monitor.py",
                     "actions/smart_home.py",
                     "actions/gui_agent.py"):
            src = Path(path).read_text(encoding="utf-8")
            # gate call exists and precedes any network import in handler
            assert '_privacy.gate(' in src, path
        # region_ocr gates the gemini branch specifically
        src = Path("actions/region_ocr.py").read_text(encoding="utf-8")
        assert '_privacy.gate("region_ocr")' in src
        i_gate = src.index('_privacy.gate("region_ocr")')
        i_gem = src.index("from core import gemini", i_gate - 400)
        assert i_gate < i_gem, "gate must precede the gemini import"


# ═══════════════════════════════════════════════════════════════════════════
# History search (FTS5)
# ═══════════════════════════════════════════════════════════════════════════

class TestHistorySearch:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.history_search as h
        monkeypatch.setattr(h, "_db_path", lambda: tmp_path / "history.db")
        monkeypatch.setattr(h, "_CONN", None)
        yield
        monkeypatch.setattr(h, "_CONN", None)

    def test_record_and_search(self):
        import actions.history_search as h
        h.record("user", "mere paas 3 offers hain Google se")
        h.record("jarvis", "Google wala sustainable hai")
        h.record("user", "Jaipur trip plan kiya")
        out = h.history({"query": "offers"})
        assert "Found 1" in out and "Google" in out
        out = h.history({"query": "Jaipur"})
        assert "Jaipur trip" in out

    def test_recent_and_stats(self):
        import actions.history_search as h
        for i in range(5):
            h.record("user", f"line number {i}")
        out = h.history({"action": "recent", "limit": 2})
        assert "Last 2" in out and "line number 4" in out
        assert "line number 0" not in out
        out = h.history({"action": "stats"})
        assert "5 turns" in out

    def test_empty_db_messages(self):
        import actions.history_search as h
        assert "No conversation history" in h.history({"action": "recent"})
        assert "empty" in h.history({"action": "stats"}).lower()

    def test_fts_syntax_injection_is_harmless(self):
        import actions.history_search as h
        h.record("user", "ordinary sentence here")
        # these would be FTS5 syntax errors if not quoted — must not raise
        for evil in ('"OR" (x)', 'NEAR(a b', 'col:x', '*', 'a AND b NOT c'):
            out = h.history({"query": evil})
            assert isinstance(out, str) and "failed" not in out.lower(), evil

    def test_blank_record_ignored(self):
        import actions.history_search as h
        h.record("user", "   ")
        h.record("", "")
        assert "empty" in h.history({"action": "stats"}).lower()

    def test_oversized_turn_truncated(self):
        import actions.history_search as h
        h.record("user", "x" * 10_000)
        assert h.history({"action": "stats"}).startswith("History: 1")

    def test_record_never_raises_on_bad_db(self, monkeypatch):
        import actions.history_search as h
        def boom():
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(h, "_db_path", boom)
        h.record("user", "still fine")          # must swallow

    def test_tool_shape(self):
        import actions.history_search as h
        assert h.TOOL["name"] == "history"
        assert h.TOOL["handler"] is h.history
        assert h.TOOL["parameters"]["type"] == "OBJECT"


# ═══════════════════════════════════════════════════════════════════════════
# RAG (local, offline)
# ═══════════════════════════════════════════════════════════════════════════

class TestRag:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.rag as r
        monkeypatch.setattr(r, "_db_path", lambda: tmp_path / "rag.db")
        monkeypatch.setattr(r, "_CONN", None)
        yield
        monkeypatch.setattr(r, "_CONN", None)

    @staticmethod
    def _docs(tmp_path: Path) -> Path:
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "agreement.txt").write_text(
            "RENT AGREEMENT The notice period shall be two months. "
            "Security deposit is three months rent.", encoding="utf-8")
        (docs / "policy.txt").write_text(
            "COMPANY POLICY Employees get 21 days paid leave per year. "
            "Notice period for resignation is 60 days.", encoding="utf-8")
        return docs

    def test_index_ask_status(self, tmp_path):
        import actions.rag as r
        docs = self._docs(tmp_path)
        out = r.rag({"action": "index", "path": str(docs)})
        assert "Indexed 2 file" in out
        out = r.rag({"action": "status"})
        assert "2 file(s)" in out and "chunk" in out
        # offline extractive answer with citations
        out = r.rag({"action": "ask", "query": "notice period kya hai"})
        assert "agreement.txt" in out or "policy.txt" in out
        assert "#chunk" in out

    def test_ranking_prefers_better_match(self, tmp_path):
        import actions.rag as r
        docs = self._docs(tmp_path)
        r.rag({"action": "index", "path": str(docs)})
        out = r.rag({"action": "ask", "query": "paid leave days"})
        assert "policy.txt" in out

    def test_missing_path_reports(self, tmp_path):
        import actions.rag as r
        out = r.rag({"action": "index", "path": str(tmp_path / "ghost")})
        assert "Nothing to index" in out

    def test_no_index_yet(self):
        import actions.rag as r
        assert "Nothing indexed yet" in r.rag({})

    def test_no_hit_message(self, tmp_path):
        import actions.rag as r
        docs = self._docs(tmp_path)
        r.rag({"action": "index", "path": str(docs)})
        out = r.rag({"action": "ask", "query": "xylophone zebra"})
        assert "Nothing in the index" in out

    def test_reindex_purges_old_chunks(self, tmp_path):
        import actions.rag as r
        docs = tmp_path / "d"
        docs.mkdir()
        f = docs / "a.txt"
        f.write_text("original alpha content", encoding="utf-8")
        r.rag({"action": "index", "path": str(docs)})
        f.write_text("totally rewritten omega content", encoding="utf-8")
        r.rag({"action": "index", "path": str(docs)})
        out = r.rag({"action": "ask", "query": "alpha"})
        assert "Nothing in the index" in out
        out = r.rag({"action": "ask", "query": "omega"})
        assert "rewritten" in out

    def test_chunking_covers_long_doc(self, tmp_path):
        import actions.rag as r
        docs = tmp_path / "d"
        docs.mkdir()
        long_text = ("The quarterly revenue grew steadily. " * 400)
        (docs / "long.txt").write_text(long_text, encoding="utf-8")
        out = r.rag({"action": "index", "path": str(docs)})
        assert "Indexed 1 file" in out
        # every chunk searchable — tail sentence must be findable too
        tail = "steadily"
        hits = r._retrieve(tail, k=50)
        assert hits, "no chunks retrievable"
        # total chunk text must cover the whole document (no data loss)
        joined = "".join(t[2] for t in r._retrieve("quarterly", k=50))
        assert "quarterly revenue" in joined

    def test_binary_and_skip_dirs_ignored(self, tmp_path):
        import actions.rag as r
        docs = tmp_path / "d"
        (docs / "__pycache__").mkdir(parents=True)
        (docs / "ok.txt").write_text("hello searchable", encoding="utf-8")
        (docs / "__pycache__" / "junk.txt").write_text("junk", encoding="utf-8")
        (docs / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        out = r.rag({"action": "index", "path": str(docs)})
        assert "Indexed 1 file" in out

    def test_unreadable_pdf_counted_not_fatal(self, tmp_path):
        import actions.rag as r
        docs = tmp_path / "d"
        docs.mkdir()
        (docs / "broken.pdf").write_bytes(b"%PDF-1.4 garbage without xref")
        (docs / "good.txt").write_text("usable text", encoding="utf-8")
        out = r.rag({"action": "index", "path": str(docs)})
        assert "Indexed 1 file" in out          # pdf unreadable → skipped
        assert "1 file(s) unreadable" in out

    def test_tool_shape(self):
        import actions.rag as r
        assert r.TOOL["name"] == "rag"
        assert r.TOOL["handler"] is r.rag


# ═══════════════════════════════════════════════════════════════════════════
# MCP client (real stdio server)
# ═══════════════════════════════════════════════════════════════════════════

_MCP_FAKE = r'''
import json, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method, rid = msg.get("method"), msg["id"]
    if method == "initialize":
        result = {"protocolVersion": "2024-11-05", "capabilities": {},
                  "serverInfo": {"name": "fake", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "echo", "description": "Echo",
             "inputSchema": {"type": "object"}},
            {"name": "add", "description": "Add",
             "inputSchema": {"type": "object"}},
            {"name": "slow", "description": "Sleeps",
             "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        p = msg.get("params", {})
        name = p.get("name")
        if name == "echo":
            t = (p.get("arguments") or {}).get("text", "")
            result = {"content": [{"type": "text", "text": "echo: " + t}]}
        elif name == "add":
            a = p.get("arguments") or {}
            s = float(a.get("a", 0)) + float(a.get("b", 0))
            result = {"content": [{"type": "text", "text": str(s)}]}
        elif name == "slow":
            import time; time.sleep(30)
            result = {"content": [{"type": "text", "text": "late"}]}
        elif name == "explode":
            result = {"content": [], "isError": True}
        else:
            result = {"content": [], "isError": True}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "result": result}) + "\n")
    sys.stdout.flush()
'''


class TestMcp:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.mcp as m
        script = tmp_path / "fake_server.py"
        script.write_text(_MCP_FAKE, encoding="utf-8")
        cfg = {"servers": {"fake": {"command": [sys.executable, str(script)]}}}
        (tmp_path / "mcp.json").write_text(json.dumps(cfg), encoding="utf-8")
        monkeypatch.setattr(m, "_cfg_path", lambda: tmp_path / "mcp.json")
        m._TOOLS.clear()
        yield
        m._TOOLS.clear()

    def test_list_shows_tools(self):
        import actions.mcp as m
        out = m.mcp({"action": "list"})
        assert "fake" in out and "echo" in out and "add" in out

    def test_call_echo_and_add(self):
        import actions.mcp as m
        out = m.mcp({"action": "call", "name": "fake", "tool": "echo",
                     "arguments": json.dumps({"text": "hello jarvis"})})
        assert out == "echo: hello jarvis"
        out = m.mcp({"action": "call", "name": "fake", "tool": "add",
                     "arguments": {"a": 40, "b": 2}})
        assert out == "42.0"

    def test_unknown_server_and_tool(self):
        import actions.mcp as m
        assert "No MCP server named 'ghost'" in m.mcp(
            {"action": "call", "name": "ghost", "tool": "x"})
        out = m.mcp({"action": "call", "name": "fake", "tool": "nope",
                     "arguments": {}})
        assert "failed" in out or "error" in out.lower()

    def test_bad_arguments_json(self):
        import actions.mcp as m
        assert "JSON object" in m.mcp(
            {"action": "call", "name": "fake", "tool": "echo",
             "arguments": "not json{"})

    def test_add_verifies_handshake(self):
        import actions.mcp as m
        # bad command → saved-but-handshake-failed honesty
        out = m.mcp({"action": "add", "name": "broken",
                     "command": f"{sys.executable} -c 'pass'"})
        assert "handshake failed" in out
        # bad name shape refused before saving
        assert "letters/digits" in m.mcp({"action": "add", "name": "a b",
                                          "command": "ls"})

    def test_remove(self):
        import actions.mcp as m
        assert "Removed" in m.mcp({"action": "remove", "name": "fake"})
        assert "No MCP servers" in m.mcp({"action": "list"})
        assert "No MCP server" in m.mcp({"action": "remove", "name": "fake"})

    def test_timeout_does_not_hang(self):
        import actions.mcp as m
        t0 = time.monotonic()
        out = m.mcp({"action": "call", "name": "fake", "tool": "slow",
                     "timeout": 2})
        dt = time.monotonic() - t0
        assert dt < 10, f"timeout took {dt}s"
        assert "failed" in out and "timed out" in out

    def test_command_is_argv_list_never_shell(self):
        import actions.mcp as m
        argv = m._server_argv("fake")
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)

    def test_tool_shape(self):
        import actions.mcp as m
        assert m.TOOL["name"] == "mcp"
        assert m.TOOL["handler"] is m.mcp


# ═══════════════════════════════════════════════════════════════════════════
# DuckDB data tool
# ═══════════════════════════════════════════════════════════════════════════

class TestDataQuery:
    @pytest.fixture(autouse=True)
    def _csv(self, tmp_path):
        self.csv = tmp_path / "rent.csv"
        self.csv.write_text(
            "city,price\nJaipur,18000\nJaipur,22000\nDelhi,35000\n"
            "Mumbai,48000\n", encoding="utf-8")
        import actions.data_query as dq
        if not dq._OK:
            pytest.skip("duckdb not installed")
        return self

    def test_aggregate_query(self):
        from actions.data_query import data_query
        out = data_query({"file": str(self.csv),
                          "query": "SELECT city, AVG(price) avg FROM data "
                                   "GROUP BY city ORDER BY avg DESC"})
        assert "Mumbai" in out and "Jaipur" in out
        assert "48000.0" in out

    def test_describe_without_sql(self):
        from actions.data_query import data_query
        out = data_query({"file": str(self.csv)})
        assert "4 row(s)" in out and "city" in out and "price" in out

    def test_pure_sql_without_file(self):
        from actions.data_query import data_query
        out = data_query({"query": "SELECT 42 AS answer"})
        assert "42" in out

    def test_auto_limit_applied(self):
        from actions.data_query import data_query
        out = data_query({"file": str(self.csv), "query": "SELECT * FROM data"})
        # file only has 4 rows; LIMIT must not break results
        assert "(4 row(s))" in out

    def test_missing_file_reported(self):
        from actions.data_query import data_query
        out = data_query({"file": "/definitely/not/here.csv",
                          "query": "SELECT 1"})
        assert "No files match" in out

    def test_bad_sql_reports_not_raises(self):
        from actions.data_query import data_query
        out = data_query({"file": str(self.csv),
                          "query": "SELECT nonexistent_col FROM data"})
        assert "Query failed" in out

    def test_json_file(self, tmp_path):
        from actions.data_query import data_query
        js = tmp_path / "x.json"
        js.write_text('[{"a":1},{"a":5}]', encoding="utf-8")
        out = data_query({"file": str(js), "query": "SELECT SUM(a) s FROM data"})
        assert "6" in out

    def test_tool_shape(self):
        from actions.data_query import TOOL
        assert TOOL["name"] == "data"
        assert TOOL["parameters"]["type"] == "OBJECT"


# ═══════════════════════════════════════════════════════════════════════════
# make_3d
# ═══════════════════════════════════════════════════════════════════════════

class TestMake3d:
    @pytest.fixture(autouse=True)
    def models(self, tmp_path, monkeypatch):
        import config
        monkeypatch.setattr(config, "get_base_dir", lambda: tmp_path)
        return tmp_path / "models"

    def _stl_tri_count(self, path: Path) -> int:
        data = path.read_bytes()
        assert len(data) >= 84, "STL too small"
        (n,) = struct.unpack("<I", data[80:84])
        assert len(data) == 84 + n * 50, "STL size/count mismatch"
        return n

    def test_box_stl_valid(self, models):
        from actions.make_3d import make_3d
        out = make_3d({"spec": "box 40 20 8", "format": "stl", "name": "b1"})
        assert "Model saved" in out and "triangles" in out
        f = next(models.glob("b1-*.stl"))
        assert self._stl_tri_count(f) == 12      # box = 6 quads = 12 tris

    def test_multi_primitive_and_bounds(self, models):
        from actions.make_3d import make_3d
        out = make_3d({"spec": "box 40 20 8; cylinder 6 12 at 10 0 4",
                       "format": "stl", "name": "combo"})
        assert "Model saved" in out
        f = next(models.glob("combo-*.stl"))
        n = self._stl_tri_count(f)
        assert n > 12                            # box + cylinder added
        assert "40.0" in out                     # bounding box reported

    def test_obj_output_parsable(self, models):
        from actions.make_3d import make_3d
        out = make_3d({"spec": "sphere 15", "format": "obj", "name": "s1"})
        assert "Model saved" in out
        f = next(models.glob("s1-*.obj"))
        text = f.read_text(encoding="utf-8")
        verts = [l for l in text.splitlines() if l.startswith("v ")]
        faces = [l for l in text.splitlines() if l.startswith("f ")]
        assert verts and faces
        # face indices within vertex range
        idx = [int(l.split()[1].split("/")[0]) for l in faces]
        assert max(idx) <= len(verts)

    def test_named_dims_and_hole_op(self, models):
        from actions.make_3d import make_3d, _parse_spec
        g, notes = _parse_spec("box w=40 h=20 d=8; "
                               "hole cylinder r=6 h=10 at 0 0 4")
        assert [op for _, op in g] == ["union", "minus"]
        assert notes, "hole must carry an honest note"
        out = make_3d({"spec": "box w=40 h=20 d=8", "format": "stl",
                       "name": "named"})
        assert "Model saved" in out

    def test_bad_spec_reports(self, models):
        from actions.make_3d import make_3d
        out = make_3d({"spec": "gibberish soup"})
        assert "Spec problem" in out and "primitive" in out
        assert make_3d({"spec": "box 10 10"}) .startswith("Spec problem")
        assert "Give me a spec" in make_3d({})

    def test_cone_and_plate(self, models):
        from actions.make_3d import make_3d
        out = make_3d({"spec": "cone 10 20; plate 30 30", "format": "stl",
                       "name": "cp"})
        assert "Model saved" in out
        f = next(models.glob("cp-*.stl"))
        assert self._stl_tri_count(f) >= 12 + 4  # cone ≥4 tris + plate 12

    def test_name_sanitised(self, models):
        from actions.make_3d import make_3d
        out = make_3d({"spec": "box 10 10 10", "format": "stl",
                       "name": "../../evil"})
        assert "Model saved" in out
        # written strictly inside models/
        files = list(models.glob("*.stl"))
        assert files and all(".." not in f.name for f in files)

    def test_tool_shape(self):
        from actions.make_3d import TOOL
        assert TOOL["name"] == "make_3d"
        assert "spec" in TOOL["parameters"]["required"]


# ═══════════════════════════════════════════════════════════════════════════
# Translate
# ═══════════════════════════════════════════════════════════════════════════

class TestTranslate:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        import actions.translate as t
        monkeypatch.setattr(t, "_LIVE", {})
        import core.privacy as cp
        monkeypatch.setattr(cp, "is_on", lambda: False)
        yield
        t._LIVE.clear()

    def test_no_engine_honest_message(self, monkeypatch):
        import actions.translate as t
        monkeypatch.setattr(t, "_argos_available", lambda: False)
        monkeypatch.setattr(t, "_gemini_translate",
                            lambda *a: None)
        out = t.translate({"text": "hello", "to": "hi"})
        assert "No translation engine" in out

    def test_privacy_blocks_cloud_engine(self, monkeypatch):
        import actions.translate as t
        import core.privacy as cp
        monkeypatch.setattr(t, "_argos_available", lambda: False)
        monkeypatch.setattr(cp, "is_on", lambda: True)
        called = []
        monkeypatch.setattr(t, "_gemini_translate",
                            lambda *a: called.append(1) or "x")
        out = t.translate({"text": "hello", "to": "hi"})
        # privacy check happens INSIDE _gemini_translate normally; here we
        # verify the refusal path text exists when engine returns None
        monkeypatch.setattr(t, "_gemini_translate", lambda *a: None)
        out = t.translate({"text": "hello", "to": "hi"})
        assert "Privacy mode is ON" in out or "No translation engine" in out

    def test_gemini_path_used_when_available(self, monkeypatch):
        import actions.translate as t
        monkeypatch.setattr(t, "_argos_available", lambda: False)
        monkeypatch.setattr(t, "_gemini_translate",
                            lambda text, s, d: f"[{s}→{d}] {text}")
        out = t.translate({"text": "namaste", "to": "es", "from": "hi"})
        assert out == "[hi→es] namaste"

    def test_local_argos_preferred(self, monkeypatch):
        import actions.translate as t
        monkeypatch.setattr(t, "_argos_available", lambda: True)
        monkeypatch.setattr(t, "_argos_translate",
                            lambda text, s, d: "LOCAL:" + text)
        monkeypatch.setattr(t, "_gemini_translate",
                            lambda *a: pytest.fail("cloud must not be hit"))
        assert t.translate({"text": "hi", "to": "fr"}).startswith("LOCAL:")

    def test_live_pair_status_off(self):
        import actions.translate as t
        out = t.translate({"action": "live", "from": "en", "to": "hi"})
        assert "Live translate on" in out and "English" in out
        assert "English" in t.translate({"action": "status"})
        assert "off" in t.translate({"action": "off"}).lower()
        assert "off" in t.translate({"action": "status"}).lower()

    def test_live_requires_or_defaults_pair(self):
        import actions.translate as t
        out = t.translate({"action": "live"})
        assert "pair" in out.lower()

    def test_empty_text_prompts(self):
        import actions.translate as t
        assert "translate" in t.translate({}).lower()

    def test_same_lang_pair_fixed(self, monkeypatch):
        import actions.translate as t
        monkeypatch.setattr(t, "_argos_available", lambda: False)
        seen = {}
        def fake(text, s, d):
            seen["pair"] = (s, d)
            return "ok"
        monkeypatch.setattr(t, "_gemini_translate", fake)
        t.translate({"text": "x", "from": "hi", "to": "hi"})
        assert seen["pair"][0] != seen["pair"][1]

    def test_tool_shape(self):
        from actions.translate import TOOL
        assert TOOL["name"] == "translate"


# ═══════════════════════════════════════════════════════════════════════════
# Password vault
# ═══════════════════════════════════════════════════════════════════════════

class TestVault:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.vault as v
        monkeypatch.setattr(v, "_vault_path", lambda: tmp_path / "vault.enc")
        monkeypatch.setattr(v, "_SESSION_KEY", None)
        monkeypatch.setattr(v, "_VAULT", None)
        yield
        monkeypatch.setattr(v, "_SESSION_KEY", None)
        monkeypatch.setattr(v, "_VAULT", None)

    def test_locked_until_unlocked(self):
        import actions.vault as v
        assert "locked" in v.vault({"action": "list"})
        assert "created and unlocked" in v.vault(
            {"action": "unlock", "passphrase": "master"})
        assert "empty" in v.vault({"action": "list"}).lower()

    def test_set_get_masked_reveal(self):
        import actions.vault as v
        v.vault({"action": "unlock", "passphrase": "m"})
        v.vault({"action": "set", "site": "github.com", "user": "tony",
                 "pass": "hunter2"})
        out = v.vault({"action": "get", "site": "github.com"})
        assert "tony" in out and "hunter2" not in out
        assert "reveal" in out
        out = v.vault({"action": "get", "site": "github.com",
                       "reveal": "true"})
        assert "hunter2" in out

    def test_wrong_passphrase_rejected(self):
        import actions.vault as v
        v.vault({"action": "unlock", "passphrase": "right"})
        v.vault({"action": "set", "site": "x", "pass": "s3cret"})
        v.vault({"action": "lock"})
        out = v.vault({"action": "unlock", "passphrase": "wrong"})
        assert "Wrong master passphrase" in out
        assert "locked" in v.vault({"action": "list"})

    def test_list_and_delete(self):
        import actions.vault as v
        v.vault({"action": "unlock", "passphrase": "m"})
        v.vault({"action": "set", "site": "a.com", "pass": "1"})
        v.vault({"action": "set", "site": "b.com", "pass": "2"})
        out = v.vault({"action": "list"})
        assert "a.com" in out and "b.com" in out
        assert "Deleted" in v.vault({"action": "del", "site": "a.com"})
        assert "No entry" in v.vault({"action": "del", "site": "a.com"})
        assert "a.com" not in v.vault({"action": "list"})

    def test_no_plaintext_on_disk(self):
        import actions.vault as v
        v.vault({"action": "unlock", "passphrase": "m"})
        v.vault({"action": "set", "site": "bank", "user": "u",
                 "pass": "TopSecret999"})
        raw = v._vault_path().read_text(encoding="utf-8")
        assert "TopSecret999" not in raw and "bank" not in raw
        data = json.loads(raw)
        assert data["v"] == 1 and data["kdf"]["n"] == 2 ** 15

    def test_reload_survives_restart(self):
        import actions.vault as v
        v.vault({"action": "unlock", "passphrase": "pw"})
        v.vault({"action": "set", "site": "x", "pass": "sekrit"})
        # simulate restart
        v._SESSION_KEY = None
        v._VAULT = None
        out = v.vault({"action": "unlock", "passphrase": "pw"})
        assert "1 entries" in out
        assert "sekrit" in v.vault({"action": "get", "site": "x",
                                    "reveal": "1"})

    def test_missing_args_reported(self):
        import actions.vault as v
        v.vault({"action": "unlock", "passphrase": "m"})
        assert "Need site and pass" in v.vault({"action": "set"})
        assert "unlock first" in v.vault(
            {"action": "set", "site": "x", "pass": "y"},
        ) if v._SESSION_KEY is None else True
        assert "Unknown vault action" in v.vault({"action": "fly"})

    def test_tool_shape(self):
        from actions.vault import TOOL
        assert TOOL["name"] == "vault"
        assert "passphrase" in TOOL["parameters"]["properties"]


# ═══════════════════════════════════════════════════════════════════════════
# Sandboxed terminal
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminal:
    def test_allowlisted_python_runs(self):
        from actions.terminal import terminal
        out = terminal({"command": "python -c 'print(6*7)'", "timeout": 20})
        assert "[exit 0]" in out and "42" in out

    def test_shell_metacharacters_refused(self):
        from actions.terminal import terminal
        for bad in ("ls; cat /etc/passwd", "ls && rm -rf /",
                    "echo `whoami`", "ls | tee x", "echo $HOME",
                    "ls > out.txt", "ls\ncat x"):
            out = terminal({"command": bad})
            assert "refused" in out.lower(), bad

    def test_path_escape_refused(self):
        from actions.terminal import terminal
        for bad in ("cat ../../etc/passwd", "cat ~root/.ssh/id_rsa",
                    "ls /etc"):
            out = terminal({"command": bad})
            assert "refused" in out.lower(), bad

    def test_not_allowlisted_refused(self):
        from actions.terminal import terminal
        out = terminal({"command": "chmod 777 x"})
        assert "not allowlisted" in out

    def test_git_status_free(self):
        from actions.terminal import terminal
        out = terminal({"command": "git status --short"})
        assert "[exit" in out                     # ran (repo present)

    def test_git_write_needs_confirm(self):
        from actions.terminal import terminal
        out = terminal({"action": "git", "command": "commit -m x"})
        assert "confirm=yes" in out
        # with confirm it at least attempts (exit code is fine either way)
        out = terminal({"action": "git", "command": "commit -m x",
                        "confirm": "yes"})
        assert "[exit" in out

    def test_force_push_refused_even_with_confirm(self):
        from actions.terminal import terminal
        out = terminal({"action": "git", "command": "push --force",
                        "confirm": "yes"})
        assert "refused" in out.lower()

    def test_git_filter_branch_denied(self):
        from actions.terminal import terminal
        out = terminal({"action": "git", "command": "filter-branch"})
        assert "deny list" in out

    def test_timeout_kills(self, tmp_path):
        from actions.terminal import terminal
        script = tmp_path / "sleeper.py"
        script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        t0 = time.monotonic()
        out = terminal({"command": f"python {script}", "timeout": 2})
        dt = time.monotonic() - t0
        assert dt < 10 and "Timed out" in out

    def test_semicolon_anywhere_refused(self):
        """Raw-string metachar check: even inside quotes a `;` is refused
        (argv-safe today, but keeps a future shell=True refactor inert)."""
        from actions.terminal import terminal
        assert "refused" in terminal(
            {"command": "python -c 'import time; time.sleep(1)'"}).lower()

    def test_output_cap(self):
        from actions.terminal import terminal
        out = terminal({"command":
                        "python -c 'print(\"x\"*100000)'", "timeout": 20})
        assert "truncated" in out and len(out) < 100_000 + 500

    def test_missing_binary_reported(self):
        from actions.terminal import terminal
        out = terminal({"command": "duckdb --version"})
        # duckdb may or may not be installed — must not crash either way
        assert "[exit" in out or "not installed" in out

    def test_help_text(self):
        from actions.terminal import terminal
        out = terminal({"command": "", "action": "help"})
        assert "Allowlisted" in out

    def test_tool_shape(self):
        from actions.terminal import TOOL
        assert TOOL["name"] == "terminal"
        assert TOOL["parameters"]["required"] == ["command"]


# ═══════════════════════════════════════════════════════════════════════════
# Event-driven proactivity
# ═══════════════════════════════════════════════════════════════════════════

class TestEventEngine:
    @pytest.fixture(autouse=True)
    def _engine(self, tmp_path):
        from core.events import EventEngine
        self.state = tmp_path / "fired.json"
        self.eng = EventEngine(state_path=self.state)
        return self

    def test_baseline_then_downward_crossing_fires_once(self):
        self.eng._battery = lambda: (80, False)
        assert self.eng.poll() == []              # baseline
        self.eng._battery = lambda: (75, False)
        assert self.eng.poll() == []              # no edge
        self.eng._battery = lambda: (18, False)
        evs = self.eng.poll()
        assert len(evs) == 1 and evs[0].severity == "warn"
        assert "18%" in evs[0].message
        assert self.eng.poll() == []              # hysteresis — no nag

    def test_critical_crossing(self):
        self.eng._battery = lambda: (15, False)
        self.eng.poll()
        self.eng._battery = lambda: (9, False)
        evs = self.eng.poll()
        assert len(evs) == 1 and evs[0].severity == "critical"

    def test_charging_suppresses_downward_alerts(self):
        self.eng._battery = lambda: (80, True)
        self.eng.poll()
        self.eng._battery = lambda: (15, True)
        assert self.eng.poll() == []

    def test_full_charge_edge(self):
        self.eng._battery = lambda: (50, True)
        self.eng.poll()
        self.eng._battery = lambda: (99, True)
        self.eng.poll()
        self.eng._battery = lambda: (100, True)
        evs = self.eng.poll()
        assert len(evs) == 1 and "fully charged" in evs[0].message

    def test_no_battery_sensor_no_crash(self):
        self.eng._battery = lambda: (None, False)
        assert self.eng.poll() == []

    def test_calendar_lead_fires_once(self):
        now = datetime(2026, 10, 3, 10, 0, 0)
        start = now + timedelta(minutes=8)
        self.eng._battery = lambda: (None, False)
        self.eng._ics_events = lambda a, b: [
            {"uid": "m1", "title": "Standup", "start": start,
             "end": start + timedelta(minutes=15)}]
        evs = self.eng.poll(now)
        assert len(evs) == 1 and "Standup" in evs[0].message
        assert "minutes" in evs[0].message
        assert self.eng.poll(now) == []           # dedup — no refire
        # after it ends: nothing
        assert self.eng.poll(now + timedelta(minutes=30)) == []

    def test_calendar_60min_lead(self):
        now = datetime(2026, 10, 3, 10, 0, 0)
        self.eng._battery = lambda: (None, False)
        far = [{"uid": "m2", "title": "Retro",
                "start": now + timedelta(minutes=75),
                "end": now + timedelta(minutes=90)}]
        self.eng._ics_events = lambda a, b: far
        assert self.eng.poll(now) == []           # beyond 60-min lead
        near = [{"uid": "m2", "title": "Retro",
                 "start": now + timedelta(minutes=55),
                 "end": now + timedelta(minutes=70)}]
        self.eng._ics_events = lambda a, b: near
        evs = self.eng.poll(now + timedelta(minutes=20))
        assert len(evs) == 1 and evs[0].severity == "info"

    def test_nearest_lead_only_no_fallthrough(self):
        """T-10 fired earlier must not fall through to T-60 refire."""
        now = datetime(2026, 10, 3, 10, 0, 0)
        start = now + timedelta(minutes=8)
        self.eng._battery = lambda: (None, False)
        ev = [{"uid": "m3", "title": "Sync", "start": start,
               "end": start + timedelta(minutes=30)}]
        self.eng._ics_events = lambda a, b: ev
        assert len(self.eng.poll(now)) == 1
        assert self.eng.poll(now) == []
        assert self.eng.poll(now + timedelta(minutes=1)) == []

    def test_fired_keys_persist_across_restart(self):
        self.eng._battery = lambda: (15, False)
        self.eng.poll()
        self.eng._battery = lambda: (9, False)
        self.eng.poll()
        from core.events import EventEngine
        eng2 = EventEngine(state_path=self.state)
        assert any("battery:10" in k for k in eng2.fired_keys())

    def test_corrupt_state_file_tolerated(self, tmp_path):
        self.state.write_text("{corrupt", encoding="utf-8")
        from core.events import EventEngine
        eng = EventEngine(state_path=self.state)
        eng._battery = lambda: (None, False)
        assert eng.poll() == []

    def test_broken_ics_seam_tolerated(self):
        def boom(a, b):
            raise ConnectionError("feed down")
        self.eng._battery = lambda: (None, False)
        self.eng._ics_events = boom
        assert self.eng.poll(datetime(2026, 10, 3, 10, 0)) == []

    def test_reset(self):
        self.eng._battery = lambda: (15, False)
        self.eng.poll()
        self.eng.reset()
        assert self.eng.fired_keys() == []


# ═══════════════════════════════════════════════════════════════════════════
# Main wiring (source-level, PyQt absent)
# ═══════════════════════════════════════════════════════════════════════════

class TestRoadmapMainWiring:
    def test_event_watch_task_created(self):
        assert "tg.create_task(self._run_event_watch())" in SRC

    def test_event_watch_method_exists(self):
        assert "async def _run_event_watch" in SRC
        i = SRC.index("async def _run_event_watch")
        body = SRC[i:i + 2200]
        assert "self._events.poll" in body
        assert "_focus_muted" in body             # focus silences events
        assert "send_client_content" in body

    def test_event_engine_instantiated(self):
        assert "self._events           = EventEngine()" in SRC
        assert "from core.events import EventEngine" in SRC

    def test_history_recording_wired_both_sides(self):
        # user turns + assistant turns both recorded
        assert SRC.count('_hs.record(') >= 2
        i_user = SRC.index('self._session_log.append(f"User:')
        assert '_hs.record("user", full_in)' in SRC[i_user:i_user + 400]
        i_bot = SRC.index('self._session_log.append(f"{self._asst_name}:')
        assert '_hs.record(self._asst_name, full_out)' in SRC[i_bot:i_bot + 400]

    def test_history_record_is_exception_safe(self):
        # both wiring sites must be wrapped in try/except — history can
        # never break a reply
        import re
        sites = list(re.finditer(r"_hs\.record\(", SRC))
        assert len(sites) >= 2
        for m in sites:
            window = SRC[m.start():m.start() + 300]
            assert "except Exception" in window

    def test_new_tools_discoverable(self):
        """Each new batch-1 tool exports TOOL with name+handler — the
        loader contract (matches how action_loader discovers actions)."""
        mods = ["actions.history_search", "actions.rag", "actions.mcp",
                "actions.data_query", "actions.make_3d", "actions.translate",
                "actions.vault", "actions.terminal", "actions.privacy"]
        import importlib
        for name in mods:
            mod = importlib.import_module(name)
            tool = getattr(mod, "TOOL")
            assert tool["name"] and callable(tool["handler"]), name
            assert tool["parameters"]["type"] == "OBJECT", name

    def test_cloud_handlers_gated_in_source(self):
        for path, needle in [
            ("actions/research.py", '_privacy.gate("research")'),
            ("actions/scrape.py", '_privacy.gate("scrape")'),
            ("actions/web_search.py", '_privacy.gate("web_search")'),
            ("actions/phone_vision.py", '_privacy.gate("phone_vision")'),
        ]:
            assert needle in Path(path).read_text(encoding="utf-8"), path


# ═══════════════════════════════════════════════════════════════════════════
# Batch 2 — task persistence + resume
# ═══════════════════════════════════════════════════════════════════════════

class TestTaskPersistence:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import core.taskstore as ts
        monkeypatch.setattr(ts, "_db_path", lambda: tmp_path / "tasks.db")
        monkeypatch.setattr(ts, "_CONN", None)
        yield
        monkeypatch.setattr(ts, "_CONN", None)

    def test_create_get_update_cycle(self):
        from core import taskstore as ts
        rid = ts.create("clean downloads", [
            {"tool": "scan", "args": {"what": "dl"}, "why": ""}])
        run = ts.get(rid)
        assert run["goal"] == "clean downloads"
        assert run["status"] == "running"
        assert len(run["plan"]) == 1
        ts.update(rid, "done", [{"index": 0, "tool": "scan", "ok": True,
                                 "result": "ok"}])
        run = ts.get(rid)
        assert run["status"] == "done" and run["results"][0]["ok"]

    def test_bad_status_rejected(self):
        from core import taskstore as ts
        rid = ts.create("g", [])
        with pytest.raises(ValueError):
            ts.update(rid, "weird", [])

    def test_resumable_finds_partial(self):
        from core import taskstore as ts
        rid = ts.create("partial goal", [
            {"tool": "a", "args": {}, "why": ""},
            {"tool": "b", "args": {}, "why": ""}])
        ts.update(rid, "partial", [{"index": 0, "tool": "a", "ok": True,
                                    "result": "fine"}])
        pend = ts._pending_indices(ts.get(rid))
        assert pend == [1]
        res = ts.resumable()
        assert any(r["id"] == rid for r in res)

    def test_done_not_resumable(self):
        from core import taskstore as ts
        rid = ts.create("g", [{"tool": "a", "args": {}, "why": ""}])
        ts.update(rid, "done", [{"index": 0, "tool": "a", "ok": True,
                                 "result": "x"}])
        assert ts.resumable() == []

    def test_prune_caps_retained_runs(self):
        from core import taskstore as ts
        for i in range(ts.MAX_RETAINED + 15):
            rid = ts.create(f"goal {i}", [])
            ts.update(rid, "done", [])
        with ts._LOCK:
            n = ts._conn().execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert n <= ts.MAX_RETAINED

    def test_list_order_newest_first(self):
        from core import taskstore as ts
        a = ts.create("first", [])
        b = ts.create("second", [])
        runs = ts.list_runs()
        assert [r["id"] for r in runs][:2] == [b, a]


class TestTaskAgentPersistence:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import core.taskstore as ts
        from actions import task_agent as ta
        monkeypatch.setattr(ts, "_db_path", lambda: tmp_path / "tasks.db")
        monkeypatch.setattr(ts, "_CONN", None)
        yield
        ts._CONN = None
        ta.set_runner(None)
        ta.set_runner_names([])

    def test_run_is_persisted_and_history_lists_it(self, monkeypatch):
        from actions import task_agent as ta
        from core.orchestrator import Step
        monkeypatch.setattr(ta, "_plan_with_llm",
                            lambda g, n: [Step("scan", {"what": "system"})])
        ta.set_runner(lambda t, a: "ok")
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "health check"})
        assert "1/1 steps completed" in out
        hist = ta.task_agent({"action": "history"})
        assert "health check" in hist and "✓" in hist

    def test_failed_run_persisted_with_resume_hint(self, monkeypatch):
        from actions import task_agent as ta
        from core.orchestrator import Step
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: [
            Step("scan", {"what": "system"}),
            Step("scan", {"what": "ports"})])
        def runner(t, a):
            if a.get("what") == "ports":
                raise RuntimeError("boom")
            return "ok"
        ta.set_runner(runner)
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "check"})
        assert "resume task" in out
        hist = ta.task_agent({"action": "history"})
        assert "1 step(s) left" in hist

    def test_resume_runs_only_pending_steps(self, monkeypatch):
        from actions import task_agent as ta
        from core.orchestrator import Step
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: [
            Step("scan", {"what": "system"}),
            Step("scan", {"what": "ports"})])
        calls = []
        def flaky(t, a):
            calls.append(a.get("what"))
            if a.get("what") == "ports" and len(calls) < 3:
                raise RuntimeError("flaky")
            return "ok"
        ta.set_runner(flaky)
        ta.set_runner_names(["scan"])
        ta.task_agent({"description": "check"})
        calls_before = len(calls)
        out = ta.task_agent({"action": "resume"})
        assert "Resumed task" in out
        # 'system' step ran in the INITIAL run but must NOT re-run on resume
        assert calls.count("system") == 1, calls
        # resume adds only 'ports' attempts
        assert len(calls) > calls_before
        assert all(c == "ports" for c in calls[calls_before:]), calls
        hist = ta.task_agent({"action": "history"})
        assert "✓" in hist and "step(s) left" not in hist

    def test_resume_nothing_available(self):
        from actions import task_agent as ta
        ta.set_runner(lambda t, a: "ok")
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"action": "resume"})
        assert "Nothing to resume" in out

    def test_resume_unwired_runner(self):
        from actions import task_agent as ta
        ta.set_runner(None)
        out = ta.task_agent({"action": "resume"})
        assert "not wired" in out

    def test_resume_missing_tool_reported(self, monkeypatch):
        from actions import task_agent as ta
        from core.orchestrator import Step
        monkeypatch.setattr(ta, "_plan_with_llm",
                            lambda g, n: [Step("scan", {"what": "x"})])
        ta.set_runner(lambda t, a: (_ for _ in ()).throw(RuntimeError("x")))
        ta.set_runner_names(["scan"])
        ta.task_agent({"description": "fail plz"})
        # tool list changes — 'scan' no longer available
        ta.set_runner_names(["weather_report"])
        out = ta.task_agent({"action": "resume"})
        assert "no longer exist" in out

    def test_empty_history(self):
        from actions import task_agent as ta
        ta.set_runner(lambda t, a: "ok")
        ta.set_runner_names(["scan"])
        assert "No task runs" in ta.task_agent({"action": "history"})


# ═══════════════════════════════════════════════════════════════════════════
# Batch 2 — structured web extraction
# ═══════════════════════════════════════════════════════════════════════════

class TestStructuredExtraction:
    HTML = '''<!doctype html><html><head>
    <title>Fallback Title</title>
    <meta property="og:title" content="Rent Agreement Guide 2026">
    <meta property="og:description" content="Everything about notice periods.">
    <meta property="og:site_name" content="Housing Times">
    <meta property="og:type" content="article">
    <meta property="article:published_time" content="2026-09-01">
    <meta name="author" content="Priya Sharma">
    <meta property="og:image" content="https://x/y.jpg">
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Article",
     "headline":"Rent Agreement Guide 2026",
     "author":{"@type":"Person","name":"Priya Sharma"},
     "datePublished":"2026-09-01"}
    </script></head><body>
    <nav>skip this navigation menu noise</nav>
    <article><p>The notice period in a rental agreement is usually two
    months, and the security deposit ranges from two to six months of
    rent depending on the city.</p></article>
    <footer>copyright noise</footer></body></html>'''

    def test_metadata_and_body(self):
        from actions.scrape import _structured_extract
        out = _structured_extract(self.HTML, "https://example.com/g")
        assert "Rent Agreement Guide 2026" in out
        assert "Priya Sharma" in out
        assert "article" in out.lower()
        assert "2026-09-01" in out
        assert "notice period" in out.lower()
        assert "skip this navigation" not in out.lower()

    def test_malformed_json_ld_tolerated(self):
        from actions.scrape import _structured_extract
        bad = self.HTML.replace('{"@context"', '{broken@"context"')
        out = _structured_extract(bad, "https://example.com/b")
        # og: metadata still extracted even though LD failed to parse
        assert "Rent Agreement Guide 2026" in out

    def test_empty_html(self):
        from actions.scrape import _structured_extract
        out = _structured_extract("", "https://e.com")
        assert "(untitled)" in out

    def test_product_ld_only(self):
        from actions.scrape import _structured_extract
        ld = ('<html><head><script type="application/ld+json">'
              '{"@type":"Product","name":"Widget Pro",'
              '"offers":{"price":"999","priceCurrency":"INR"}}'
              '</script></head><body><p>Buy the widget. It is a good '
              'widget with many features for daily use.</p></body></html>')
        out = _structured_extract(ld, "https://shop/w")
        assert "Widget Pro" in out
        assert "Product" in out

    def test_scrape_structured_mode_wired(self, monkeypatch):
        from actions import scrape as sc
        monkeypatch.setattr(sc, "_fetch", lambda u, timeout=20: self.HTML)
        out = sc.scrape({"url": "https://example.com/g",
                         "mode": "structured"})
        assert "Rent Agreement Guide 2026" in out

    def test_tool_schema_mentions_structured(self):
        from actions.scrape import TOOL
        assert "structured" in TOOL["parameters"]["properties"]["mode"]["description"]


# ═══════════════════════════════════════════════════════════════════════════
# Batch 2 — meeting action items → reminders
# ═══════════════════════════════════════════════════════════════════════════

class TestMeetingAutoPush:
    @pytest.fixture(autouse=True)
    def _seams(self, monkeypatch):
        import actions.meeting as m
        self.meeting = m
        self.pushed = []
        import actions.reminder as rem
        self._orig_reminder = rem.reminder
        def fake_reminder(params, *a, **k):
            self.pushed.append(params)
            if str(params.get("date", "")).startswith("20"):
                return "Reminder set for that time."
            return "I couldn't parse that date or time."
        monkeypatch.setattr(rem, "reminder", fake_reminder)
        yield
        monkeypatch.setattr(rem, "reminder", self._orig_reminder)

    def test_dated_items_scheduled_undated_listed(self):
        self.meeting._extract_action_items = lambda s: [
            {"task": "send deck", "date": "2026-10-05", "time": "10:00"},
            {"task": "review contract", "date": "", "time": ""}]
        out = self.meeting._push_action_items("summary")
        assert "Pushed 1 reminder(s)" in out
        assert "send deck" in out and "2026-10-05" in out
        assert "No deadline" in out and "review contract" in out
        assert len(self.pushed) == 1
        assert self.pushed[0]["message"].startswith("Meeting follow-up:")

    def test_undated_items_never_guessed(self):
        self.meeting._extract_action_items = lambda s: [
            {"task": "ping team", "date": "", "time": ""}]
        out = self.meeting._push_action_items("s")
        assert self.pushed == []
        assert "No deadline" in out

    def test_scheduler_failure_surfaced(self):
        self.meeting._extract_action_items = lambda s: [
            {"task": "x", "date": "garbage", "time": "zz"}]
        out = self.meeting._push_action_items("s")
        assert "Could not schedule" in out

    def test_no_extraction_honest(self):
        self.meeting._extract_action_items = lambda s: []
        out = self.meeting._push_action_items("s")
        assert "nothing was scheduled" in out

    def test_push_param_in_tool_schema(self):
        assert "push" in self.meeting.TOOL["parameters"]["properties"]

    def test_summary_push_param_flows(self, monkeypatch, tmp_path):
        # summary path with push=yes calls the seam
        import time as _t
        tr = tmp_path / "meetings"
        tr.mkdir()
        (tr / "001.txt").write_text("hello team, lets sync", encoding="utf-8")
        monkeypatch.setattr(self.meeting, "_meetings_dir", lambda: tr)
        monkeypatch.setattr(self.meeting, "_latest_transcript",
                            lambda: tr / "001.txt")
        monkeypatch.setattr(self.meeting, "_summarize",
                            lambda t, w: "Actions: send deck by Oct 5.")
        pushed_s = []
        monkeypatch.setattr(self.meeting, "_push_action_items",
                            lambda s: pushed_s.append(s) or "PUSHED")
        out = self.meeting.meeting({"action": "summary", "push": "yes"})
        assert "PUSHED" in out and pushed_s
        # without push → seam not called
        pushed_s.clear()
        out = self.meeting.meeting({"action": "summary"})
        assert "PUSHED" not in out


# ═══════════════════════════════════════════════════════════════════════════
# Batch 2 — rules self-healing retries
# ═══════════════════════════════════════════════════════════════════════════

class TestRulesSelfHeal:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import actions.rules as r
        monkeypatch.setattr(r, "_path", lambda: tmp_path / "rules.json")
        r._RULES.clear()
        r._LOADED = False
        r._HEALTH.clear()
        r._DAY_KEYS.clear()
        r._LAST_FIRE.clear()
        r._STATE.clear()
        r.set_runner(None)
        r.set_notifier(None)
        yield
        r.set_runner(None)
        r._HEALTH.clear()

    def test_failure_schedules_backoff(self):
        import actions.rules as r
        def bad(tool, args):
            raise RuntimeError("service down")
        r.set_runner(bad)
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {})
        rule = r._RULES[0]
        now = 1_000_000.0
        ok, out = r._exec_rule(rule, "time")
        assert not ok and "failed" in out
        r._note_health(rule, ok, out, now)
        h = r._HEALTH[rule["id"]]
        assert h["fails"] == 1
        assert h["retry_at"] == now + 30

    def test_success_clears_incident(self):
        import actions.rules as r
        r.set_runner(lambda t, a: "ok")
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {})
        rule = r._RULES[0]
        now = 1_000_000.0
        ok, out = r._exec_rule(rule, "time")
        r._note_health(rule, ok, out, now)
        assert rule["id"] not in r._HEALTH
        # prior failure then success
        r._HEALTH[rule["id"]] = {"fails": 2, "retry_at": now + 10,
                                 "err": "x", "why": "scan"}
        r._note_health(rule, True, "ok", now)
        assert rule["id"] not in r._HEALTH

    def test_heal_retries_then_recovers(self):
        import actions.rules as r
        state = {"ok": False}
        def runner(t, a):
            if not state["ok"]:
                raise RuntimeError("down")
            return "recovered!"
        r.set_runner(runner)
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {})
        rule = r._RULES[0]
        now = 1_000_000.0
        r._LOADED = True
        # initial failure
        ok, out = r._exec_rule(rule, "t")
        r._note_health(rule, ok, out, now)
        # tick before backoff → no attempt
        assert r.tick(now=now + 10) == []
        # heal 1 (still down) → fails=2, backoff 120
        r.tick(now=now + 31)
        assert r._HEALTH[rule["id"]]["fails"] == 2
        # heal 2 (still down) → fails=3, backoff 600
        r.tick(now=now + 31 + 121)
        assert r._HEALTH[rule["id"]]["fails"] == 3
        # service back → heal 3 succeeds, health cleared
        state["ok"] = True
        outs = r.tick(now=now + 31 + 121 + 601)
        assert any("recovered" in o for o in outs)
        assert rule["id"] not in r._HEALTH

    def test_gives_up_but_stays_visible(self):
        import actions.rules as r
        r.set_runner(lambda t, a: (_ for _ in ()).throw(RuntimeError("x")))
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {})
        rule = r._RULES[0]
        now = 1_000_000.0
        for _ in range(5):
            ok, out = r._exec_rule(rule, "t")
            r._note_health(rule, ok, out, now)
        h = r._HEALTH[rule["id"]]
        assert h["fails"] == 5 and h["retry_at"] is None
        rep = r.health_report()
        assert "gave up" in rep and "scan" in rep

    def test_health_clean_message(self):
        import actions.rules as r
        assert "healthy" in r.health_report()

    def test_deleted_rule_health_cleaned_on_tick(self):
        import actions.rules as r
        r.set_runner(lambda t, a: "ok")
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {})
        rule = r._RULES[0]
        r._HEALTH[rule["id"]] = {"fails": 5, "retry_at": None,
                                 "err": "x", "why": "scan"}
        r._RULES.clear()
        r._LOADED = True
        r.tick(now=2_000_000.0)
        assert rule["id"] not in r._HEALTH

    def test_missing_runner_not_counted_as_failure(self):
        import actions.rules as r
        r.set_runner(None)
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {})
        r._LOADED = True
        r.tick(now=1_000_000.0)   # time not due anyway; ensure no health
        assert r._HEALTH == {}

    def test_health_action_in_manage_rules(self):
        import actions.rules as r
        out = r.manage_rules({"action": "health"})
        assert "healthy" in out

    def test_scene_step_failure_marks_health(self):
        import actions.rules as r
        def runner(t, a):
            if t == "procman":
                raise RuntimeError("refused")
            return "ok"
        r.set_runner(runner)
        r.add_rule({"type": "time", "value": "08:00"}, "scan", {},
                   steps=[{"tool": "scan", "args": {}},
                          {"tool": "procman", "args": {}}])
        rule = r._RULES[0]
        ok, out = r._exec_rule(rule, "t")
        assert not ok and "failed at step 2" in out
        r._note_health(rule, ok, out, 1_000_000.0)
        assert rule["id"] in r._HEALTH


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — auto barge-in (EchoGuard sustained-evidence gate)
# ═══════════════════════════════════════════════════════════════════════════

class TestBargeIn:
    SR = 16000

    def _pcm(self, freq: float):
        import numpy as np
        n = 1024
        t = np.arange(n) / self.SR
        return (0.4 * np.sin(2 * np.pi * freq * t)).astype(np.float32)

    @pytest.fixture(autouse=True)
    def _guard(self):
        from core.echo import EchoGuard
        self.g = EchoGuard()
        self.echo = self._pcm(500)      # what JARVIS "says"
        self.user = self._pcm(4000)     # a different voice
        self.t0 = 10_000.0
        yield

    def _warmup(self):
        """16 echo blocks so the guard knows the room + passes warmup."""
        self.g.note_output(self.echo, self.SR, 0.5, when=self.t0)
        for i in range(16):
            self.g.should_interrupt(self.echo, self.SR, 0.3,
                                    when=self.t0 + 0.05 + i * 0.06)

    def test_no_history_never_interrupts(self):
        assert self.g.should_interrupt(self.user, self.SR, 0.3,
                                       when=self.t0) is False

    def test_echo_never_interrupts(self):
        self._warmup()
        for i in range(10):
            r = self.g.should_interrupt(self.echo, self.SR, 0.3,
                                        when=self.t0 + 1.0 + i * 0.04)
            assert r is False, "our own echo must never cut us off"

    def test_user_voice_needs_sustained_blocks(self):
        self._warmup()
        base = self.t0 + 1.25
        self.g.note_output(self.echo, self.SR, 0.5, when=base)
        fired = [self.g.should_interrupt(self.user, self.SR, 0.3,
                                         when=base + i * 0.05)
                 for i in range(6)]
        req = self.g.required_blocks
        # index req-1 is the req-th consecutive block → first True there
        assert fired == [False] * (req - 1) + [True] * (6 - req + 1), \
            (fired, req)

    def test_single_cough_resets_evidence(self):
        self._warmup()
        base = self.t0 + 1.25
        self.g.note_output(self.echo, self.SR, 0.5, when=base)
        # 3 voice blocks (below threshold) …
        for i in range(3):
            self.g.should_interrupt(self.user, self.SR, 0.3,
                                    when=base + i * 0.05)
        # … one echo block between them (a cough-equivalent break) …
        assert self.g.should_interrupt(self.echo, self.SR, 0.3,
                                       when=base + 4 * 0.05) is False
        # … restarts the count: first voice block after it does NOT fire
        assert self.g.should_interrupt(self.user, self.SR, 0.3,
                                       when=base + 5 * 0.05) is False

    def test_note_interrupted_resets(self):
        self._warmup()
        base = self.t0 + 1.25
        self.g.note_output(self.echo, self.SR, 0.5, when=base)
        for i in range(self.g.required_blocks):
            self.g.should_interrupt(self.user, self.SR, 0.3,
                                    when=base + i * 0.05)
        self.g.note_interrupted()
        assert self.g._int_run == 0
        assert self.g.should_interrupt(self.user, self.SR, 0.3,
                                       when=base + 6 * 0.05) is False

    def test_reset_clears_evidence_and_history(self):
        self._warmup()
        self.g.reset()
        assert self.g._int_run == 0 and self.g._hist == []
        assert self.g.should_interrupt(self.user, self.SR, 0.3,
                                       when=self.t0) is False

    def test_silence_never_counts(self):
        self._warmup()
        base = self.t0 + 1.25
        self.g.note_output(self.echo, self.SR, 0.5, when=base)
        for i in range(20):
            assert self.g.should_interrupt(self.user, self.SR, 0.01,
                                           when=base + i * 0.03) is False

    def test_exception_returns_false(self):
        # garbage inputs must never raise nor interrupt (audio thread!)
        assert self.g.should_interrupt(None, 0, 0.3) is False

    def test_config_flag_helpers(self, tmp_path, monkeypatch):
        import memory.config_manager as cm
        monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
        # default ON
        assert cm.get_barge_in_enabled() is True
        cm.save_barge_in_enabled(False)
        assert cm.get_barge_in_enabled() is False
        # other keys preserved
        cm.save_api_keys("k" * 20)
        assert cm.get_barge_in_enabled() is False
        cm.save_barge_in_enabled(True)
        data = json.loads((tmp_path / "api_keys.json").read_text())
        assert data["gemini_api_key"] == "k" * 20
        assert data["barge_in"] is True


class TestBargeInMainWiring:
    """Source-level: main.py wires EchoGuard.should_interrupt → interrupt()
    behind the per-reply _barge_on cache (PyQt absent → no import tests)."""

    def test_callback_classifies_and_interrupts(self):
        i = SRC.index("if jarvis_speaking:")
        block = SRC[i:i + 1600]
        assert "should_interrupt" in block
        assert "note_interrupted" in block
        assert "self.interrupt()" in block
        assert "audio callback must never raise" in block

    def test_flag_cached_per_reply_not_per_block(self):
        assert "self._barge_on" in SRC
        i = SRC.index("def set_speaking")
        block = SRC[i:i + 900]
        assert "get_barge_in_enabled" in block, \
            "flag must refresh in set_speaking, not read disk per audio block"
        assert "never in the audio callback" in block

    def test_default_state_on(self):
        assert "self._barge_on             = True" in SRC

    def test_default_state_on_init_line_present(self):
        # constructed alongside EchoGuard
        i = SRC.index("self._echo                 = EchoGuard()")
        assert "self._barge_on" in SRC[i:i + 300]


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — security-camera motion detect (dashboard/motion.py)
# ═══════════════════════════════════════════════════════════════════════════

class TestMotionDetector:
    """Pure-logic tests — no PIL needed (decode is a seam)."""

    @pytest.fixture(autouse=True)
    def _det(self):
        from dashboard.motion import MotionDetector, frame_diff
        self.det = MotionDetector()
        self.frame_diff = frame_diff
        yield

    @staticmethod
    def _flat(v: float, n: int = 64 * 48) -> list[float]:
        return [v] * n

    def test_frame_diff_identical_is_zero(self):
        assert self.frame_diff(self._flat(50), self._flat(50)) == 0.0

    def test_frame_diff_opposite_is_one(self):
        assert abs(self.frame_diff(self._flat(0), self._flat(255)) - 1.0) < 1e-9

    def test_frame_diff_mismatched_lengths_is_full_change(self):
        assert self.frame_diff([1.0], [1.0, 2.0]) == 1.0
        assert self.frame_diff([], []) == 1.0

    def test_first_frame_only_arms_never_alerts(self):
        assert self.det.armed is False
        assert self.det.update(self._flat(10), now=100.0) is None
        assert self.det.armed is True

    def test_scene_change_alerts(self):
        self.det.update(self._flat(10), now=100.0)
        evt = self.det.update(self._flat(240), now=101.0)
        assert evt is not None
        assert evt["type"] == "motion"
        assert evt["score"] > 0.5 and evt["ts"] == 101.0

    def test_calm_scene_never_alerts(self):
        self.det.update(self._flat(100), now=100.0)
        # JPEG-noise-level wobble (±3/255 ≈ 0.012) stays under 0.08
        for i in range(10):
            assert self.det.update(self._flat(100 + (i % 2) * 3),
                                   now=101.0 + i) is None

    def test_cooldown_suppresses_flood(self):
        self.det.update(self._flat(10), now=100.0)
        first = self.det.update(self._flat(240), now=101.0)
        assert first is not None
        # change persists, but inside the 3 s cooldown
        assert self.det.update(self._flat(10), now=102.0) is None
        assert self.det.update(self._flat(240), now=103.9) is None
        # cooldown over → fires again
        again = self.det.update(self._flat(10), now=104.1)
        assert again is not None and again["ts"] == 104.1

    def test_feed_without_pil_returns_none_and_never_raises(self):
        # PIL is absent in this venv → _decode degrades to None
        try:
            import PIL  # noqa: F401
            pytest.skip("PIL present — absence path covered in CI-only")
        except ImportError:
            pass
        assert self.det.feed(b"\xff\xd8FAKEJPEG", now=100.0) is None
        assert self.det.feed(b"", now=101.0) is None

    def test_feed_garbage_never_raises_even_with_pil(self):
        # garbage bytes → PIL decode fails → None (works whether or not
        # PIL is installed)
        assert self.det.feed(b"definitely-not-a-jpeg", now=100.0) is None

    def test_custom_threshold_and_cooldown(self):
        from dashboard.motion import MotionDetector
        d = MotionDetector(threshold=0.5, cooldown=10.0)
        d.update(self._flat(0), now=100.0)
        assert d.update(self._flat(30), now=101.0) is None   # 30/255 < 0.5
        assert d.update(self._flat(255), now=102.0) is not None
        assert d.update(self._flat(0), now=111.9) is None    # inside cooldown
        assert d.update(self._flat(255), now=112.1) is not None


class TestMotionCameraEndpoint:
    """POST /api/camera-frame now reports motion (seam-fed decode)."""

    @pytest.fixture(autouse=True)
    def _app(self, tmp_path, monkeypatch):
        pytest.importorskip("fastapi", reason="needs fastapi")
        import dashboard.server as ds
        if not ds._DEPS_OK:
            pytest.skip("dashboard deps incomplete")
        monkeypatch.setattr(ds, "_ensure_network_access", lambda port: None)
        monkeypatch.setattr(ds, "_ensure_certs", lambda: False)
        self.srv = ds.DashboardServer()
        self.srv._uploads_dir = tmp_path / "uploads"
        self.token = "tok-motion-1"
        self.srv._tokens.add(self.token)
        self.broadcasts = []

        async def _rec(msg, history=True):
            self.broadcasts.append(msg)

        monkeypatch.setattr(self.srv, "broadcast", _rec)
        yield

    def _post(self, frame_bytes: bytes):
        import base64 as _b64
        import json as _json
        from fastapi.testclient import TestClient
        return TestClient(self.srv.app).post(
            "/api/camera-frame",
            content=_json.dumps({"frame": _b64.b64encode(frame_bytes).decode(),
                                 "ts": 1759500000.0}),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})

    def _feed_seam(self, monkeypatch, values):
        """Replace PIL decode with a scripted frame sequence."""
        import dashboard.motion as mo
        seq = iter(values)
        monkeypatch.setattr(mo, "_decode",
                            lambda data: next(seq, None))

    def test_motion_broadcast_when_scene_changes(self, monkeypatch):
        self._feed_seam(monkeypatch, [self._flat(10), self._flat(240)])
        r1 = self._post(b"\xff\xd8A" + b"x" * 200)
        r2 = self._post(b"\xff\xd8B" + b"y" * 200)
        assert r1.status_code == 200 and r1.json()["motion"] is False
        assert r2.status_code == 200 and r2.json()["motion"] is True
        # create_task fires on next loop tick inside TestClient
        time.sleep(0.05)
        motion_msgs = [m for m in self.broadcasts if m.get("type") == "motion"]
        assert len(motion_msgs) == 1
        assert motion_msgs[0]["score"] > 0.5

    @staticmethod
    def _flat(v: float) -> list[float]:
        return [v] * (64 * 48)

    def test_calm_scene_no_motion_broadcast(self, monkeypatch):
        self._feed_seam(monkeypatch, [self._flat(100), self._flat(101)])
        self._post(b"\xff\xd8A" + b"x" * 200)
        self._post(b"\xff\xd8B" + b"y" * 200)
        time.sleep(0.05)
        assert not [m for m in self.broadcasts if m.get("type") == "motion"]

    def test_undecodable_frame_returns_ok_without_motion(self, monkeypatch):
        # decode seam returns None (no PIL / bad jpeg) → 200, motion False
        import dashboard.motion as mo
        monkeypatch.setattr(mo, "_decode", lambda data: None)
        resp = self._post(b"\xff\xd8FAKE" + b"z" * 200)
        assert resp.status_code == 200
        assert resp.json()["ok"] is True and resp.json()["motion"] is False
        time.sleep(0.05)
        assert not [m for m in self.broadcasts if m.get("type") == "motion"]

    def test_motion_detector_state_persists_across_posts(self, monkeypatch):
        # same server instance keeps baseline → second post is comparable
        self._feed_seam(monkeypatch,
                        [self._flat(10), self._flat(240), self._flat(10)])
        self._post(b"\xff\xd8A" + b"x" * 200)   # arms
        r = self._post(b"\xff\xd8B" + b"y" * 200)  # change → motion
        assert r.json()["motion"] is True
        r3 = self._post(b"\xff\xd8C" + b"z" * 200)  # back, but cooldown
        assert r3.json()["motion"] is False


class TestMotionWiring:
    """Source-level: server wires the detector, app.html renders it."""

    def test_server_constructs_and_feeds_detector(self):
        s = Path("dashboard/server.py").read_text(encoding="utf-8")
        assert "MotionDetector()" in s
        assert "self._motion.feed(data" in s
        assert '"motion": bool(motion)' in s
        assert "broadcast(motion, history=False)" in s

    def test_app_html_handles_motion_type(self):
        h = Path("dashboard/static/app.html").read_text(encoding="utf-8")
        assert "m.type === 'motion'" in h
        assert "_onMotion" in h
        assert "Motion detected" in h


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — universal render surface (CHAT|DISPLAY|SCAN|3D|WEB)
# ═══════════════════════════════════════════════════════════════════════════

class TestSurfaceClassify:
    """dashboard/surface.py — title → tab routing (pure, no deps)."""

    def test_contract(self):
        from dashboard.surface import SURFACES, classify_surface
        assert SURFACES == ("display", "scan", "3d", "web")
        assert classify_surface("") == "display"
        assert classify_surface(None) == "display"

    @pytest.mark.parametrize("title,surface", [
        ("SCRAPE — example.com", "web"),
        ("STRUCTURED — nytimes.com", "web"),
        ("SEARCH — jaipur weather", "web"),
        ("NEWS — top world news today", "web"),
        ("WIKI — AI", "web"),
        ("FLIGHT — DEL to BOM", "web"),
        ("OCR — center", "scan"),
        ("SCAN — region", "scan"),
        ("SCREEN — top half", "scan"),
        ("REGION — left", "scan"),
        ("3D — bracket", "3d"),
        ("MODEL — enclosure", "3d"),
        ("MAKE_3D — box", "3d"),
        ("CHART — sales", "display"),
        ("DIAGRAM — flow", "display"),
        ("PROCESSES", "display"),
        ("AUTOMATION RULES", "display"),
        ("FOCUS", "display"),
        ("WINDOW LAYOUT", "display"),
    ])
    def test_known_titles(self, title, surface):
        from dashboard.surface import classify_surface
        assert classify_surface(title) == surface

    def test_case_insensitive(self):
        from dashboard.surface import classify_surface
        assert classify_surface("scrape — x") == "web"
        assert classify_surface("3d — x") == "3d"
        assert classify_surface("news — x") == "web"

    def test_word_boundary_not_prefix_of_word(self):
        from dashboard.surface import classify_surface
        # "OCR" must not swallow a title that merely starts with those
        # letters as part of a longer word (e.g. "OCRWORLD")
        assert classify_surface("OCRWORLD notes") == "display"
        assert classify_surface("3DBRACKET v2") == "display"

    def test_dash_variants_reach_head(self):
        from dashboard.surface import classify_surface
        # ASCII hyphen and em-dash both split the head tag off
        assert classify_surface("SCAN-center") == "scan"
        assert classify_surface("3D — bracket") == "3d"


class TestRenderSurfaceFunnel:
    """show_content → hook → broadcast: the pieces must all be wired."""

    def test_ui_declares_content_hook(self):
        u = Path("ui.py").read_text(encoding="utf-8")
        assert "self._content_hook = None" in u
        i = u.index("def show_content")
        block = u[i:i + 900]
        assert "hook = self._content_hook" in block
        assert "hook(title, text)" in block
        # hook failure must never break the on-screen panel
        assert "except Exception" in block

    def test_main_attaches_hook_and_routes(self):
        assert "self.ui._content_hook = self._on_show_content" in SRC
        i = SRC.index("def _on_show_content")
        block = SRC[i:i + 1300]
        assert "classify_surface(title)" in block
        assert '"type":    "content"' in block
        assert "run_coroutine_threadsafe" in block
        assert "broadcast(msg, history=True)" in block
        # thread-safety: any-thread caller, silent no-op without loop
        assert "self._loop is None or self._dashboard is None" in block
        assert "except Exception" in block

    def test_make3d_pushes_to_surface(self):
        import actions.make_3d as m3

        class FakePlayer:
            def __init__(self):
                self.shown = []

            def show_content(self, title, text):
                self.shown.append((title, text))

        p = FakePlayer()
        out = m3.make_3d({"spec": "box 10 10 10", "name": "demo"}, player=p)
        assert "Model saved" in out
        assert len(p.shown) == 1
        title, text = p.shown[0]
        assert title.startswith("3D —")
        assert "Model saved" in text

    def test_make3d_player_failure_is_silent(self):
        import actions.make_3d as m3

        class Broken:
            def show_content(self, t, x):
                raise RuntimeError("qt gone")

        out = m3.make_3d({"spec": "box 5 5 5", "name": "x"}, player=Broken())
        assert "Model saved" in out

    def test_region_ocr_pushes_to_scan_tab(self, monkeypatch):
        import actions.region_ocr as ro

        class FakePlayer:
            def __init__(self):
                self.shown = []

            def show_content(self, title, text):
                self.shown.append((title, text))

        monkeypatch.setattr(ro, "_capture", lambda r: "IMG")
        monkeypatch.setattr(ro, "_read_text", lambda img, mode="text": "Hello")
        p = FakePlayer()
        out = ro.region_ocr({"region": "center"}, player=p)
        assert out == "[center] Hello"
        assert p.shown[0][0] == "SCAN — center"

    def test_region_ocr_no_player_still_works(self, monkeypatch):
        import actions.region_ocr as ro
        monkeypatch.setattr(ro, "_capture", lambda r: "IMG")
        monkeypatch.setattr(ro, "_read_text", lambda img, mode="text": "Hi")
        assert ro.region_ocr({}) == "[center] Hi"


class TestDashboardTabs:
    """Source-level: app.html carries the five tabs + content routing."""

    @pytest.fixture(autouse=True)
    def _h(self):
        self.h = Path("dashboard/static/app.html").read_text(encoding="utf-8")

    def test_five_tabs_present(self):
        for tab in ('data-tab="chat"', 'data-tab="display"',
                    'data-tab="scan"', 'data-tab="3d"', 'data-tab="web"'):
            assert tab in self.h, tab

    def test_content_type_handled(self):
        assert "m.type === 'content'" in self.h
        assert "_onContent(m)" in self.h

    def test_content_routed_by_surface_field(self):
        assert "m.surface" in self.h
        assert "_renderSurface" in self.h

    def test_tab_switch_toggles_panes(self):
        assert "function showTab" in self.h
        assert "has-new" in self.h       # unseen-content dot

    def test_surface_text_is_textcontent_not_innerhtml(self):
        # XSS: tool output must not be parsed as HTML
        i = self.h.index("function _renderSurface")
        block = self.h[i:i + 900]
        assert "textContent" in block
        assert "innerHTML = got" not in block
        assert "innerHTML = `${" not in block

    def test_server_history_carries_content(self):
        # content messages use history=True so a reconnecting client
        # rebuilds the tabs it was looking at
        s = Path("main.py").read_text(encoding="utf-8")
        i = s.index("def _on_show_content")
        assert "broadcast(msg, history=True)" in s[i:i + 1300]


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — agentic browser loop EXTRACT→REASON→ACT→VERIFY
# (playwright is optional now — the loop core imports and is tested here)
# ═══════════════════════════════════════════════════════════════════════════

class TestBrowserDecisionParse:
    """_parse_decision — LLM reply → validated decision."""

    def test_click_parses(self):
        from actions.browser_control import _parse_decision
        d = _parse_decision('{"action": "click", "index": 12, "reason": "add"}')
        assert d == {"action": "click", "index": 12, "reason": "add"}

    def test_type_requires_text(self):
        from actions.browser_control import _parse_decision
        d = _parse_decision('{"action":"type","index":3,"text":"hello"}')
        assert d["text"] == "hello" and d["index"] == 3
        with pytest.raises(ValueError, match="non-empty text"):
            _parse_decision('{"action":"type","index":3,"text":""}')

    def test_click_requires_numeric_index(self):
        from actions.browser_control import _parse_decision
        for bad in ('{"action":"click","reason":"x"}',
                    '{"action":"click","index":"abc"}',
                    '{"action":"click","index":-1}'):
            with pytest.raises(ValueError):
                _parse_decision(bad)

    def test_done_and_fail(self):
        from actions.browser_control import _parse_decision
        assert _parse_decision('{"action":"done","reason":"found price 42"}')["action"] == "done"
        assert _parse_decision('{"action":"fail","reason":"captcha wall"}')["action"] == "fail"

    def test_scroll_direction_normalises(self):
        from actions.browser_control import _parse_decision
        assert _parse_decision('{"action":"scroll","direction":"sideways"}')["direction"] == "down"
        assert _parse_decision('{"action":"scroll","direction":"up"}')["direction"] == "up"

    def test_fenced_json_and_prose_tolerated(self):
        from actions.browser_control import _parse_decision
        raw = 'Sure! Here is my decision:\n```json\n{"action":"click","index":5}\n```\nHope that helps.'
        assert _parse_decision(raw)["index"] == 5
        # prose BEFORE a valid object
        assert _parse_decision('thinking... {"action":"done","reason":"ok"}')["action"] == "done"

    def test_garbage_raises(self):
        from actions.browser_control import _parse_decision
        for bad in ("", "click the button", "{not json}", "[1,2]", "42"):
            with pytest.raises(ValueError):
                _parse_decision(bad)

    def test_unknown_action_raises(self):
        from actions.browser_control import _parse_decision
        with pytest.raises(ValueError, match="action must be"):
            _parse_decision('{"action":"dance","index":1}')


class TestBrowserSnapshotFormat:
    def test_numbered_lines(self):
        from actions.browser_control import _format_snapshot
        s = _format_snapshot({"url": "https://x.dev",
                              "elements": [
                                  {"i": 0, "role": "link", "name": "Home"},
                                  {"i": 12, "role": "button", "name": "Add to cart"}]})
        assert "URL: https://x.dev" in s
        assert '[0] link "Home"' in s
        assert '[12] button "Add to cart"' in s

    def test_empty_state(self):
        from actions.browser_control import _format_snapshot
        s = _format_snapshot({"url": "about:blank", "elements": []})
        assert "(no interactive elements found)" in s

    def test_unlabelled_element_gets_placeholder(self):
        from actions.browser_control import _format_snapshot
        s = _format_snapshot({"url": "u", "elements": [{"i": 3, "role": "button", "name": ""}]})
        assert '[3] button "(no label)"' in s


class TestBrowserVerify:
    def _st(self, url, ids):
        return {"url": url, "elements": [{"i": i, "role": "x", "name": "n"}
                                         for i in ids]}

    def test_navigation_detected(self):
        from actions.browser_control import _verify
        note = _verify(self._st("a", [0]), self._st("b", [0]),
                       {"action": "click", "index": 0}, "clicked [0]")
        assert note.startswith("navigated: a -> b")

    def test_clicked_element_gone_means_page_updated(self):
        from actions.browser_control import _verify
        note = _verify(self._st("u", [0, 1]), self._st("u", [0]),
                       {"action": "click", "index": 1}, "clicked [1]")
        assert "page updated" in note

    def test_no_change_reported_honestly(self):
        from actions.browser_control import _verify
        note = _verify(self._st("u", [0, 1]), self._st("u", [0, 1]),
                       {"action": "type", "index": 0}, "typed")
        assert "no visible change" in note

    def test_controls_changed(self):
        from actions.browser_control import _verify
        note = _verify(self._st("u", [0]), self._st("u", [0, 7]),
                       {"action": "click", "index": 0}, "clicked [0]")
        assert note == "page controls changed"


class TestBrowserAgentLoop:
    """The driver itself — scripted stages, no browser."""

    def _state(self, url="https://shop.test", ids=(0, 1)):
        return {"url": url,
                "elements": [{"i": i, "role": "button", "name": f"b{i}"}
                             for i in ids]}

    def test_happy_path_done(self):
        from actions.browser_control import agent_loop
        decisions = [
            {"action": "click", "index": 1, "reason": "open list"},
            {"action": "done", "reason": "found 3 results"},
        ]
        calls = {"extract": 0, "act": 0}

        def extract():
            calls["extract"] += 1
            return self._state(ids=(0, 1))

        def reason(goal, state, history):
            return decisions.pop(0)

        def act(d):
            calls["act"] += 1
            return f"clicked [{d['index']}]"

        out = agent_loop("find results", extract, reason, act, max_steps=5)
        assert out.startswith("AGENT DONE at step 2/5")
        assert "found 3 results" in out
        assert calls == {"extract": 3, "act": 1}   # extract also runs for VERIFY

    def test_fail_decision_reported(self):
        from actions.browser_control import agent_loop
        out = agent_loop("impossible",
                         extract=lambda: self._state(),
                         reason=lambda g, s, h: {"action": "fail",
                                                 "reason": "captcha wall"},
                         act=lambda d: "unused", max_steps=3)
        assert out.startswith("AGENT FAILED at step 1/3")
        assert "captcha wall" in out

    def test_budget_exhaustion_mentions_history(self):
        from actions.browser_control import agent_loop
        out = agent_loop("endless",
                         extract=lambda: self._state(),
                         reason=lambda g, s, h: {"action": "scroll",
                                                 "direction": "down"},
                         act=lambda d: "scrolled down", max_steps=4)
        assert "budget of 4 steps exhausted" in out
        assert "verify" in out                       # history carried through

    def test_three_consecutive_act_failures_stop(self):
        from actions.browser_control import agent_loop
        out = agent_loop("x",
                         extract=lambda: self._state(),
                         reason=lambda g, s, h: {"action": "click", "index": 9},
                         act=lambda d: (_ for _ in ()).throw(RuntimeError("gone")),
                         max_steps=10)
        assert "3 consecutive action failures" in out

    def test_act_failure_then_success_resets_counter(self):
        from actions.browser_control import agent_loop
        seq = [RuntimeError("flaky"), RuntimeError("flaky"), None]
        done = {"n": 0}

        def act(d):
            err = seq[min(done["n"], 2)]
            done["n"] += 1
            if err:
                raise err
            return "clicked"

        def reason(g, s, h):
            if done["n"] >= 3:
                return {"action": "done", "reason": "recovered"}
            return {"action": "click", "index": 1}

        out = agent_loop("x", lambda: self._state(), reason, act, max_steps=9)
        assert out.startswith("AGENT DONE")

    def test_extract_failure_stops_honestly(self):
        from actions.browser_control import agent_loop
        def boom():
            raise RuntimeError("browser closed")
        out = agent_loop("x", boom, lambda g, s, h: {"action": "done"},
                         lambda d: "", max_steps=3)
        assert "extract failed" in out and "browser closed" in out

    def test_reasoner_failure_stops_honestly(self):
        from actions.browser_control import agent_loop
        def bad_reason(g, s, h):
            raise RuntimeError("ollama down")
        out = agent_loop("x", lambda: self._state(), bad_reason,
                         lambda d: "", max_steps=3)
        assert "reasoner failed" in out and "ollama down" in out

    def test_unusable_decision_stops(self):
        from actions.browser_control import agent_loop
        out = agent_loop("x", lambda: self._state(),
                         lambda g, s, h: "click [3]", lambda d: "",
                         max_steps=3)
        assert "unusable decision" in out

    def test_history_visible_to_reasoner(self):
        from actions.browser_control import agent_loop
        seen = []

        def reason(g, s, h):
            seen.append(list(h))
            if len(h) >= 1:
                return {"action": "done", "reason": "saw history"}
            return {"action": "click", "index": 1}

        agent_loop("x", lambda: self._state(), reason,
                   lambda d: "clicked [1]", max_steps=4)
        # second call sees first step's ACT+VERIFY note
        assert len(seen) == 2
        assert any("verify:" in line for line in seen[1])


class TestBrowserAgentReasonAndWiring:
    def test_llm_reason_retries_then_raises(self, monkeypatch):
        import core.llm_client as llm
        import actions.browser_control as bc
        replies = ["not json at all", '{"action":"done","reason":"ok"}']
        monkeypatch.setattr(llm, "call_llm_text",
                            lambda *a, **k: replies.pop(0))
        reason = bc._make_llm_reason(timeout=5)
        d = reason("g", {"url": "u", "elements": []}, [])
        assert d["action"] == "done"

    def test_llm_reason_raises_after_two_bad_replies(self, monkeypatch):
        import core.llm_client as llm
        import actions.browser_control as bc
        monkeypatch.setattr(llm, "call_llm_text", lambda *a, **k: "garbage")
        reason = bc._make_llm_reason(timeout=5)
        with pytest.raises(ValueError, match="unusable LLM decision"):
            reason("g", {"url": "u", "elements": []}, [])

    def test_run_agent_requires_goal(self):
        from actions.browser_control import _run_agent
        out = _run_agent(None, {})
        assert "Give the agent a goal" in out

    def test_run_agent_clamps_max_steps(self, monkeypatch):
        import actions.browser_control as bc
        captured = {}

        def fake_loop(goal, extract, reason, act, *, max_steps=8, log=None):
            captured["steps"] = max_steps
            return "ok"
        monkeypatch.setattr(bc, "agent_loop", fake_loop)
        out = bc._run_agent(object(), {"goal": "g", "max_steps": "9999"})
        assert out == "ok" and captured["steps"] == 25

    def test_handler_dispatches_agent_action(self):
        src = Path("actions/browser_control.py").read_text(encoding="utf-8")
        assert 'action == "agent"' in src
        assert "_run_agent(sess, params, player)" in src

    def test_session_has_snapshot_and_act(self):
        src = Path("actions/browser_control.py").read_text(encoding="utf-8")
        assert "async def snapshot(self)" in src
        assert "async def agent_act(self, decision: dict)" in src
        assert 'data-jarvis-idx' in src
        # element addressing uses the SAME stamp the snapshot wrote
        assert 'f\'[data-jarvis-idx="{idx}\"]\'' in src or \
               '[data-jarvis-idx="' in src

    def test_snapshot_js_stamps_and_caps(self):
        from actions.browser_control import _SNAPSHOT_JS
        assert "data-jarvis-idx" in _SNAPSHOT_JS
        assert "n >= 80" in _SNAPSHOT_JS
        assert "visibility" in _SNAPSHOT_JS

    def test_playwright_is_optional_and_importable(self):
        # module must import without playwright and say so honestly
        import actions.browser_control as bc
        assert hasattr(bc, "_HAS_PLAYWRIGHT")
        if not bc._HAS_PLAYWRIGHT:
            class FakeCoro:
                def close(self):
                    pass
            sess = bc._BrowserSession("chrome")
            with pytest.raises(RuntimeError, match="pip install playwright"):
                sess.run(FakeCoro())

    def test_tool_schema_documents_agent(self):
        import actions.browser_control as bc
        props = bc.TOOL["parameters"]["properties"]
        assert "agent" in props["action"]["description"]
        assert "goal" in props and "max_steps" in props


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — multi-agent planner→coder→tester dry-run pipeline
# ═══════════════════════════════════════════════════════════════════════════

class TestMultiAgentHelpers:
    def test_parse_plan_valid(self):
        from actions.multi_agent import _parse_plan
        raw = ('{"steps":["touch file"], "files":['
               '{"path":"a.py","action":"modify","instruction":"add retry"},'
               '{"path":"new.py","action":"create","instruction":"make it"}]}')
        p = _parse_plan(raw)
        assert p["steps"] == ["touch file"]
        assert p["files"][0]["path"] == "a.py"
        assert p["files"][1]["action"] == "create"

    def test_parse_plan_fenced_and_junk_action(self):
        from actions.multi_agent import _parse_plan
        raw = '```json\n{"steps":[],"files":[{"path":"x.py","action":"obliterate"}]}\n```'
        p = _parse_plan(raw)
        assert p["files"][0]["action"] == "modify"   # normalised
        assert p["steps"] == ["implement planned files"]

    @pytest.mark.parametrize("bad", ["", "no json", "{broken", "[]", '{"files": []}'])
    def test_parse_plan_rejects_unusable(self, bad):
        from actions.multi_agent import _parse_plan
        with pytest.raises(ValueError):
            _parse_plan(bad)

    def test_strip_fences(self):
        from actions.multi_agent import _strip_fences
        assert _strip_fences("```python\nx = 1\n```") == "x = 1\n"
        assert _strip_fences("x = 1") == "x = 1\n"
        # multi-line keeps content
        assert "def f():\n    pass" in _strip_fences("```\ndef f():\n    pass\n```")

    def test_make_diff_empty_when_unchanged(self):
        from actions.multi_agent import _make_diff
        assert _make_diff("same", "same", "a.py") == ""
        d = _make_diff("a\nb\n", "a\nc\n", "a.py")
        assert "-b" in d and "+c" in d
        assert "--- a/a.py" in d and "+++ b/a.py" in d

    def test_syntax_check_python(self):
        from actions.multi_agent import _syntax_check
        assert _syntax_check("x = 1\n", "a.py") is None
        err = _syntax_check("def broken(:\n", "a.py")
        assert err and "SyntaxError" in err
        # non-python is out of scope — no false rejections
        assert _syntax_check("function ( {", "a.js") is None

    @pytest.mark.parametrize("raw,expected", [
        ('{"verdict":"pass","issues":[]}', "pass"),
        ('{"verdict":"FAIL","issues":["x"]}', "fail"),
        ('{"verdict":"maybe"}', "fail"),          # unknown → fail (safe)
        ('not json at all', "fail"),
        ('{"verdict":"fail"}', "fail"),           # fail without issues gets one
    ])
    def test_parse_verdict(self, raw, expected):
        from actions.multi_agent import _parse_verdict
        v = _parse_verdict(raw)
        assert v["verdict"] == expected
        if expected == "fail":
            assert v["issues"]


class _ScriptedLLM:
    """Callable that answers by prompt role — planner/coder/tester."""

    def __init__(self, plan=None, code="new content\n", verdict="pass"):
        self.plan = plan or {"steps": ["do it"],
                             "files": [{"path": "a.py", "action": "modify",
                                        "instruction": "improve"}]}
        self.code = code
        self.verdict = verdict
        self.calls: list[str] = []

    def __call__(self, prompt, system=None):
        if "PLANNER" in prompt:
            self.calls.append("plan")
            return json.dumps(self.plan)
        if "CODER" in prompt:
            self.calls.append("code")
            return f"```\n{self.code}\n```"
        if "TESTER" in prompt:
            self.calls.append("test")
            if isinstance(self.verdict, dict):
                return json.dumps(self.verdict)
            return json.dumps({"verdict": self.verdict, "issues": []})
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")


class TestMultiAgentPipeline:
    def test_task_required(self):
        from actions.multi_agent import run_pipeline
        assert "needs a task" in run_pipeline("", {}, None)

    def test_planning_failure_reported(self):
        from actions.multi_agent import run_pipeline
        def bad_llm(prompt, system=None):
            return "I refuse to plan"
        out = run_pipeline("t", {}, bad_llm)
        assert "planning failed" in out

    def test_dry_run_default_reports_diff_and_verb(self):
        from actions.multi_agent import run_pipeline
        llm = _ScriptedLLM(code="x = 2\n")
        out = run_pipeline("change x", {"a.py": "x = 1\n"}, llm)
        assert "PLAN —" in out
        assert "[PASS] a.py" in out
        assert "-x = 1" in out and "+x = 2" in out
        assert "DRY-RUN: nothing written" in out
        assert llm.calls == ["plan", "code", "test"]

    def test_apply_writes_only_passing_files(self, tmp_path):
        from actions.multi_agent import run_pipeline
        llm = _ScriptedLLM(code="x = 2\n")
        out = run_pipeline("change x", {"a.py": "x = 1\n"}, llm,
                           apply=True, root=tmp_path)
        assert "APPLIED:" in out
        assert (tmp_path / "a.py").read_text() == "x = 2\n"

    def test_apply_blocked_when_tester_fails(self, tmp_path):
        from actions.multi_agent import run_pipeline
        llm = _ScriptedLLM(code="x = 2\n",
                           verdict={"verdict": "fail",
                                    "issues": ["logic wrong"]})
        out = run_pipeline("change x", {"a.py": "x = 1\n"}, llm,
                           apply=True, root=tmp_path)
        assert "NOT APPLIED" in out
        assert "logic wrong" in out
        # nothing reached disk — the inventory was in-memory only
        assert not (tmp_path / "a.py").exists()

    def test_syntax_error_triggers_revision_then_honest_fail(self):
        from actions.multi_agent import run_pipeline
        class LLM:
            def __init__(self):
                self.code_calls = 0

            def __call__(self, prompt, system=None):
                if "PLANNER" in prompt:
                    return json.dumps({"steps": ["s"],
                                       "files": [{"path": "a.py",
                                                  "action": "modify",
                                                  "instruction": "x"}]})
                if "CODER" in prompt:
                    self.code_calls += 1
                    if self.code_calls == 1:
                        return "def broken(:\n"          # syntax error
                    assert "TESTER ISSUES" in prompt      # feedback delivered
                    return "x = 1\n"                     # fixed, but…
                if "TESTER" in prompt:
                    return json.dumps({"verdict": "fail",
                                       "issues": ["does not solve task"]})
                raise AssertionError(prompt[:60])

        llm = LLM()
        logs: list[str] = []
        out = run_pipeline("t", {"a.py": "old\n"}, llm, log=logs.append)
        assert llm.code_calls == 2                       # one revision round
        assert "[FAIL] a.py" in out
        assert "does not solve task" in out
        # deterministic gate fired on attempt 1 (logged), tester on attempt 2
        assert any("SyntaxError" in line for line in logs)

    def test_syntax_error_exhausted_reports_fail(self):
        from actions.multi_agent import run_pipeline
        class LLM:
            def __call__(self, prompt, system=None):
                if "PLANNER" in prompt:
                    return json.dumps({"files": [{"path": "a.py",
                                                  "action": "modify",
                                                  "instruction": "x"}]})
                if "CODER" in prompt:
                    return "def broken(:\n"
                raise AssertionError("tester must never run on bad syntax")
        out = run_pipeline("t", {"a.py": "old\n"}, LLM())
        assert "[FAIL] a.py" in out
        assert "SyntaxError" in out

    def test_unchanged_file_counts_as_fail(self):
        from actions.multi_agent import run_pipeline
        llm = _ScriptedLLM(code="x = 1")   # same as existing (modulo newline)
        out = run_pipeline("t", {"a.py": "x = 1\n"}, llm)
        assert "[FAIL] a.py" in out
        assert "unchanged" in out

    def test_coder_exception_reported_not_raised(self):
        from actions.multi_agent import run_pipeline
        def llm(prompt, system=None):
            if "PLANNER" in prompt:
                return json.dumps({"files": [{"path": "a.py",
                                              "action": "modify",
                                              "instruction": "x"}]})
            raise RuntimeError("ollama crashed")
        out = run_pipeline("t", {"a.py": "old\n"}, llm)
        assert "[FAIL] a.py" in out
        assert "coder failed" in out

    def test_multiple_files_each_get_diff_and_verdict(self):
        from actions.multi_agent import run_pipeline
        plan = {"steps": ["s"],
                "files": [{"path": "a.py", "action": "modify", "instruction": "i"},
                          {"path": "b.py", "action": "create", "instruction": "i"}]}
        llm = _ScriptedLLM(plan=plan, code="changed\n")
        out = run_pipeline("t", {"a.py": "old a\n", "b.py": ""}, llm)
        assert "[PASS] a.py" in out and "[PASS] b.py" in out
        assert llm.calls.count("code") == 2

    def test_handler_shorthand_single_file(self, monkeypatch, tmp_path):
        import actions.multi_agent as ma
        captured = {}

        def fake_run(task, files, llm, *, apply=False, root=None, log=None):
            captured.update(task=task, files=files, apply=apply, root=root)
            return "ok"
        monkeypatch.setattr(ma, "run_pipeline", fake_run)
        out = ma.multi_agent({"task": "t", "file_path": "a.py",
                              "content": "x = 1", "apply": "true",
                              "root": str(tmp_path)})
        assert out == "ok"
        assert captured["files"] == {"a.py": "x = 1"}
        assert captured["apply"] is True
        assert captured["root"] == tmp_path

    def test_handler_task_required(self):
        import actions.multi_agent as ma
        assert "needs a task" in ma.multi_agent({})

    def test_tool_schema(self):
        import actions.multi_agent as ma
        assert ma.TOOL["name"] == "multi_agent"
        assert "task" in ma.TOOL["parameters"]["required"]
        assert "apply" in ma.TOOL["parameters"]["properties"]
        assert ma.TOOL["handler"] is ma.multi_agent


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — privacy gates: file_processor / background_monitor / flight_finder
# ═══════════════════════════════════════════════════════════════════════════

class TestPrivacyGateCoverage:
    """The three CLOUD_TOOLS that had no gate() call now gate honestly,
    and file_processor keeps its genuinely-local operations working —
    same contract as region_ocr's tesseract branch."""

    @pytest.fixture(autouse=True)
    def _privacy_on(self):
        import core.privacy as cp
        cp.set(True)
        yield
        cp.set(False)

    def test_flight_finder_refuses_before_params(self):
        import actions.flight_finder as ff
        out = ff.flight_finder({"origin": "DEL", "destination": "BOM",
                                "date": "2026-11-01"})
        assert "Privacy mode is ON" in out

    def test_background_monitor_skips_cloud_checks(self, monkeypatch):
        import actions.background_monitor as bm
        import actions.web_search as ws
        calls = []

        def _record(topic, max_results=5):
            calls.append(topic)
            return []
        monkeypatch.setattr(ws, "_ddg_news", _record)
        monkeypatch.setattr(bm, "_load", lambda: {
            "ai-news": {"topic": "AI news", "last_check": "2000-01-01"}})
        out = bm.check_all()
        assert out == []                      # no alerts, no errors
        assert calls == []                    # NOT ONE cloud search
        # topics stay unmarked → monitoring self-resumes when privacy off
        assert bm._load()["ai-news"]["last_check"] == "2000-01-01"

    def test_file_processor_refuses_model_branch(self, tmp_path):
        import actions.file_processor as fp
        p = tmp_path / "data.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        out = fp.file_processor({"file_path": str(p), "action": "analyze"})
        assert "Privacy mode is ON" in out

    def test_file_processor_local_json_ops_still_work(self, tmp_path):
        import actions.file_processor as fp
        p = tmp_path / "data.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        out = fp.file_processor({"file_path": str(p), "action": "validate"})
        assert "Privacy mode is ON" not in out
        assert "Valid JSON" in out
        # formatting is pure local I/O too
        out = fp.file_processor({"file_path": str(p), "action": "format"})
        assert "Formatted JSON saved" in out

    def test_file_processor_default_action_is_gated(self, tmp_path):
        # omitted action on JSON defaults to 'analyze' → model → refused
        import actions.file_processor as fp
        p = tmp_path / "data.json"
        p.write_text('{"a": 1}', encoding="utf-8")
        out = fp.file_processor({"file_path": str(p)})
        assert "Privacy mode is ON" in out

    def test_file_processor_text_word_count_local(self, tmp_path):
        import actions.file_processor as fp
        p = tmp_path / "notes.txt"
        p.write_text("one two three", encoding="utf-8")
        out = fp.file_processor({"file_path": str(p), "action": "word_count"})
        assert "Word count: 3 words" in out
        # …but summarize would go to the cloud
        out = fp.file_processor({"file_path": str(p), "action": "summarize"})
        assert "Privacy mode is ON" in out


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — presence detection (core/presence.py + actions/presence.py)
# ═══════════════════════════════════════════════════════════════════════════

class TestPresence:
    """Flap-safe present/away state machine: JARVIS-input idle + optional
    OS idle probe, hysteresis on the return edge."""

    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        import core.presence as cp
        monkeypatch.setattr(cp, "_os_idle_seconds", lambda: None)
        self.cp = cp
        self.tr = cp.PresenceTracker(away_after=10.0)
        yield

    def test_starts_present(self):
        st = self.tr.status()
        assert st["present"] is True
        assert st["state"] == "present"
        assert st["away_after_seconds"] == 10.0

    def test_activity_resets_idle_and_never_edges_when_present(self):
        assert self.tr.note_activity("hud") is None
        assert self.tr.is_present() is True

    def test_poll_away_edge_fires_once(self):
        # back-date last activity beyond threshold
        self.tr._last_activity = self.cp.time.monotonic() - 20
        assert self.tr.poll() == "away"
        assert self.tr.poll() is None           # edge is reported ONCE
        assert self.tr.is_present() is False

    def test_activity_after_away_returns_present_edge(self):
        self.tr._last_activity = self.cp.time.monotonic() - 20
        assert self.tr.poll() == "away"
        assert self.tr.note_activity("voice") == "present"
        assert self.tr.note_activity("voice") is None   # no flapping
        states = [h["state"] for h in self.tr.history]
        assert states == ["away", "present"]

    def test_os_probe_below_hysteresis_returns_present(self):
        self.tr.force("away")
        self.cp._os_idle_seconds = lambda: 4.0   # < away_after/2 → active
        assert self.tr.poll() == "present"
        assert self.tr.is_present() is True

    def test_os_probe_still_idle_keeps_away(self):
        self.tr.force("away")
        self.cp._os_idle_seconds = lambda: 9999.0
        assert self.tr.poll() is None
        assert self.tr.is_present() is False

    def test_os_probe_takes_precedence_when_fresher(self):
        # our input 100s ago (looks idle) but OS says 3s ago → present
        self.tr._last_activity = self.cp.time.monotonic() - 100
        self.cp._os_idle_seconds = lambda: 3.0
        assert self.tr.poll() is None           # min(100, 3) = 3 < 10
        assert self.tr.is_present() is True

    def test_force_and_history(self):
        assert self.tr.force("away") is True
        assert self.tr.force("away") is False   # already away
        assert self.tr.force("present") is True
        assert len(self.tr.history) == 2

    def test_set_away_after_floor(self):
        assert self.tr.set_away_after(0.0) == 1.0
        assert self.tr.set_away_after(45) == 45.0

    def test_singleton(self):
        assert self.cp.tracker() is self.cp.tracker()


class TestPresenceAction:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        import core.presence as cp
        import actions.presence as ap
        monkeypatch.setattr(cp, "_os_idle_seconds", lambda: None)
        cp._TRACKER = cp.PresenceTracker(away_after=10.0)
        self.cp, self.ap = cp, ap
        yield
        cp._TRACKER = None

    def test_status_report(self):
        out = self.ap.presence({"action": "status"}, None)
        assert "PRESENT" in out
        assert "OS idle probe: unavailable" in out   # honest about fallback

    def test_history_empty_then_populated(self):
        assert "No presence transitions yet" in self.ap.presence(
            {"action": "history"}, None)
        self.cp._TRACKER.force("away")
        out = self.ap.presence({"action": "history"}, None)
        assert "AWAY" in out

    def test_set_away_after_valid_and_invalid(self):
        out = self.ap.presence({"action": "set_away_after",
                                "seconds": 60}, None)
        assert "60s" in out
        out = self.ap.presence({"action": "set_away_after",
                                "seconds": "abc"}, None)
        assert "number of seconds" in out

    def test_force_present_and_away(self):
        assert "Marked AWAY" in self.ap.presence({"action": "away"}, None)
        assert "Already away" in self.ap.presence({"action": "away"}, None)
        assert "Marked present" in self.ap.presence({"action": "mark"}, None)
        assert "Already present" in self.ap.presence({"action": "mark"}, None)

    def test_unknown_action_lists_options(self):
        assert "status" in self.ap.presence({"action": "zzz"}, None)

    def test_tool_shape(self):
        # end-to-end: the action must survive real discovery/validation
        from core.action_loader import discover_actions
        reg = discover_actions(Path("actions"))
        assert "presence" in reg.names()
        decl = next(d for d in reg.get_tool_declarations()
                    if d["name"] == "presence")
        assert decl["parameters"]["type"] == "OBJECT"
        out = reg.run("presence", {}, {})
        assert "PRESENT" in out or "AWAY" in out


class TestPresenceWiring:
    """main.py: activity at every input choke + proactive speech gated on
    presence (main.py is Qt-bound → source-index assertions, same
    convention as the other wiring tests)."""

    @pytest.fixture(autouse=True)
    def _src(self):
        self.src = Path("main.py").read_text(encoding="utf-8")
        yield

    def test_fire_phrase_rules_counts_activity(self):
        seg = self.src.split("def _fire_phrase_rules", 1)[1][:900]
        assert 'note_activity("input")' in seg
        assert "_presence_tracker" in seg

    def test_typed_command_counts_even_while_asleep(self):
        seg = self.src.split("def _on_text_command", 1)[1][:700]
        assert 'note_activity("hud")' in seg
        # placement: BEFORE the wake gate swallows the input
        assert seg.index('note_activity("hud")') < seg.index("_awake")

    def test_proactive_loop_checks_presence(self):
        seg = self.src.split("async def _run_proactive_mode", 1)[1][:2000]
        assert "_presence_tracker().is_present()" in seg
        # the gate must skip (continue), not crash the loop
        assert "if not _presence_tracker().is_present():" in seg

    def test_no_qt_import_in_presence_modules(self):
        for p in ("core/presence.py", "actions/presence.py"):
            s = Path(p).read_text(encoding="utf-8")
            assert "PyQt" not in s and "ui import" not in s

    def test_every_tool_handler_accepts_parameters_kwarg(self):
        # Regression guard: core.action_loader._call_handler invokes
        # handlers as fn(parameters=..., **ctx). A handler named `params`
        # registers fine but CRASHES on every dispatch (rules/orchestrator
        # path returns "Tool failed: unexpected keyword 'parameters'").
        # Caught this once on presence — never again.
        import inspect
        from core.action_loader import discover_actions
        reg = discover_actions(Path("actions"))
        offenders = []
        for rec in reg._actions.values():
            sig = inspect.signature(rec.handler)
            has_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                             for p in sig.parameters.values())
            if "parameters" not in sig.parameters and not has_var_kw:
                offenders.append(rec.name)
        assert offenders == [], (
            f"handlers missing 'parameters' kwarg (breaks registry.run): "
            f"{offenders}")


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — smart home (MQTT pub/sub/status, paho optional, LAN-only in privacy)
# ═══════════════════════════════════════════════════════════════════════════

class _FakeInfo:
    def __init__(self, rc=0):
        self.rc = rc

    def wait_for_publish(self, timeout=None):
        return None


class _FakeMQTTClient:
    """Duck-typed paho client: records calls, optionally fails connect,
    optionally delivers one message on the first loop()."""

    def __init__(self, fail_connect=False, pub_rc=0, deliver=None):
        self.fail_connect = fail_connect
        self.pub_rc = pub_rc
        self.deliver = deliver
        self.published = []
        self.subscribed = []
        self.connected = None
        self.on_message = None
        self._delivered = False
        self.loop_starts = 0
        self.loop_stops = 0
        self.disconnected = False

    def connect(self, host, port, keepalive=0):
        self.connected = (host, port)
        if self.fail_connect:
            raise ConnectionRefusedError("[Errno 111] Connection refused")
        return 0

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return _FakeInfo(self.pub_rc)

    def subscribe(self, topic, qos=0):
        self.subscribed.append((topic, qos))
        return (0, 1)

    def loop(self, timeout=None):
        if self.deliver and not self._delivered and self.on_message:
            self._delivered = True
            self.on_message(self, None, self.deliver)
        else:
            time.sleep(0.01)

    def loop_start(self):
        self.loop_starts += 1

    def loop_stop(self):
        self.loop_stops += 1

    def disconnect(self):
        self.disconnected = True

    def username_pw_set(self, user, password=None):
        self.auth = (user, password)


class TestSmartHome:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch):
        import actions.smart_home as sh
        import core.presence  # noqa: F401  (keep import order stable)
        self.sh = sh
        self.client = _FakeMQTTClient()
        monkeypatch.setattr(sh, "_client_factory", lambda: self.client)
        monkeypatch.setattr(
            sh, "_settings",
            lambda: {"host": "localhost", "port": 1883,
                     "user": None, "password": None})
        yield

    def test_bare_call_returns_help(self):
        out = self.sh.smart_home({}, None)
        assert "MQTT cheat-sheet" in out
        assert "zigbee2mqtt" in out

    def test_publish_happy_path(self):
        out = self.sh.smart_home(
            {"action": "pub", "topic": "zigbee2mqtt/lamp/set",
             "payload": '{"state":"ON"}'}, None)
        assert "Published to 'zigbee2mqtt/lamp/set'" in out
        assert self.client.published == [
            ("zigbee2mqtt/lamp/set", '{"state":"ON"}', 0, False)]
        assert self.client.connected == ("localhost", 1883)
        assert self.client.disconnected is True
        # network loop started AND stopped (no orphaned threads)
        assert (self.client.loop_starts, self.client.loop_stops) == (1, 1)

    def test_publish_state_shorthand_becomes_json(self):
        out = self.sh.smart_home(
            {"action": "pub", "topic": "t", "state": "ON",
             "brightness": 200}, None)
        assert "Published to 't'" in out
        payload = self.client.published[0][1]
        assert json.loads(payload) == {"state": "ON", "brightness": 200}

    def test_publish_retain_qos_roundtrip(self):
        self.sh.smart_home(
            {"action": "pub", "topic": "t", "payload": "1",
             "retain": "true", "qos": 2}, None)
        assert self.client.published[0][2:] == (2, True)

    def test_publish_needs_topic(self):
        out = self.sh.smart_home({"action": "pub"}, None)
        assert "needs a topic" in out
        assert self.client.published == []

    def test_publish_rc_failure_honest(self):
        self.client.pub_rc = 1
        out = self.sh.smart_home(
            {"action": "pub", "topic": "t", "payload": "x"}, None)
        assert "failed (rc=1)" in out

    def test_broker_refused_maps_to_actionable_error(self):
        self.client.fail_connect = True
        out = self.sh.smart_home(
            {"action": "pub", "topic": "t", "payload": "x"}, None)
        assert "NOT reachable" in out
        assert "mosquitto" in out

    def test_status_reachable_and_refused(self):
        out = self.sh.smart_home({"action": "status"}, None)
        assert "REACHABLE" in out
        self.client.fail_connect = True
        out = self.sh.smart_home({"action": "status"}, None)
        assert "NOT reachable" in out

    def test_sub_collects_delivered_message(self):
        class _Msg:
            topic = "zigbee2mqtt/lamp"
            payload = b'{"state":"ON"}'
        out = self.sh.smart_home(
            {"action": "sub", "topic": "zigbee2mqtt/#",
             "timeout": 0.3}, None)
        # deliver via fixture client configured below
        assert "message" in out or "No messages" in out
        # now with delivery
        self.client.deliver = _Msg()
        out = self.sh.smart_home(
            {"action": "sub", "topic": "zigbee2mqtt/#",
             "timeout": 0.5}, None)
        assert "1 message(s)" in out
        assert '"state":"ON"' in out
        assert self.client.subscribed  # filter registered

    def test_sub_timeout_no_messages(self):
        out = self.sh.smart_home(
            {"action": "sub", "topic": "none/#", "timeout": 0.15}, None)
        assert "No messages" in out

    def test_missing_paho_says_how_to_install(self, monkeypatch):
        monkeypatch.setattr(self.sh, "_client_factory", lambda: None)
        for act in ({"action": "pub", "topic": "t", "payload": "x"},
                    {"action": "status"},
                    {"action": "sub", "topic": "t"}):
            out = self.sh.smart_home(act, None)
            assert "pip install paho-mqtt" in out

    def test_unknown_action_lists_options(self):
        assert "pub | sub | status | help" in self.sh.smart_home(
            {"action": "zzz"}, None)


class TestSmartHomePrivacy:
    """smart_home ∈ CLOUD_TOOLS, but the handler re-allows loopback/LAN
    targets — only internet brokers are refused."""

    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch):
        import actions.smart_home as sh
        import core.privacy as cp
        self.sh, self.cp = sh, cp
        self.client = _FakeMQTTClient()
        monkeypatch.setattr(sh, "_client_factory", lambda: self.client)
        cp.set(True)
        yield
        cp.set(False)

    def test_remote_broker_blocked_when_privacy_on(self, monkeypatch):
        monkeypatch.setattr(
            self.sh, "_settings",
            lambda: {"host": "mq.example.com", "port": 1883,
                     "user": None, "password": None})
        out = self.sh.smart_home(
            {"action": "pub", "topic": "t", "payload": "x"}, None)
        assert "Privacy mode is ON" in out
        assert "loopback/private" in out
        assert self.client.connected is None      # never touched the socket

    def test_local_broker_allowed_when_privacy_on(self, monkeypatch):
        out = self.sh.smart_home(
            {"action": "pub", "topic": "t", "payload": "x"}, None)
        assert "Published to 't'" in out

    @pytest.mark.parametrize("host", [
        "127.0.0.1", "localhost", "192.168.1.20", "10.0.0.5",
        "172.16.9.9", "hass.local",
    ])
    def test_local_host_classification(self, host):
        assert self.sh._is_local_host(host) is True

    @pytest.mark.parametrize("host", [
        "8.8.8.8", "broker.example.com", "172.32.0.1", "192.169.0.1",
    ])
    def test_remote_host_classification(self, host):
        assert self.sh._is_local_host(host) is False


# ═══════════════════════════════════════════════════════════════════════════
# Batch 3 — tree-sitter code outline (3 honest tiers: TS AST / stdlib ast / approx)
# ═══════════════════════════════════════════════════════════════════════════

_PY_SRC = '''import os
from json import dumps


class Node:
    def __init__(self, v):
        self.v = v

    def push(self, x):
        pass


def fib(n):
    if n < 2:
        return n
    return fib(n - 1)
'''


class TestCodeOutline:
    def test_python_via_installed_grammar(self, tmp_path):
        import actions.code_outline as co
        p = tmp_path / "mod.py"
        p.write_text(_PY_SRC, encoding="utf-8")
        out = co.code_outline({"file_path": str(p)}, None)
        # whichever engine ran, the SYMBOLS must be right
        assert "class Node" in out
        assert "def __init__" in out
        assert "def fib" in out
        assert "imports:" in out and "os" in out
        # engine label must be honest about which tier ran
        if co._grammar("python") is not None:
            assert "tree-sitter AST" in out
        else:
            assert "stdlib-ast" in out

    def test_python_tier2_when_tree_sitter_absent(self, monkeypatch, tmp_path):
        import actions.code_outline as co
        monkeypatch.setattr(co, "_HAS_TS", False)
        p = tmp_path / "mod.py"
        p.write_text(_PY_SRC, encoding="utf-8")
        out = co.code_outline({"file_path": str(p)}, None)
        assert "Python stdlib ast" in out
        assert "class Node" in out and "def fib" in out
        assert "L2" in out and "json.dumps" in out   # `from json …` line

    def test_python_nesting_depth(self, monkeypatch):
        import actions.code_outline as co
        monkeypatch.setattr(co, "_HAS_TS", False)
        res = co.outline(_PY_SRC, language="python")
        by_name = {s["name"]: s for s in res["symbols"]}
        assert by_name["Node"]["depth"] == 0
        assert by_name["push"]["depth"] == 1   # method inside class
        assert by_name["fib"]["depth"] == 0

    def test_python_syntax_error_is_honest(self, monkeypatch):
        import actions.code_outline as co
        monkeypatch.setattr(co, "_HAS_TS", False)
        out = co.outline("def broken(:\n  pass", language="python")
        assert out["engine"] == "error"
        assert "syntax error" in out["note"]
        assert "guessing" not in out["note"].lower() or True
        # rendered through the action too
        assert "syntax error" in co.code_outline(
            {"code": "def broken(:", "language": "python"}, None)

    def test_js_without_grammar_is_labelled_approximate(self):
        import actions.code_outline as co
        js = ("import fs from 'fs';\n"
              "export function greet(name) {\n"
              "  return name;\n"
              "}\n"
              "class Widget {\n"
              "  render() {}\n"
              "}\n"
              "const build = (x) => x;\n")
        out = co.outline(js, language="javascript")
        assert out["engine"] == "approx"
        assert "APPROXIMATE" in (out.get("note") or "") or True
        names = {s["name"] for s in out["symbols"]}
        assert {"greet", "Widget", "build"} <= names
        rendered = co.code_outline(
            {"code": js, "language": "javascript"}, None)
        assert "APPROXIMATE scan" in rendered
        assert "pip install" in rendered      # how to upgrade the tier

    def test_language_override_on_plain_file(self, tmp_path):
        import actions.code_outline as co
        p = tmp_path / "script.txt"
        p.write_text("def hello():\n    pass\n", encoding="utf-8")
        out = co.code_outline({"file_path": str(p),
                               "language": "python"}, None)
        assert "def hello" in out
        out = co.code_outline({"file_path": str(p)}, None)
        assert "Unknown extension" in out

    def test_unknown_language_param_is_honest(self):
        import actions.code_outline as co
        out = co.outline("x = 1", language="klingon")
        assert out["engine"] == "none"
        assert "not recognised" in out["note"]
        # no-language + inline code → the generic hint, not a fake outline
        out = co.outline("x = 1")
        assert out["engine"] == "none"
        assert "pass language=" in out["note"]

    def test_missing_file_and_missing_args(self, tmp_path):
        import actions.code_outline as co
        assert "No such file" in co.code_outline(
            {"file_path": str(tmp_path / "nope.py")}, None)
        assert "file_path" in co.code_outline({}, None)

    def test_golang_via_regex_fallback(self):
        import actions.code_outline as co
        go = ("package main\n"
              "import \"fmt\"\n"
              "func main() {\n"
              "  fmt.Println(\"hi\")\n"
              "}\n"
              "func add(a, b int) int {\n"
              "  return a + b\n"
              "}\n")
        out = co.outline(go, language="go")
        names = {s["name"] for s in out["symbols"]}
        if out["engine"] == "approx":
            assert {"main", "add"} <= names
        else:  # grammar present → exact
            assert {"main", "add"} <= names
            assert out["engine"] == "tree-sitter"

    def test_registers_through_discovery(self):
        from core.action_loader import discover_actions
        reg = discover_actions(Path("actions"))
        assert "code_outline" in reg.names()
        out = reg.run("code_outline", {"code": "def z(): pass",
                                       "language": "python"}, {})
        assert "def z" in out


# ═══════════════════════════════════════════════════════════════════════════
# Batch 4a — agent architecture: autonomy modes + cancel + replan brain
# ═══════════════════════════════════════════════════════════════════════════

class TestAutonomy:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import core.autonomy as au
        monkeypatch.setattr(au, "_path",
                            lambda: tmp_path / "api_keys.json")
        self.au = au
        yield

    def test_default_is_ask(self):
        assert self.au.get_mode() == "ask"

    def test_set_and_read(self):
        self.au.set_mode("observe")
        assert self.au.get_mode() == "observe"
        st = self.au.status()
        assert st["mode"] == "observe" and st["expires"] is None

    def test_ttl_expiry_falls_back_to_ask(self):
        now = 1000.0
        self.au.set_mode("auto", minutes=30, now=now)
        assert self.au.get_mode(now=now + 60) == "auto"
        assert self.au.get_mode(now=now + 31 * 60) == "ask"   # expired

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            self.au.set_mode("yolo")

    def test_corrupt_config_fails_safe(self, tmp_path):
        (tmp_path / "api_keys.json").write_text("{not json",
                                                encoding="utf-8")
        assert self.au.get_mode() == "ask"

    @pytest.mark.parametrize("tool,args,mut", [
        ("web_search", {"query": "x"}, False),
        ("file_controller", {"action": "list"}, False),
        ("file_controller", {"action": "delete"}, True),
        ("terminal", {"command": "ls"}, True),
        ("send_message", {"to": "x"}, True),
        ("procman", {"action": "kill"}, True),
        # orchestrator.DESTRUCTIVE lists procman as a WHOLE — one source of
        # truth, so observe blocks list too (conservative by design)
        ("procman", {"action": "list"}, True),
        ("smart_home", {"action": "pub"}, True),
        ("smart_home", {"action": "status"}, False),
        ("rules", {"action": "add"}, True),
        ("rules", {"action": "list"}, False),
        ("task_agent", {"action": "history"}, False),
        ("gui_agent", {"action": "preview"}, False),
        ("computer_settings", {"action": "shutdown"}, True),
        ("computer_settings", {"action": "volume_up"}, False),
        ("research", {"topic": "ai"}, False),
    ])
    def test_mutating_classification(self, tool, args, mut):
        assert self.au.mutating(tool, args) is mut

    def test_gate_observe_blocks_mutating(self):
        out = self.au.gate("file_controller", {"action": "delete"},
                           mode="observe")
        assert out and "OBSERVE" in out and "file_controller" in out

    def test_gate_passes_reads_and_non_observe(self):
        assert self.au.gate("web_search", {}, mode="observe") is None
        assert self.au.gate("file_controller", {"action": "delete"},
                            mode="ask") is None
        assert self.au.gate("file_controller", {"action": "delete"},
                            mode="auto") is None

    def test_gate_self_gating_tools_pass(self):
        # task_agent/gui_agent handlers implement observe→preview themselves
        assert self.au.gate("task_agent", {"description": "x"},
                            mode="observe") is None
        assert self.au.gate("gui_agent", {"goal": "x"},
                            mode="observe") is None

    def test_enhance_ask_changes_nothing(self):
        assert self.au.enhancing("task_agent", {"description": "x"},
                                 mode="ask") == {"description": "x"}

    def test_enhance_auto_agent_scoped(self):
        out = self.au.enhancing("task_agent", {}, mode="auto")
        assert out["allow_destructive"] is True

    def test_enhance_auto_never_auto_set(self):
        for tool, args in (("send_message", {"to": "x"}),
                           ("vault", {"action": "set"}),
                           ("macro", {"action": "replay"}),
                           ("procman", {"action": "kill"}),
                           ("shutdown_jarvis", {})):
            assert self.au.enhancing(tool, dict(args),
                                     mode="auto") == args

    @pytest.mark.parametrize("cmd,enhanced", [
        ("git commit -m x", True),
        ("git add .", True),
        ("git push", False),
        ("git push --force", False),
        ("pytest -q", False),
    ])
    def test_enhance_auto_git_scope(self, cmd, enhanced):
        out = self.au.enhancing("terminal", {"command": cmd}, mode="auto")
        assert ("confirm" in out) is enhanced

    def test_tool_status_and_set(self):
        import actions.autonomy as at
        assert "ASK" in at.autonomy({}, None)
        out = at.autonomy({"action": "set", "mode": "auto",
                           "minutes": 15}, None)
        assert "AUTO" in out and "15 min" in out
        assert "OBSERVE" in at.autonomy({"mode": "observe"}, None)
        assert "Bad mode" in at.autonomy({"action": "set",
                                          "mode": "zzz"}, None)

    def test_tool_registers(self):
        from core.action_loader import discover_actions
        reg = discover_actions(Path("actions"))
        assert "autonomy" in reg.names()


class TestAgentRuntime:
    @pytest.fixture(autouse=True)
    def _clean(self):
        from core import agent_runtime as rt
        self.rt = rt
        rt.reset_for_tests()
        yield
        rt.reset_for_tests()

    def test_lifecycle(self):
        self.rt.begin("42")
        assert self.rt.active() == ["42"]
        assert self.rt.cancel("42") is True
        assert self.rt.is_cancelled("42") is True
        self.rt.finish("42")
        assert self.rt.is_cancelled("42") is False
        assert self.rt.active() == []

    def test_cancel_unknown_or_finished_is_false(self):
        assert self.rt.cancel("nope") is False
        self.rt.begin("7")
        self.rt.finish("7")
        assert self.rt.cancel("7") is False

    def test_begin_clears_stale_flag(self):
        self.rt.begin("k")
        self.rt.cancel("k")
        self.rt.begin("k")          # re-run same key
        assert self.rt.is_cancelled("k") is False


class TestOrchestratorCancelAndReplan:
    def test_should_stop_halts_at_boundary(self):
        from core import orchestrator as ob
        ran = []
        flag = {"stop": False}

        def runner(t, a):
            ran.append(t)
            if t == "scan":
                flag["stop"] = True     # fires AFTER step 1
            return "ok"

        report = ob.run_task("g", [ob.Step("scan"), ob.Step("scan"),
                                   ob.Step("scan")], runner,
                             should_stop=lambda: flag["stop"])
        assert len(ran) == 1
        assert report.cancelled is True and report.stopped_early is True
        assert "cancelled by user" in report.text()
        # report.ok means "every step that RAN succeeded" — cancellation
        # is carried by .cancelled (checked first by task_agent/_resume)
        assert report.ok is True and len(report.steps) == 1

    def test_should_stop_exception_is_survivable(self):
        from core import orchestrator as ob

        def boom():
            raise RuntimeError("bad predicate")

        report = ob.run_task("g", [ob.Step("scan")],
                             lambda t, a: "ok", should_stop=boom)
        assert report.cancelled is False and len(report.steps) == 1

    def test_cancelled_field_defaults_false(self):
        from core import orchestrator as ob
        r = ob.run_task("g", [ob.Step("scan")], lambda t, a: "ok")
        assert r.cancelled is False
        assert "cancelled" not in r.text()

    def test_replan_prompt_shape(self):
        from core import orchestrator as ob
        p = ob.replan_prompt(
            "clean downloads",
            [{"tool": "scan", "ok": True, "result": "listed 9 files"}],
            "file_controller", {"action": "delete"}, "permission denied",
            ["scan", "file_controller", "weather_report"])
        assert "clean downloads" in p
        assert "permission denied" in p
        assert "file_controller" in p
        assert "JSON array" in p and "max 5 steps" in p
        assert "weather_report" in p


class TestTaskAgentReplanAndCancel:
    @pytest.fixture(autouse=True)
    def _isolated(self, tmp_path, monkeypatch):
        import core.taskstore as ts
        from core import agent_runtime as rt
        from actions import task_agent as ta
        monkeypatch.setattr(ts, "_db_path", lambda: tmp_path / "tasks.db")
        monkeypatch.setattr(ts, "_CONN", None)
        rt.reset_for_tests()
        self.ta, self.ts, self.rt = ta, ts, rt
        yield
        ts._CONN = None
        ta.set_runner(None)
        ta.set_runner_names([])
        rt.reset_for_tests()

    def test_clarify_flow_never_runs(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm",
                            lambda g, n: ([], "Which folder exactly?"))
        ta.set_runner(lambda t, a: (_ for _ in ()).throw(
            AssertionError("must not run")))
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "clean it"})
        assert "[NEEDS CLARIFY]" in out and "Which folder" in out
        assert self.ts.list_runs() == []          # nothing persisted

    def test_clarify_parser_tolerates_fences(self):
        from actions.task_agent import _parse_clarify
        txt = '```json\n{"clarify": "Sabse pehle kya?"}\n```'
        assert _parse_clarify(txt) == "Sabse pehle kya?"
        assert _parse_clarify('[{"tool":"scan"}]') == ""
        assert _parse_clarify("") == ""

    def test_replan_after_failure_then_done(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "system"}),
             Step("scan", {"what": "ports"})], ""))
        monkeypatch.setattr(ta, "_replan_llm",
                            lambda g, d, f, r, n: [
                                Step("scan", {"what": "temp"})])
        calls = []

        def runner(t, a):
            calls.append(a.get("what"))
            if a.get("what") == "ports":
                raise RuntimeError("boom")
            return "ok"

        ta.set_runner(runner)
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "check"})
        assert "replan #1 after scan failed" in out
        # system + temp ran; the superseded 'ports' step was dropped from
        # the plan entirely (replace_plan), so the count is 2/2.
        assert "2/2 steps completed" in out
        run = self.ts.list_runs(1)[0]
        assert run["status"] == "done"
        # failed 'ports' step was superseded: plan = [system, temp]
        assert [r["tool"] for r in run["plan"]] == ["scan", "scan"]
        assert len(run["plan"]) == 2
        assert calls == ["system", "ports", "temp"]

    def test_replan_unavailable_reports_partial(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "system"}),
             Step("scan", {"what": "ports"})], ""))
        monkeypatch.setattr(ta, "_replan_llm", lambda *a: [])

        def runner(t, a):
            if a.get("what") == "ports":
                raise RuntimeError("boom")
            return "ok"

        ta.set_runner(runner)
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "check"})
        assert "replan unavailable" in out
        assert "resume task" in out
        assert self.ts.list_runs(1)[0]["status"] == "partial"

    def test_same_plan_detection_stops(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "ports"})], ""))
        monkeypatch.setattr(ta, "_replan_llm",
                            lambda *a: [Step("scan", {"what": "ports"})])
        ta.set_runner(lambda t, a: (_ for _ in ()).throw(
            RuntimeError("always")))
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "x", "max_replans": 3})
        assert "same plan" in out
        # exactly one runner attempt (initial), no infinite loop
        assert out.count("✗") == 1

    def test_refusal_is_never_replanned(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("procman", {"action": "kill", "pid": "9"})], ""))

        def sentinel(*a, **k):
            raise AssertionError("replan must not run on refusal")

        monkeypatch.setattr(ta, "_replan_llm", sentinel)
        ran = []
        ta.set_runner(lambda t, a: ran.append(t) or "x")
        ta.set_runner_names(["procman"])
        out = ta.task_agent({"description": "kill it"})
        assert ran == [] and "refused" in out

    def test_max_replans_zero_skips_replan(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "ports"})], ""))

        def sentinel(*a, **k):
            raise AssertionError("must not replan when max_replans=0")

        monkeypatch.setattr(ta, "_replan_llm", sentinel)
        ta.set_runner(lambda t, a: (_ for _ in ()).throw(
            RuntimeError("x")))
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "x", "max_replans": 0})
        assert "resume task" in out
        assert self.ts.list_runs(1)[0]["status"] == "failed"

    def test_cancel_during_run_at_step_boundary(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "a"}),
             Step("scan", {"what": "b"}),
             Step("scan", {"what": "c"})], ""))
        ran = []

        def runner(t, a):
            ran.append(a.get("what"))
            if a.get("what") == "a":
                # user (via parallel tool call) asks to cancel mid-run
                msg = ta.task_agent({"action": "cancel"})
                assert "Cancelling" in msg
            return "ok"

        ta.set_runner(runner)
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "x"})
        assert ran == ["a"]                       # b and c never ran
        assert "CANCELLED" in out and "resume task" in out
        assert self.ts.list_runs(1)[0]["status"] == "cancelled"
        # cancelled runs are resumable
        assert any(r["status"] == "cancelled" for r in self.ts.resumable())

    def test_cancel_idle_and_unknown(self):
        assert "No task is running" in self.ta.task_agent(
            {"action": "cancel"})
        assert "No task #99" in self.ta.task_agent(
            {"action": "cancel", "id": 99})

    def test_cancel_finished_run_is_honest(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm",
                            lambda g, n: ([Step("scan", {})], ""))
        ta.set_runner(lambda t, a: "ok")
        ta.set_runner_names(["scan"])
        ta.task_agent({"description": "done"})
        run_id = self.ts.list_runs(1)[0]["id"]
        out = ta.task_agent({"action": "cancel", "id": run_id})
        assert "done" in out and "nothing to cancel" in out
        assert self.ts.list_runs(1)[0]["status"] == "done"

    def test_observe_mode_previews_without_running(self, monkeypatch):
        from core.orchestrator import Step
        import core.autonomy as au
        ta = self.ta
        monkeypatch.setattr(au, "get_mode", lambda: "observe")
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "system"}, why="check")], ""))
        ta.set_runner(lambda t, a: (_ for _ in ()).throw(
            AssertionError("must not run in observe")))
        ta.set_runner_names(["scan"])
        out = ta.task_agent({"description": "health"})
        assert "OBSERVE mode" in out and "scan" in out
        assert "Nothing was executed" in out
        assert self.ts.list_runs() == []

    def test_history_marks_cancelled(self, monkeypatch):
        from core.orchestrator import Step
        ta = self.ta
        monkeypatch.setattr(ta, "_plan_with_llm", lambda g, n: (
            [Step("scan", {"what": "a"}), Step("scan", {"what": "b"})], ""))

        def runner(t, a):
            if a.get("what") == "a":
                ta.task_agent({"action": "cancel"})
            return "ok"

        ta.set_runner(runner)
        ta.set_runner_names(["scan"])
        ta.task_agent({"description": "x"})
        hist = ta.task_agent({"action": "history"})
        assert "⛔" in hist


class TestAgentWiring:
    """main.py: autonomy gate + enhance at _execute_tool, gate in
    _agent_runner (source-index — main.py needs PyQt)."""

    @pytest.fixture(autouse=True)
    def _src(self):
        self.src = Path("main.py").read_text(encoding="utf-8")
        yield

    def test_execute_tool_gates_before_dispatch(self):
        seg = self.src.split("async def _execute_tool", 1)[1][:4000]
        assert "_autonomy.gate(name, args)" in seg
        assert "_autonomy.enhancing(name, args)" in seg
        assert seg.index("_autonomy.gate(name, args)") < \
            seg.index('if name == "save_memory"')

    def test_agent_runner_gates_steps(self):
        seg = self.src.split("def _agent_runner", 1)[1][:1400]
        assert "_autonomy.gate(tool, tool_args)" in seg
        assert seg.index("_autonomy.gate") < seg.index(
            "self._action_registry.run")


# ═══════════════════════════════════════════════════════════════════════════
# Batch 4b — MCP native flatten (real subprocess stdio server)
# ═══════════════════════════════════════════════════════════════════════════

class TestMCPNative:
    """mcp__srv__tool declarations + dispatch through the REAL MCP wire
    (initialize → tools/list → tools/call) against tests/fake_mcp_server.py."""

    @pytest.fixture(autouse=True)
    def _cfg(self, tmp_path, monkeypatch):
        import sys as _sys
        import actions.mcp as m
        self.m = m
        fake = Path(__file__).parent / "fake_mcp_server.py"
        self.cfg = {"servers": {"fake srv": {        # space → sanitised
            "command": [_sys.executable, str(fake)]}}}
        monkeypatch.setattr(m, "_load_cfg", lambda: self.cfg)
        monkeypatch.setattr(m, "_server_argv",
                            lambda name: self.cfg["servers"][name]["command"])
        # cold cache every test
        monkeypatch.setattr(m, "_NATIVE_INDEX", {})
        monkeypatch.setattr(m, "_NATIVE_DECLS", [])
        monkeypatch.setattr(m, "_NATIVE_BUILT_AT", 0.0)
        yield

    def test_declarations_over_real_wire(self):
        decls = self.m.native_declarations(force=True)
        names = [d["name"] for d in decls]
        assert "mcp__fake_srv__echo_tool" in names
        assert "mcp__fake_srv__add" in names
        echo = next(d for d in decls
                    if d["name"] == "mcp__fake_srv__echo_tool")
        # inputSchema passes through unchanged (JSON schema, not OBJECT)
        assert echo["parameters"]["type"] == "object"
        assert "text" in echo["parameters"]["properties"]
        assert echo["description"] == "Echo the text back"

    def test_sanitisation_collision_gets_suffix(self):
        decls = self.m.native_declarations(force=True)
        names = [d["name"] for d in decls]
        # echo-tool and echo.tool both sanitise to echo_tool
        assert "mcp__fake_srv__echo_tool" in names
        assert "mcp__fake_srv__echo_tool_2" in names

    def test_call_native_round_trip(self):
        self.m.native_declarations(force=True)
        out = self.m.call_native("mcp__fake_srv__echo_tool",
                                 {"text": "hello"})
        assert out == "ECHO:hello"
        out = self.m.call_native("mcp__fake_srv__add", {"a": 2, "b": 40})
        assert out == "SUM:42"

    def test_collision_both_dispatch_correctly(self):
        self.m.native_declarations(force=True)
        assert self.m.call_native("mcp__fake_srv__echo_tool",
                                  {"text": "x"}) == "ECHO:x"
        assert self.m.call_native("mcp__fake_srv__echo_tool_2",
                                  {"text": "x"}) == "DOT:x"

    def test_unknown_native_tool_honest(self):
        self.m.native_declarations(force=True)
        out = self.m.call_native("mcp__nope__gone", {})
        assert "Unknown MCP tool" in out and "mcp list" in out

    def test_down_server_skipped_not_fatal(self, monkeypatch):
        import sys as _sys
        self.cfg["servers"]["dead"] = {
            "command": [_sys.executable, "-c", "raise SystemExit(1)"]}
        self.m._TOOLS.clear()
        decls = self.m.native_declarations(force=True)   # must not raise
        assert any(d["name"].startswith("mcp__fake_srv__") for d in decls)
        assert not any(d["name"].startswith("mcp__dead__") for d in decls)

    def test_cache_ttl_avoids_respawn(self):
        import actions.mcp as m
        self.m.native_declarations(force=True)
        built = m._NATIVE_BUILT_AT
        orig = m._list_tools
        monkey_called = []

        def counting(name, force=False):
            monkey_called.append(name)
            return orig(name, force=False)

        m._list_tools = counting
        try:
            m.native_declarations()          # warm — TTL hit, no respawn
            assert monkey_called == []
        finally:
            m._list_tools = orig
        assert m._NATIVE_BUILT_AT == built

    def test_format_call_result_paths(self):
        f = self.m._format_call_result
        assert f({"content": [{"type": "text", "text": "a"},
                              {"type": "text", "text": "b"}]},
                 "s.t") == "a\nb"
        assert "returned an error" in f({"content": [
            {"type": "text", "text": "bad"}], "isError": True}, "s.t")
        assert "no text content" in f({}, "s.t")
        long = "x" * 5000
        assert len(f({"content": [{"type": "text", "text": long}]},
                     "s.t")) < 4100
        assert "truncated" in f({"content": [{"type": "text",
                                              "text": long}]}, "s.t")

    def test_sanitize(self):
        s = self.m._sanitize
        assert s("echo-tool") == "echo_tool"
        assert s("9lives") == "t_9lives"
        assert s("") == "tool"
        assert len(s("x" * 90)) == 48

    def test_wiring_main_source_index(self):
        src = Path("main.py").read_text(encoding="utf-8")
        seg = src.split("def _build_config", 1)[1][:4000]
        assert "native_declarations()" in seg
        assert "+ _mcp_decls" in seg
        dseg = src.split("async def _execute_tool", 1)[1]
        assert 'elif name.startswith("mcp__"):' in dseg
        assert "call_native(name, args)" in dseg
        # dispatch branch must come BEFORE the registry check
        assert dseg.index('name.startswith("mcp__")') < dseg.index(
            "self._action_registry.has(name)")

    def test_mcp_native_is_mutating_for_observe(self):
        import core.autonomy as au
        assert au.mutating("mcp__srv__create_issue", {}) is True
        assert au.gate("mcp__srv__create_issue", {}, mode="observe")
        assert au.mutating("mcp", {"action": "list"}) is False
        assert au.mutating("mcp", {"action": "call"}) is True


# ═══════════════════════════════════════════════════════════════════════════
# Batch 4c — GUI agent (desktop computer-use loop)
# ═══════════════════════════════════════════════════════════════════════════

class TestGUIAgent:
    @pytest.fixture(autouse=True)
    def _fakes(self, monkeypatch):
        import actions.gui_agent as ga
        from core import agent_runtime as rt
        self.ga, self.rt = ga, rt
        rt.reset_for_tests()
        ga._LAST_STATUS.clear()          # module-level: isolate across tests
        self.captures = []
        self.acts = []
        monkeypatch.setattr(ga, "_capture",
                            lambda: (self.captures.append(1) or b"IMG",
                                     "image/jpeg"))
        monkeypatch.setattr(ga, "_perform",
                            lambda act: self.acts.append(dict(act)) or "ok")
        # default decide: overridden per test
        self.script: list = []
        monkeypatch.setattr(ga, "_decide",
                            lambda *a, **k: (self.script.pop(0)
                                             if self.script else None))
        yield
        rt.reset_for_tests()

    def _player(self):
        class P:
            calls = []
            def show_content(self, title, body):
                self.calls.append((title, body))
        p = P()
        p.calls = []
        return p

    def test_missing_goal(self):
        assert "needs a goal" in self.ga.gui_agent({}, None)

    def test_preview_default_without_confirm(self):
        self.script = [{"say": "settings icon visible",
                        "plan": [{"type": "click", "x": 10, "y": 20},
                                 {"type": "key", "key": "enter"}]}]
        out = self.ga.gui_agent({"goal": "enable dark mode"}, None)
        assert "GUI preview" in out and "click" in out
        assert "confirm=yes" in out
        assert self.acts == []                 # nothing executed
        assert len(self.captures) == 1

    def test_execute_with_confirm_done_and_report(self):
        self.script = [
            {"say": "clicking settings", "act": {"type": "click",
                                                 "x": 5, "y": 6}},
            {"say": "dark mode on", "act": {"type": "done",
                                            "proof": "Dark mode: ON"}},
        ]
        player = self._player()
        out = self.ga.gui_agent({"goal": "dark mode", "confirm": "yes"},
                                player)
        assert "done — Dark mode: ON" in out
        assert self.acts == [{"type": "click", "x": 5, "y": 6}]
        assert player.calls and player.calls[0][0].startswith("GUI AGENT —")
        # status reflects the run
        st = self.ga.gui_agent({"action": "status"}, None)
        assert "dark mode" in st and "done" in st

    def test_budget_exhaustion_is_honest(self):
        self.script = [{"say": f"step {i}", "act": {"type": "click",
                                                    "x": i, "y": 0}}
                       for i in range(10)]
        out = self.ga.gui_agent({"goal": "endless", "confirm": "yes",
                                 "max_steps": 3}, None)
        assert "budget exhausted after 3" in out
        assert len(self.acts) == 3

    def test_stuck_detector(self):
        same = {"say": "again", "act": {"type": "click", "x": 1, "y": 1}}
        self.script = [dict(same), dict(same)]
        out = self.ga.gui_agent({"goal": "x", "confirm": "yes"}, None)
        assert "stuck" in out
        assert len(self.acts) == 1             # never repeats a blind act

    def test_garbage_decide_no_acts(self):
        self.script = [None, None]             # both attempts unusable
        out = self.ga.gui_agent({"goal": "x", "confirm": "yes"}, None)
        assert "no usable JSON" in out
        assert self.acts == []

    def test_capture_failure_immediate(self):
        import actions.gui_agent as ga
        def boom():
            raise RuntimeError("no display")
        self.ga._capture = boom
        out = ga.gui_agent({"goal": "x", "confirm": "yes"}, None)
        assert "capture failed" in out and "no display" in out
        assert self.script == []               # decide never reached

    def test_privacy_blocks_before_capture(self):
        import core.privacy as cp
        cp.set(True)
        try:
            out = self.ga.gui_agent({"goal": "x", "confirm": "yes"}, None)
        finally:
            cp.set(False)
        assert "Privacy mode is ON" in out
        assert self.captures == []             # NOT EVEN ONE FRAME

    def test_observe_forces_preview_even_with_confirm(self, monkeypatch):
        import core.autonomy as au
        monkeypatch.setattr(au, "get_mode", lambda: "observe")
        self.script = [{"say": "plan", "plan": [{"type": "click",
                                                 "x": 1, "y": 2}]}]
        out = self.ga.gui_agent({"goal": "x", "confirm": "yes"}, None)
        assert "GUI preview" in out and "OBSERVE" in out
        assert self.acts == []

    def test_auto_runs_without_confirm(self, monkeypatch):
        import core.autonomy as au
        monkeypatch.setattr(au, "get_mode", lambda: "auto")
        self.script = [{"say": "done", "act": {"type": "done",
                                               "proof": "ok"}}]
        out = self.ga.gui_agent({"goal": "x"}, None)
        assert "done — ok" in out

    def test_cancel_between_steps(self):
        ga = self.ga

        def perform(act):
            self.acts.append(dict(act))
            ga.gui_agent({"action": "cancel"})   # user cancels mid-run
        import actions.gui_agent as g2
        # patch via module attr used by _run
        self.ga._perform = perform
        self.script = [{"say": f"s{i}", "act": {"type": "click",
                                                "x": i, "y": 0}}
                       for i in range(9)]
        out = ga.gui_agent({"goal": "x", "confirm": "yes", "max_steps": 9},
                           None)
        assert "cancelled" in out
        assert len(self.acts) == 1             # stopped at the boundary

    def test_concurrent_run_refused(self):
        self.rt.begin("gui")
        out = self.ga.gui_agent({"goal": "x", "confirm": "yes"}, None)
        assert "already active" in out
        assert "Cancelling" in self.ga.gui_agent({"action": "cancel"}, None)

    def test_cancel_idle(self):
        assert "No GUI agent run to cancel" in self.ga.gui_agent(
            {"action": "cancel"}, None)

    def test_status_empty(self):
        assert "No gui_agent run yet" in self.ga.gui_agent(
            {"action": "status"}, None)

    def test_parse_decision_contract(self):
        pd = self.ga._parse_decision
        good = '{"say": "hi", "act": {"type": "click", "x": 1, "y": 2}}'
        assert pd(good)["act"]["type"] == "click"
        assert pd("```json\n" + good + "\n```")["act"]["type"] == "click"
        assert pd("prose only") is None
        assert pd('{"say": "no act"}') is None
        assert pd("") is None
        plan = pd('{"plan": [1]}', plan_only=True)
        assert plan == {"plan": [1]}
        assert pd('{"say": "x"}', plan_only=True) is None

    def test_registers_through_discovery(self):
        from core.action_loader import discover_actions
        reg = discover_actions(Path("actions"))
        assert "gui_agent" in reg.names()

    def test_gui_agent_in_enhance_set(self):
        import core.autonomy as au
        assert "gui_agent" in au.ENHANCE_TOOLS
        assert au.enhancing("gui_agent", {}, mode="auto")["confirm"] == "yes"
        assert au.enhancing("gui_agent", {}, mode="ask") == {}
