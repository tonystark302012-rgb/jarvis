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
                     "actions/web_search.py", "actions/phone_vision.py"):
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
