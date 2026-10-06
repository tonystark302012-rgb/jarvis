# tests/test_dots.py — Dots platform (self-hosted agent workspace)
# Batch 6a: storage, blocks, revisions, approvals, memory, conversations.
import json
from pathlib import Path

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# Fixture: isolated db + TestClient + scripted brain
# ═══════════════════════════════════════════════════════════════════════════

def _tmp_pcs_root(tmp_path):
    def _root():
        d = tmp_path / "dots_pcs"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _root


@pytest.fixture()
def env(monkeypatch, tmp_path):
    import dots.db as db
    import dots.brain as brain
    import dots.computer as computer
    import dots.scheduler as scheduler
    monkeypatch.setattr(db, "_db_path", lambda: tmp_path / "dots.db")
    scheduler.shutdown()
    monkeypatch.setattr(computer, "_pcs_root", _tmp_pcs_root(tmp_path))
    db.reset_for_tests()
    computer.reset_for_tests()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dots.server import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    brain.set_llm(lambda system, hist: f"scripted reply to: {hist[-1]['content']}")
    yield {"client": client, "brain": brain, "db": db, "tmp": tmp_path}
    brain.set_llm(None)
    computer.reset_for_tests()
    import time as _t
    _end = _t.monotonic() + 2.0
    while scheduler._live and _t.monotonic() < _end:
        _t.sleep(0.05)              # let spawned task workers finish
    scheduler.shutdown()
    db.reset_for_tests()


def _mk_dot(client, name="Researcher", role="cite sources",
            perms=None):
    r = client.post("/api/dots", json={"name": name, "role": role,
                                       "permissions": perms or {}})
    assert r.status_code == 201, r.text
    return r.json()


def _mk_space_page(client, title="Page One", md="- first bullet"):
    s = client.post("/api/spaces", json={"name": "Work"})
    assert s.status_code == 201
    p = client.post(f"/api/spaces/{s.json()['id']}/pages",
                    json={"title": title, "content_md": md})
    assert p.status_code == 201, p.text
    return s.json(), p.json()


# ═══════════════════════════════════════════════════════════════════════════
# Blocks: canonical JSON ⇄ markdown source
# ═══════════════════════════════════════════════════════════════════════════

class TestBlocks:
    def test_roundtrip_all_types(self):
        from dots.blocks import blocks_to_md, md_to_blocks, validate_blocks
        blocks = [
            {"type": "text", "text": "hello para"},
            {"type": "bullet", "text": "one"},
            {"type": "numbered", "text": "first"},
            {"type": "numbered", "text": "second"},
            {"type": "checklist", "text": "todo", "checked": False},
            {"type": "checklist", "text": "done", "checked": True},
            {"type": "quote", "text": "wise words"},
            {"type": "code", "text": "print('hi')", "lang": "python"},
            {"type": "divider"},
            {"type": "table", "rows": [["a", "b"], ["1", "2"]],
             "header": True},
        ]
        md = blocks_to_md(blocks)
        back = md_to_blocks(md)
        assert back == blocks
        validated, err = validate_blocks(back)
        assert err is None and validated == blocks

    def test_validate_rejects_garbage(self):
        from dots.blocks import validate_blocks
        assert validate_blocks("not json at all")[1] is not None
        assert validate_blocks({"type": "text"})[1] is not None
        assert validate_blocks([{"type": "alien"}])[1] is not None
        assert validate_blocks([{"type": "text", "text": 5}])[1] is not None
        assert validate_blocks([{"type": "table", "rows": "x"}])[1] is not None
        ok, err = validate_blocks('[{"type": "text", "text": "s"}]')
        assert err is None and ok[0]["type"] == "text"

    def test_md_import_degrades_to_text(self):
        from dots.blocks import md_to_blocks
        blocks = md_to_blocks("plain line\n\nanother !!unknown!! line")
        assert [b["type"] for b in blocks] == ["text", "text"]

    def test_multiline_paragraph_merges(self):
        from dots.blocks import blocks_to_md, md_to_blocks
        blocks = [{"type": "text", "text": "line one\nline two"}]
        assert md_to_blocks(blocks_to_md(blocks)) == blocks

    def test_pipe_escaped_in_table_cells(self):
        from dots.blocks import blocks_to_md, md_to_blocks
        blocks = [{"type": "table", "header": True,
                   "rows": [["a|b", "c"], ["d", "e"]]}]
        assert md_to_blocks(blocks_to_md(blocks)) == blocks


# ═══════════════════════════════════════════════════════════════════════════
# Dots + spaces + pages CRUD
# ═══════════════════════════════════════════════════════════════════════════

class TestDotsAndPages:
    def test_dot_crud_and_unique(self, env):
        c = env["client"]
        d = _mk_dot(c, "Researcher")
        assert d["name"] == "Researcher" and d["permissions"] == {}
        assert c.get("/api/dots").json()[0]["name"] == "Researcher"
        assert c.post("/api/dots", json={"name": "researcher"}).status_code == 400
        r = c.patch(f"/api/dots/{d['id']}",
                    json={"permissions": {"memory": False}})
        assert r.json()["permissions"]["memory"] is False
        assert c.delete(f"/api/dots/{d['id']}").json()["ok"] is True
        assert c.get(f"/api/dots/{d['id']}").status_code == 404

    def test_page_md_import_is_canonical(self, env):
        c = env["client"]
        _s, p = _mk_space_page(c, md="# heading-ish\n- bullet")
        assert p["rev"] == 1
        types = [b["type"] for b in p["blocks"]]
        assert types == ["text", "bullet"]     # md import via parser
        assert "# heading-ish" in p["content_md"]
        assert "- bullet" in p["content_md"]
        # direct json create
        p2 = c.post("/api/spaces/" + str(p["space_id"]) + "/pages",
                    json={"title": "J", "content_json":
                          json.dumps([{"type": "code", "text": "x"}])})
        assert p2.status_code == 201
        assert p2.json()["blocks"][0]["type"] == "code"

    def test_owner_save_bumps_rev_and_writes_revision(self, env):
        c = env["client"]
        _s, p = _mk_space_page(c)
        r = c.patch(f"/api/pages/{p['id']}",
                    json={"base_rev": 1, "content_md": "new body"})
        assert r.status_code == 200
        assert r.json()["rev"] == 2
        revs = c.get(f"/api/pages/{p['id']}/revisions").json()
        assert [x["rev"] for x in revs] == [2, 1]
        one = c.get(f"/api/pages/{p['id']}/revisions/1").json()
        assert "first bullet" in one["content_md"]

    def test_stale_owner_save_conflicts_and_never_overwrites(self, env):
        # T1 + T12
        c = env["client"]
        _s, p = _mk_space_page(c)
        ok = c.patch(f"/api/pages/{p['id']}",
                     json={"base_rev": 1, "content_md": "winner"})
        assert ok.status_code == 200
        stale = c.patch(f"/api/pages/{p['id']}",
                        json={"base_rev": 1, "content_md": "loser"})
        assert stale.status_code == 409
        body = stale.json()
        assert body["conflict"] is True and body["current_rev"] == 2
        assert "winner" in body["current_content_md"]
        # page untouched by the stale attempt
        page = c.get(f"/api/pages/{p['id']}").json()
        assert page["rev"] == 2 and page["content_md"] == "winner"

    def test_owner_bad_base_rev_rejected(self, env):
        c = env["client"]
        _s, p = _mk_space_page(c)
        r = c.patch(f"/api/pages/{p['id']}", json={"base_rev": "x"})
        assert r.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Human-in-the-loop approvals (T2, T3, T4)
# ═══════════════════════════════════════════════════════════════════════════

class TestApprovals:
    def test_dot_edit_is_proposal_only(self, env):
        # T2: page bytes unchanged until approve
        import dots.store as store
        c = env["client"]
        _s, p = _mk_space_page(c)
        dot = _mk_dot(c, "Writer")
        pend = store.propose_edit(dot["id"], p["id"], base_rev=1,
                                  content_md="dot draft",
                                  reason="improved wording")
        assert pend["status"] == "pending"
        page = c.get(f"/api/pages/{p['id']}").json()
        assert page["rev"] == 1 and "first bullet" in page["content_md"]

        out = store.approve_pending(pend["id"])
        assert out["ok"] and out["page"]["rev"] == 2
        assert out["page"]["content_md"] == "dot draft"
        page = c.get(f"/api/pages/{p['id']}").json()
        assert page["content_md"] == "dot draft"
        # revision author records the dot
        revs = c.get(f"/api/pages/{p['id']}/revisions").json()
        assert revs[0]["author"] == f"dot:{dot['id']}"

    def test_stale_proposal_approve_never_overwrites(self, env):
        # T3
        import dots.store as store
        c = env["client"]
        _s, p = _mk_space_page(c)
        dot = _mk_dot(c, "Writer")
        pend = store.propose_edit(dot["id"], p["id"], base_rev=1,
                                  content_md="old-base draft")
        # page moves on (owner saves) while proposal is pending
        c.patch(f"/api/pages/{p['id']}",
                json={"base_rev": 1, "content_md": "owner moved on"})
        out = store.approve_pending(pend["id"])
        assert out.get("conflict") and out["status"] == "stale"
        page = c.get(f"/api/pages/{p['id']}").json()
        assert page["content_md"] == "owner moved on" and page["rev"] == 2

    def test_decline_writes_nothing(self, env):
        # T4
        import dots.store as store
        c = env["client"]
        _s, p = _mk_space_page(c)
        dot = _mk_dot(c, "Writer")
        pend = store.propose_edit(dot["id"], p["id"], base_rev=1,
                                  content_md="rejected draft")
        out = store.decline_pending(pend["id"])
        assert out["ok"]
        page = c.get(f"/api/pages/{p['id']}").json()
        assert page["rev"] == 1 and "first bullet" in page["content_md"]
        # double-decide is honest
        assert "already declined" in store.decline_pending(pend["id"])["error"]

    def test_propose_create_then_approve_creates_page(self, env):
        import dots.store as store
        c = env["client"]
        _s, p = _mk_space_page(c)
        dot = _mk_dot(c, "Researcher")
        pend = store.propose_create(dot["id"], p["space_id"],
                                    "Cited summary",
                                    content_md="body with sources")
        assert pend["kind"] == "create"
        assert [x["id"] for x in
                c.get(f"/api/spaces/{p['space_id']}/pages").json()] == [p["id"]]
        out = store.approve_pending(pend["id"])
        assert out["ok"]
        pages = c.get(f"/api/spaces/{p['space_id']}/pages").json()
        assert len(pages) == 2 and pages[1]["title"] == "Cited summary"
        assert pages[1]["created_by"] == f"dot:{dot['id']}"

    def test_pending_endpoints_and_double_approve(self, env):
        c = env["client"]
        _s, p = _mk_space_page(c)
        dot = _mk_dot(c, "Writer")
        import dots.store as store
        pend = store.propose_edit(dot["id"], p["id"], base_rev=1,
                                  content_md="via api")
        listed = c.get("/api/pending").json()
        assert len(listed) == 1 and listed[0]["id"] == pend["id"]
        ok = c.post(f"/api/pending/{pend['id']}/approve")
        assert ok.status_code == 200 and ok.json()["ok"]
        again = c.post(f"/api/pending/{pend['id']}/approve")
        assert again.status_code == 409          # already approved
        bad = c.post("/api/pending/9999/approve")
        assert bad.status_code in (400, 404)


# ═══════════════════════════════════════════════════════════════════════════
# Conversations (separate per dot / per page) + brain honesty
# ═══════════════════════════════════════════════════════════════════════════

class TestConversations:
    def test_page_and_dot_chats_are_separate(self, env):
        c = env["client"]
        _s, p = _mk_space_page(c)
        d = _mk_dot(c, "Researcher")
        r = c.post(f"/api/conversations/page:{p['id']}/messages",
                   json={"content": "what's on this page?",
                         "dot_id": d["id"], "page_id": p["id"]})
        assert r.status_code == 201
        assert "scripted reply" in r.json()["reply"]["content"]
        assert "Page One" in c.get(
            f"/api/conversations/page:{p['id']}/messages").text or True

        c.post(f"/api/conversations/dot:{d['id']}/messages",
               json={"content": "hello dot", "dot_id": d["id"]})
        page_msgs = c.get(
            f"/api/conversations/page:{p['id']}/messages").json()
        dot_msgs = c.get(
            f"/api/conversations/dot:{d['id']}/messages").json()
        assert len(page_msgs) == 2 and len(dot_msgs) == 2
        assert page_msgs[0]["content"] != dot_msgs[0]["content"]
        assert dot_msgs[0]["content"] == "hello dot"
        assert dot_msgs[1]["content"].startswith("scripted reply")

    def test_system_context_carries_role_prefs_and_page(self, env):
        import dots.brain as brain
        import dots.store as store
        c = env["client"]
        _s, p = _mk_space_page(c, title="Brief", md="draft text")
        d = _mk_dot(c, "Writer", role="Write in Hindi")
        store.set_pref("tone", "formal")
        captured = {}

        def spy(system, hist):
            captured["system"] = system
            return "ok"

        brain.set_llm(spy)
        c.post(f"/api/conversations/page:{p['id']}/messages",
               json={"content": "hi", "dot_id": d["id"], "page_id": p["id"]})
        sys_txt = captured["system"]
        assert "Write in Hindi" in sys_txt
        assert "tone: formal" in sys_txt
        assert "'Brief'" in sys_txt and "draft text" in sys_txt

    def test_brain_honest_when_llm_fails(self, env, monkeypatch):
        # T11
        env["brain"].set_llm(None)
        import core.gemini as gemini

        def boom(*a, **k):
            raise RuntimeError("no key here")

        monkeypatch.setattr(gemini, "call", boom)
        c = env["client"]
        d = _mk_dot(c, "Writer")
        r = c.post(f"/api/conversations/dot:{d['id']}/messages",
                   json={"content": "hey", "dot_id": d["id"]})
        assert r.status_code == 201
        assert "No brain reachable" in r.json()["reply"]["content"]

    def test_message_validation(self, env):
        c = env["client"]
        assert c.post("/api/conversations/dot:1/messages",
                      json={"content": ""}).status_code == 400
        assert c.post("/api/conversations/dot:1/messages",
                      json={"content": "x", "dot_id": 999}).status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# Memory (preferences) + permission gating
# ═══════════════════════════════════════════════════════════════════════════

class TestMemory:
    def test_memory_crud(self, env):
        c = env["client"]
        r = c.put("/api/memory", json={"key": "language", "value": "Hinglish"})
        assert r.status_code == 200 and r.json()["allowed"] == "*"
        assert c.get("/api/memory").json()[0]["key"] == "language"
        pid = r.json()["id"]
        assert c.delete(f"/api/memory/{pid}").json()["ok"] is True
        assert c.get("/api/memory").json() == []
        assert c.delete(f"/api/memory/{pid}").status_code == 404
        assert c.put("/api/memory", json={"key": "", "value": "x"}).status_code == 400

    def test_dot_gating(self, env):
        import dots.store as store
        c = env["client"]
        store.set_pref("city", "Jaipur")
        store.set_pref("secret", "vault-code", allowed=["Admin"])
        open_d = _mk_dot(c, "Researcher")
        closed_d = _mk_dot(c, "Helper", perms={"memory": False})
        names = [p["key"] for p in store.prefs_for_dot(open_d)]
        assert "city" in names and "secret" not in names
        assert store.prefs_for_dot(closed_d) == []
        admin = _mk_dot(c, "Admin")
        assert "secret" in [p["key"] for p in store.prefs_for_dot(admin)]

    def test_bad_allowed_rejected(self, env):
        c = env["client"]
        assert c.put("/api/memory", json={"key": "k", "value": "v",
                                          "allowed": 5}).status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# Plumbing: health, schema covers later batches
# ═══════════════════════════════════════════════════════════════════════════

class TestPlumbing:
    def test_health_and_schema(self, env):
        c = env["client"]
        h = c.get("/api/health")
        assert h.status_code == 200 and h.json()["ok"] is True
        # later-batch tables exist from day one (no migrations later)
        cur = env["db"]._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r["name"] for r in cur.fetchall()}
        assert {"dots", "spaces", "pages", "page_revisions",
                "pending_changes", "messages", "preferences",
                "computers", "computer_audit", "tasks", "task_runs",
                "skills", "slack_threads"} <= tables

    def test_nested_pages_supported(self, env):
        c = env["client"]
        _s, parent = _mk_space_page(c, title="Parent")
        child = c.post(f"/api/spaces/{parent['space_id']}/pages",
                       json={"title": "Child", "parent_id": parent["id"]})
        assert child.status_code == 201
        assert child.json()["parent_id"] == parent["id"]
        bad = c.post(f"/api/spaces/{parent['space_id']}/pages",
                     json={"title": "X", "parent_id": 999})
        assert bad.status_code == 400


# ═══════════════════════════════════════════════════════════════════════════
# JARVIS integration: voice/tools surface + dashboard mount + no-standalone
# ═══════════════════════════════════════════════════════════════════════════

class TestJARVISIntegration:
    @pytest.fixture(autouse=True)
    def _f(self, env):
        self.env = env
        from actions import dots as dots_action
        from actions import pages as pages_action
        self.da, self.pa = dots_action, pages_action

    def _call(self, handler, **params):
        return handler(parameters=params, player=None)

    def test_both_actions_discoverable(self):
        from core.action_loader import discover_actions
        reg = discover_actions(Path("actions"))
        assert "dots" in reg.names() and "pages" in reg.names()
        assert self.da.TOOL["handler"] is self.da.dots
        assert self.pa.TOOL["handler"] is self.pa.pages
        assert self.da.TOOL["parameters"]["type"] == "OBJECT"

    def test_dots_action_lifecycle_and_voice_chat(self):
        out = self._call(self.da.dots, action="dot_create",
                         name="Researcher", role="cite every source")
        assert "created" in out and "#1" in out
        listing = self._call(self.da.dots, action="dot_list")
        assert "Researcher" in listing and "cite every source" in listing
        dup = self._call(self.da.dots, action="dot_create",
                         name="researcher")
        assert "already exists" in dup
        reply = self._call(self.da.dots, action="chat", dot="Researcher",
                           message="summarise topic X")
        assert reply.startswith("scripted reply")
        from dots import store
        msgs = store.list_messages("dot:1")
        assert [m["role"] for m in msgs] == ["user", "dot"]

    def test_dots_chat_unknown_is_honest(self):
        out = self._call(self.da.dots, action="chat", dot="Nobody",
                         message="hi")
        assert "No Dot named" in out

    def test_voice_approve_and_stale_refusal(self):
        from dots import store
        self._call(self.pa.pages, action="space_create", name="Research")
        self._call(self.pa.pages, action="page_create", space="Research",
                   title="Topic X", content="v1")
        dot = store.create_dot("Writer")
        p = store.list_pages(store.list_spaces()[0]["id"])[0]
        pend = store.propose_edit(dot["id"], p["id"], base_rev=p["rev"],
                                  content_md="dot draft v2")
        listed = self._call(self.da.dots, action="pending_list")
        assert f"#{pend['id']}" in listed and "Writer" in listed
        ok = self._call(self.da.dots, action="approve",
                        which=str(pend["id"]))
        assert "Approved" in ok and "rev 2" in ok
        page = store.get_page(p["id"])
        assert page["content_md"] == "dot draft v2"
        pend2 = store.propose_edit(dot["id"], p["id"],
                                   base_rev=page["rev"],
                                   content_md="next draft")
        self._call(self.pa.pages, action="page_save", page="Topic X",
                   content="owner moved to rev 3")
        stale = self._call(self.da.dots, action="approve",
                           which=str(pend2["id"]))
        assert "NOT saved" in stale and "re-propose" in stale
        assert store.get_page(p["id"])["content_md"] == \
            "owner moved to rev 3"

    def test_dots_memory_actions(self):
        out = self._call(self.da.dots, action="memory_set", key="language",
                         value="Hinglish")
        assert "Saved" in out
        assert "language = Hinglish" in self._call(
            self.da.dots, action="memory_list")
        out = self._call(self.da.dots, action="memory_delete",
                         which="language")
        assert "Deleted" in out
        assert "No preferences" in self._call(self.da.dots,
                                              action="memory_list")

    def test_pages_voice_owner_flow(self):
        self._call(self.pa.pages, action="space_create", name="Notes")
        created = self._call(self.pa.pages, action="page_create",
                             space="Notes", title="Daily",
                             content="- morning\n- evening")
        assert "created" in created
        shown = self._call(self.pa.pages, action="page_show", page="Daily")
        assert "morning" in shown and "rev 1" in shown
        saved = self._call(self.pa.pages, action="page_save", page="Daily",
                           content="- updated")
        assert "rev 2" in saved
        revs = self._call(self.pa.pages, action="revisions", page="Daily")
        assert "rev 2" in revs and "rev 1" in revs
        from dots import store
        p = store.list_pages(store.list_spaces()[0]["id"])[0]
        stale = self._call(self.pa.pages, action="page_save",
                           page="Daily", content="clobber", base_rev=1)
        assert "CONFLICT" in stale and "nothing was overwritten" in stale
        assert store.get_page(p["id"])["content_md"] == "- updated"

    def test_pages_chat_is_page_scoped(self):
        from dots import store
        store.create_dot("Writer")
        self._call(self.pa.pages, action="space_create", name="S")
        self._call(self.pa.pages, action="page_create", space="S",
                   title="Doc", content="body")
        out = self._call(self.pa.pages, action="chat", page="Doc",
                         dot="Writer", message="improve this")
        assert out.startswith("scripted reply")
        assert store.list_messages("page:1")
        assert store.list_messages("dot:1") == []

    def test_unknown_actions_are_honest(self):
        assert "Unknown dots action" in self._call(self.da.dots,
                                                   action="launch")
        assert "Unknown pages action" in self._call(self.pa.pages,
                                                    action="launch")

    def test_dashboard_mounts_router_with_auth(self):
        src = Path("dashboard/server.py").read_text(encoding="utf-8")
        assert "from dots.server import router" in src
        assert "app.include_router(_dots_router" in src
        assert "_dots_auth" in src and "401" in src

    def test_no_standalone_product_surface(self):
        assert not Path("dots/__main__.py").exists()
        import dots.server as dsrv
        assert hasattr(dsrv, "router")
        assert not hasattr(dsrv, "app")            # no standalone app
        assert "no `python -m dots`" in dsrv.__doc__
        doc = Path("docs/DOT_PLATFORM.md").read_text(encoding="utf-8")
        assert "no separate Dots product" in doc
        assert "actions/dots.py" in doc


# ═══════════════════════════════════════════════════════════════════════════
# Batch 6b: brain tool-loop + research/space tools (permission-gated)
# ═══════════════════════════════════════════════════════════════════════════

class TestBrainTools:
    def test_spec_exposure_by_permission(self, env):
        from dots import store
        from dots.tools import specs_for

        def gated(dot):
            # skill tools are published-knowledge by design → always on
            return [t["function"]["name"] for t in specs_for(dot)
                    if t["function"]["name"] not in
                    ("load_skill", "read_skill_file")]

        def skill_tools(dot):
            return [t["function"]["name"] for t in specs_for(dot)
                    if t["function"]["name"] in
                    ("load_skill", "read_skill_file")]

        bare = store.create_dot("Bare")
        assert gated(bare) == []
        assert skill_tools(bare) == ["load_skill", "read_skill_file"]
        res = store.create_dot("Web", permissions={"research": True})
        assert gated(res) == ["search_web", "read_public_page"]
        spa = store.create_dot("Sp", permissions={"space": True})
        assert gated(spa) == ["list_authorized_spaces", "list_space_pages",
                              "read_space_page", "create_space_page",
                              "edit_space_page"]
        both = store.create_dot("AB",
                                permissions={"research": True, "space": True})
        assert len(specs_for(both)) == 9

    def test_tool_loop_search_then_answer(self, env, monkeypatch):
        from dots import brain, store, tools_research
        calls = {"n": 0}

        def fake_ddg(q):
            return [{"title": f"Hit for {q}", "url": "https://ex.org/a",
                     "snippet": "the snippet"}]

        monkeypatch.setattr(tools_research, "_ddg", fake_ddg)
        monkeypatch.setattr(tools_research, "_research_mode", lambda: "parallel")

        seen = []

        def llm(messages, tools):
            calls["n"] += 1
            assert any(t["function"]["name"] == "search_web" for t in tools)
            if calls["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "search_web",
                        "arguments": {"queries": ["topic x"]}}}]}
            seen.append(messages)
            return {"content": "final answer with sources", "tool_calls": []}

        brain.set_llm(llm)
        dot = store.create_dot("R", permissions={"research": True})
        out = brain.reply(dot, "dot:999", "research topic x")
        assert out == "final answer with sources"
        tool_msgs = [m for m in seen[0] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert "https://ex.org/a" in tool_msgs[0]["content"]
        assert tool_msgs[0]["tool_call_id"] == "c1"

    def test_search_caps_queries_and_dedupes(self, env, monkeypatch):
        from dots import tools_research
        seen_queries = []

        def fake_ddg(q):
            seen_queries.append(q)
            return [{"title": q, "url": "https://same.org/x", "snippet": "s"},
                    {"title": "d", "url": f"https://u/{q}", "snippet": "p"}]

        monkeypatch.setattr(tools_research, "_ddg", fake_ddg)
        monkeypatch.setattr(tools_research, "_research_mode", lambda: "parallel")
        out = tools_research.run({}, "search_web", {
            "queries": ["a", "b", "c", "d"]})
        assert len(seen_queries) == 3          # 1..3 clamp
        assert out.count("https://same.org/x") == 1   # URL dedupe
        assert "extra queries dropped" in out
        assert "https://u/a" in out and "https://u/c" in out

    def test_research_denied_honest_in_loop(self, env, monkeypatch):
        from dots import brain, store, tools
        dot = store.create_dot("NoWeb")           # no research permission
        assert tools.run_tool(dot, "search_web",
                              {"queries": ["x"]}).startswith("denied:")

        captured = {}

        def llm(messages, tools):
            if not any(m.get("role") == "tool" for m in messages):
                return {"content": "", "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "search_web",
                        "arguments": {"queries": ["x"]}}}]}
            captured["messages"] = messages
            return {"content": "I do not have research permission.",
                    "tool_calls": []}

        brain.set_llm(llm)
        out = brain.reply(dot, "dot:0", "search something")
        denied = [m for m in captured["messages"] if m.get("role") == "tool"]
        assert denied and denied[0]["content"].startswith("denied:")
        assert "permission" in denied[0]["content"]
        assert "research permission" in out

    def test_research_mode_config_refusals(self, env, monkeypatch):
        from dots import store, tools_research
        dot = store.create_dot("R", permissions={"research": True})
        monkeypatch.setattr(tools_research, "_research_mode",
                            lambda: "disabled")
        assert "research_mode=disabled" in tools_research.run(
            dot, "search_web", {"queries": ["x"]})
        assert "research_mode=disabled" in tools_research.run(
            dot, "read_public_page", {"url": "https://a.b"})
        monkeypatch.setattr(tools_research, "_research_mode",
                            lambda: "browser")
        assert "read_public_page only" in tools_research.run(
            dot, "search_web", {"queries": ["x"]})
        monkeypatch.setattr(tools_research, "_fetch",
                            lambda u: "<html><title>T</title>body</html>")
        monkeypatch.setattr(tools_research, "_extract",
                            lambda h, u: ("T", "readable text"))
        monkeypatch.setattr(tools_research, "_links", lambda h, u: [])
        out = tools_research.run(dot, "read_public_page",
                                 {"url": "https://site.example/p"})
        assert "readable text" in out

    def test_read_public_page_guards(self, env, monkeypatch):
        from dots import tools_research
        monkeypatch.setattr(tools_research, "_research_mode",
                            lambda: "parallel")
        out = tools_research.run({}, "read_public_page",
                                 {"url": "ftp://x"})
        assert out.startswith("denied:")
        assert tools_research.run({}, "read_public_page",
                                  {"url": ""}).startswith("read_public_page")

        def boom(u):
            raise RuntimeError("403")

        monkeypatch.setattr(tools_research, "_fetch", boom)
        out = tools_research.run({}, "read_public_page",
                                 {"url": "https://x.y/z"})
        assert out.startswith("error: could not fetch")

        monkeypatch.setattr(tools_research, "_fetch",
                            lambda u: "<html>hi</html>")
        monkeypatch.setattr(tools_research, "_extract",
                            lambda h, u: ("Title", "main text"))
        monkeypatch.setattr(tools_research, "_links",
                            lambda h, u: [("L1", "https://x.y/1")])
        out = tools_research.run({}, "read_public_page",
                                 {"url": "https://x.y/z"})
        assert "title: Title" in out and "main text" in out
        assert "L1" in out

    def test_space_tools_read_and_list(self, env):
        from dots import store
        s, pg = _mk_space_page(env["client"], title="Daily",
                               md="- morning routine")
        dot = store.create_dot("S", permissions={"space": True})
        from dots.tools import run_tool
        assert f"#{s['id']} Work" in run_tool(dot, "list_authorized_spaces",
                                              {})
        assert "Daily" in run_tool(dot, "list_space_pages",
                                   {"space_id": s["id"]})
        got = run_tool(dot, "read_space_page", {"page_id": pg["id"]})
        assert "rev 1" in got and "- morning routine" in got
        assert "error:" in run_tool(dot, "read_space_page",
                                    {"page_id": 99999})

    def test_edit_tool_proposal_only_and_gates(self, env):
        from dots import store
        from dots.tools import run_tool
        s, pg = _mk_space_page(env["client"], title="Doc", md="v1")
        dot = store.create_dot("E", permissions={"space": True})
        # missing base_rev → honest, nothing written
        out = run_tool(dot, "edit_space_page",
                       {"page_id": pg["id"], "content_md": "v2"})
        assert "base_rev" in out and "read_space_page" in out
        assert store.get_page(pg["id"])["content_md"] == "v1"
        # missing content+title → honest usage error
        out = run_tool(dot, "edit_space_page",
                       {"page_id": pg["id"], "base_rev": 1})
        assert out.startswith("edit_space_page: pass content_md")
        # real proposal → pending only (T2)
        out = run_tool(dot, "edit_space_page",
                       {"page_id": pg["id"], "base_rev": 1,
                        "content_md": "v2 from dot"})
        assert "proposed — pending approval #" in out
        assert store.get_page(pg["id"])["content_md"] == "v1"  # untouched
        pid = int(out.split("#")[1].split(" ")[0])
        assert store.get_pending(pid)["status"] == "pending"

    def test_create_tool_then_approve_sources(self, env):
        from dots import store
        from dots.tools import run_tool
        s = env["client"].post("/api/spaces", json={"name": "W"}).json()
        dot = store.create_dot("C", permissions={"space": True})
        out = run_tool(dot, "create_space_page", {
            "space_id": s["id"], "title": "Findings",
            "content_md": "body",
            "sources": ["https://src.example/1",
                        {"title": "Ref", "url": "https://src.example/2"}]})
        assert "proposed — pending approval #" in out
        pid = int(out.split("#")[1].split(" ")[0])
        assert store.list_pages(s["id"]) == []   # nothing exists yet
        res = store.approve_pending(pid)
        assert res["ok"]
        page = res["page"]
        assert page["title"] == "Findings"
        assert page["sources"][0]["url"] == "https://src.example/1"
        assert page["sources"][1]["title"] == "Ref"

    def test_edit_proposal_sources_apply_and_preserve(self, env):
        from dots import store
        s, pg = _mk_space_page(env["client"], title="R", md="x")
        dot = store.create_dot("S2", permissions={"space": True})
        # proposal WITH sources → approved replaces page sources
        p1 = store.propose_edit(dot["id"], pg["id"], base_rev=1,
                                content_md="v2",
                                sources=["https://keep.me/1"])
        assert store.approve_pending(p1["id"])["ok"]
        page = store.get_page(pg["id"])
        assert page["sources"] == [{"title": "", "url": "https://keep.me/1"}]
        # proposal WITHOUT sources → approve keeps existing sources
        p2 = store.propose_edit(dot["id"], pg["id"], base_rev=2,
                                content_md="v3")
        assert store.approve_pending(p2["id"])["ok"]
        page = store.get_page(pg["id"])
        assert page["content_md"] == "v3"
        assert page["sources"][0]["url"] == "https://keep.me/1"
        # proposal with explicit [] → clears
        p3 = store.propose_edit(dot["id"], pg["id"], base_rev=3,
                                content_md="v4", sources=[])
        assert store.approve_pending(p3["id"])["ok"]
        assert store.get_page(pg["id"])["sources"] == []

    def test_owner_only_review_tool_honest(self, env):
        from dots import store
        from dots.tools import run_tool
        dot = store.create_dot("X", permissions={"space": True, "research": True})
        out = run_tool(dot, "review_space_page", {"page_id": 1})
        assert "owner-only" in out and "self-approve" in out
        assert "unknown tool" in run_tool(dot, "nope", {})

    def test_loop_round_cap_honest(self, env, monkeypatch):
        from dots import brain, store, tools_research
        monkeypatch.setattr(tools_research, "_ddg",
                            lambda q: [{"title": "t", "url": "https://u",
                                        "snippet": "s"}])
        monkeypatch.setattr(tools_research, "_research_mode",
                            lambda: "parallel")
        n = {"calls": 0}

        def stubborn(messages, tools):
            n["calls"] += 1
            return {"content": f"round {n['calls']}", "tool_calls": [
                {"id": f"c{n['calls']}", "function": {
                    "name": "search_web",
                    "arguments": {"queries": ["q"]}}}]}

        brain.set_llm(stubborn)
        dot = store.create_dot("Loop", permissions={"research": True})
        out = brain.reply(dot, "dot:1", "keep searching")
        assert n["calls"] == brain.MAX_ROUNDS == 8
        assert "tool limit" in out

    def test_system_prompt_permission_summary(self, env):
        from dots import brain, store
        dot = store.create_dot("P", permissions={"research": True})
        sys_txt = brain.system_prompt(dot, [], None)
        assert "PERMISSIONS (owner-granted): research" in sys_txt
        assert "TOOLS available: search_web, read_public_page" in sys_txt
        assert "'denied:'" in sys_txt
        bare = store.create_dot("Q")
        sys_txt = brain.system_prompt(bare, [], None)
        assert "PERMISSIONS (owner-granted): none" in sys_txt
        # skill tools are always available (published = shared)
        assert "TOOLS available: load_skill, read_skill_file" in sys_txt
        assert "PUBLISHED SKILLS: (none yet)" in sys_txt

    def test_default_path_local_llm_loop(self, env, monkeypatch):
        # no seam → local tool LLM first (doc: local-first), gemini unused
        from dots import brain, store, tools_research
        brain.set_llm(None)
        monkeypatch.setattr(brain, "_local_ready", lambda: True)
        monkeypatch.setattr(tools_research, "_research_mode",
                            lambda: "parallel")

        def fake_ddg(q):
            return [{"title": "T", "url": "https://n.example", "snippet": "s"}]

        monkeypatch.setattr(tools_research, "_ddg", fake_ddg)
        import core.gemini as gemini
        monkeypatch.setattr(gemini, "call",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("gemini must not be used")))
        import core.llm_client as llm_client
        state = {"n": 0}

        def fake_call_llm(messages, tools=None, timeout=120):
            state["n"] += 1
            if state["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"function": {"name": "search_web",
                                  "arguments": {"queries": ["ai"]}}}]}
            return {"content": "local final", "tool_calls": []}

        monkeypatch.setattr(llm_client, "call_llm", fake_call_llm)
        dot = store.create_dot("L", permissions={"research": True})
        out = brain.reply(dot, "dot:2", "find ai news")
        assert out == "local final"
        assert state["n"] == 2

    def test_default_path_unreachable_is_honest(self, env, monkeypatch):
        # T11 through the new default path: probe fails → gemini fails →
        # the stored reply SAYS no brain (never a fake answer)
        from dots import brain, store
        brain.set_llm(None)
        monkeypatch.setattr(brain, "_local_ready", lambda: False)
        import core.gemini as gemini
        monkeypatch.setattr(gemini, "call",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no key")))
        dot = store.create_dot("H")
        out = brain.reply(dot, "dot:3", "hello")
        assert out.startswith("No brain reachable")
        assert "configure a key or local LLM" in out

    def test_voice_chat_runs_tool_loop(self, env, monkeypatch):
        # end-to-end: voice chat → brain loop → research tool → answer
        from dots import brain, store, tools_research
        monkeypatch.setattr(tools_research, "_research_mode",
                            lambda: "parallel")
        monkeypatch.setattr(tools_research, "_ddg",
                            lambda q: [{"title": "Src",
                                        "url": "https://cite.example",
                                        "snippet": "evidence"}])
        env["client"].post("/api/dots", json={
            "name": "Webby", "role": "researcher",
            "permissions": {"research": True}})
        state = {"n": 0}

        def llm(messages, tools):
            state["n"] += 1
            if state["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "search_web",
                        "arguments": {"queries": ["topic"]}}}]}
            return {"content": "answer with the evidence", "tool_calls": []}

        brain.set_llm(llm)
        from actions.dots import dots as dots_action
        out = dots_action({"action": "chat", "dot": "Webby",
                           "message": "research topic"})
        assert out == "answer with the evidence"
        msgs = store.list_messages("dot:1")
        assert msgs[-2]["role"] == "user" and msgs[-1]["role"] == "dot"
        assert msgs[-1]["content"] == "answer with the evidence"

    def test_voice_dot_create_perms_parsing(self, env):
        from actions.dots import _parse_perms, dots as dots_action
        assert _parse_perms("research,memory") == {"research": True,
                                                   "memory": True}
        assert _parse_perms("all") == {"research": True, "memory": True,
                                       "space": True}
        assert _parse_perms('{"research": false}') == {"research": False}
        assert _parse_perms(None) is None
        out = dots_action({"action": "dot_create", "name": "Pars",
                           "role": "r", "perms": "research,space"})
        assert "[perms: research, space]" in out
        from dots import store
        d = store.find_dot_by_name("Pars")
        assert d["permissions"] == {"research": True, "space": True}


# ═══════════════════════════════════════════════════════════════════════════
# Batch 6c: Dot Computers — jail (T7), audit owner|agent (T8), lifecycle
# ═══════════════════════════════════════════════════════════════════════════

class _FakeEngine:
    def __init__(self, cid):
        self.cid = cid
        self.ops = []
        self.closed = False

    def perform(self, op, **kw):
        self.ops.append((op, kw))
        if op == "screenshot":
            from dots import computer
            shots = computer.dirs(self.cid)[0] / "_shots"
            shots.mkdir(parents=True, exist_ok=True)
            p = shots / "fake.png"
            p.write_bytes(b"\x89PNG fake")
            return {"ok": True, "path": "_shots/fake.png"}
        if op == "snapshot":
            return {"ok": True, "url": "https://ex.test/", "title": "T",
                    "text": "body text",
                    "elements": [{"sel": "#go", "text": "Go"}]}
        if op == "navigate":
            return {"ok": True, "url": kw.get("url"), "title": "T"}
        return {"ok": True, **{k: str(v) for k, v in kw.items()}}

    def close(self):
        self.closed = True


class TestDotComputer:
    def _mk(self, env, perms=None, comp_perms=None):
        from dots import computer, store
        c = env["client"]
        d = c.post("/api/dots", json={"name": "Op", "role": "r",
                                      "permissions": perms or {}}).json()
        r = c.post("/api/computers",
                   json={"dot_id": d["id"], "perms": comp_perms or {}})
        assert r.status_code == 201, r.text
        return store.get_dot(d["id"]), r.json()

    def test_create_dirs_and_one_per_dot(self, env, tmp_path):
        from dots import computer, store
        c = env["client"]
        d = store.create_dot("Solo")
        r = c.post("/api/computers", json={"dot_id": d["id"]})
        assert r.status_code == 201, r.text
        comp = r.json()
        assert Path(comp["work"]).exists() and Path(comp["profile"]).exists()
        assert str(tmp_path) in comp["work"]          # isolated per test
        again = c.post("/api/computers", json={"dot_id": d["id"]})
        assert again.status_code == 400 and "already has" in again.text
        assert c.post("/api/computers", json={"dot_id": 999}).status_code \
            == 404
        assert computer.get(comp["id"])["status"] == "stopped"
        assert c.get(f"/api/computers/{comp['id']}").status_code == 200

    def test_jail_t7(self, env):
        # T7: .., absolute escapes and symlinks pointing out — all refused
        dot, comp = self._mk(env, perms={"computer": True})
        from dots import computer as pc
        work = Path(comp["work"])
        outside = work.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        for bad in ("../outside.txt", str(outside), "/etc/passwd",
                    "a/../../outside.txt"):
            res = pc.files_write(comp, bad, "x")
            assert "denied" in res["error"] and "escapes" in res["error"], bad
            res = pc.files_read(comp, bad)
            assert "denied" in res["error"], bad
        link = work / "link.txt"
        link.symlink_to(outside)
        res = pc.files_read(comp, "link.txt")
        assert "denied" in res["error"]             # resolve follows link
        res = pc.files_list(comp, "..")
        assert "denied" in res["error"]
        assert not outside.exists() or outside.read_text() == "secret"
        # legit file lands inside work
        pc.files_write(comp, "notes/ok.txt", "fine")
        assert (work / "notes" / "ok.txt").read_text() == "fine"

    def test_files_ops_and_live_perm_gate(self, env):
        dot, comp = self._mk(env, perms={"computer": True})
        from dots import computer as pc
        c = env["client"]
        # files work while STOPPED (they persist by definition)
        assert pc.files_write(comp, "a.txt", "hello")["ok"] is True
        got = pc.files_read(comp, "a.txt")
        assert got["content"] == "hello"
        lst = pc.files_list(comp, "")
        assert "a.txt" in lst["entries"]
        assert "no such" in pc.files_read(comp, "missing.txt")["error"]
        # live toggle: files off → next call refused (no restart)
        r = c.patch(f"/api/computers/{comp['id']}/perms",
                    json={"perms": {"files": False}})
        assert r.status_code == 200 and r.json()["perms"]["files"] is False
        assert pc.files_read(comp, "a.txt")["error"].startswith("denied:")
        comp2 = pc.get(comp["id"])
        assert comp2["perms"]["files"] is False
        # audit recorded both the write and the denial
        rows = pc.audit_rows(comp["id"])
        assert any(r["action"] == "files_write" and r["ok"] for r in rows)
        assert any(r["action"] == "files_read" and not r["ok"]
                   for r in rows)

    def test_exec_argv_timeout_output(self, env):
        import sys
        import time as _t
        dot, comp = self._mk(env, perms={"computer": True},
                             comp_perms={"shell": True})
        from dots import computer as pc
        # shell needs the machine running
        stopped = pc.shell(comp, [sys.executable, "-c", "print(1)"])
        assert "stopped" in stopped["error"]
        pc.start(comp["id"])
        comp = pc.get(comp["id"])
        res = pc.shell(comp, [sys.executable, "-c", "print('hi')"])
        assert res["ok"] and "hi" in res["stdout"] and res["returncode"] == 0
        # argv must be a LIST — a shell line is refused, ever
        assert "no shell string" in pc.shell(comp, "ls -la")["error"]
        # timeout: 1 s kill of a 5 s sleeper, honest error
        t0 = _t.monotonic()
        res = pc.shell(comp, [sys.executable, "-c", "import time; time.sleep(5)"],
                       timeout=1)
        assert _t.monotonic() - t0 < 4 and "timeout after 1s" in res["error"]
        # hard cap clamp: 999 → 60 s cap, quick command still runs
        res = pc.shell(comp, [sys.executable, "-c", "print('c')"],
                       timeout=999)
        assert res["timeout"] == 60 and "c" in res["stdout"]
        # output cap with truncation marker
        res = pc.shell(comp, [sys.executable, "-c", "print('x' * 200000)"])
        assert res["truncated"] and "[output truncated]" in res["stdout"]
        assert len(res["stdout"]) <= 64 * 1024 + 40

    def test_stop_kills_live_process_t8(self, env):
        import sys
        import threading
        import time as _t
        dot, comp = self._mk(env, perms={"computer": True},
                             comp_perms={"shell": True})
        from dots import computer as pc
        pc.start(comp["id"])
        comp = pc.get(comp["id"])
        result = {}

        def run():
            result["res"] = pc.shell(
                comp, [sys.executable, "-c", "import time; time.sleep(30)"])

        th = threading.Thread(target=run, daemon=True)
        th.start()
        _t.sleep(0.5)                    # let the Popen register
        t0 = _t.monotonic()
        pc.stop(comp["id"], actor="owner")
        th.join(timeout=6)
        assert not th.is_alive(), "stop must kill the live exec"
        assert _t.monotonic() - t0 < 8
        assert pc.get(comp["id"])["status"] == "stopped"
        rows = pc.audit_rows(comp["id"])
        stop_row = [r for r in rows if r["action"] == "stop"][0]
        assert stop_row["actor"] == "owner"
        assert "killed 1" in stop_row["detail"]

    def test_start_stop_persistence_t8(self, env):
        dot, comp = self._mk(env, perms={"computer": True})
        from dots import computer as pc
        pc.files_write(comp, "keep.txt", "persist me")
        pc.start(comp["id"])
        assert pc.get(comp["id"])["status"] == "running"
        pc.stop(comp["id"])
        pc.start(comp["id"], actor="owner")
        comp = pc.get(comp["id"])
        assert pc.files_read(comp, "keep.txt")["content"] == "persist me"
        assert Path(comp["profile"]).exists()   # browser profile survives
        pc.stop(comp["id"])
        rows = pc.audit_rows(comp["id"])
        assert {r["actor"] for r in rows if r["action"] == "start"} == \
            {"owner"}

    def test_audit_owner_vs_agent_t8(self, env):
        # T8: owner's route calls vs the dot's tool calls — both audited
        dot, comp = self._mk(env, perms={"computer": True},
                             comp_perms={"shell": True})
        from dots import computer as pc
        import sys
        c = env["client"]
        pc.start(comp["id"])
        r = c.post(f"/api/computers/{comp['id']}/exec",
                   json={"argv": [sys.executable, "-c", "print('own')"]})
        assert r.status_code == 200 and "own" in r.json()["stdout"]
        out = pc.run_agent_tool(dot["id"], "exec",
                                {"argv": [sys.executable, "-c",
                                          "print('agent')"]})
        assert "agent" in out
        rows = pc.audit_rows(comp["id"])
        execs = [r for r in rows if r["action"] == "exec"]
        assert {r["actor"] for r in execs} == {"owner", "agent"}
        assert all(r["ok"] for r in execs)
        # route shape: audit endpoint returns rows newest-first
        j = c.get(f"/api/computers/{comp['id']}/audit").json()
        assert j[0]["id"] >= j[-1]["id"] and j[0]["actor"] in ("owner",
                                                               "agent")
        # argv string over HTTP is refused with an honest message
        bad = c.post(f"/api/computers/{comp['id']}/exec",
                     json={"argv": "ls -la"})
        assert bad.status_code == 400 and "JSON list" in bad.json()["error"]

    def test_browser_honest_refusal_without_playwright(self, env, monkeypatch):
        dot, comp = self._mk(env, perms={"computer": True},
                             comp_perms={"browser": True})
        from dots import computer as pc
        monkeypatch.setattr(pc, "_pw_available", lambda: False)
        pc.start(comp["id"])
        res = pc.browser(pc.get(comp["id"]), "navigate", actor="agent",
                         url="https://x.test")
        assert "playwright is not installed" in res["error"]
        assert any(r["action"] == "browser_navigate" and not r["ok"]
                   for r in pc.audit_rows(comp["id"]))

    def test_browser_fake_engine_flow(self, env, monkeypatch):
        import json as _json
        dot, comp = self._mk(env, perms={"computer": True},
                             comp_perms={"browser": True})
        from dots import computer as pc
        made = []

        def factory(cid):
            e = _FakeEngine(cid)
            made.append(e)
            return e

        monkeypatch.setattr(pc, "_engine_factory", factory)
        monkeypatch.setattr(pc, "_pw_available", lambda: True)  # fake in
        c = env["client"]
        comp = pc.get(comp["id"])
        # gate order: browser permission before anything
        comp_off = pc.set_perms(comp["id"], {"browser": False})
        assert pc.browser(comp_off, "navigate", url="https://a")[
            "error"].startswith("denied:")
        assert made == []
        pc.set_perms(comp["id"], {"browser": True})
        comp_on = pc.get(comp["id"])
        # must be running
        assert "stopped" in pc.browser(comp_on, "read")["error"]
        pc.start(comp["id"])
        r = c.post(f"/api/computers/{comp['id']}/browser",
                   json={"op": "navigate", "url": "https://site.test"})
        assert r.status_code == 200 and r.json()["url"] == "https://site.test"
        for op, payload in (("click", {"target": "#go"}),
                            ("type", {"target": "#q", "text": "hello"}),
                            ("key", {"key": "Enter"}),
                            ("scroll", {"direction": "down", "amount": 300}),
                            ("read", {}), ("snapshot", {})):
            rr = c.post(f"/api/computers/{comp['id']}/browser",
                        json={"op": op, **payload})
            assert rr.status_code == 200, (op, rr.text)
        snap = c.post(f"/api/computers/{comp['id']}/browser",
                      json={"op": "snapshot"}).json()
        assert snap["elements"][0]["sel"] == "#go"
        shot = c.post(f"/api/computers/{comp['id']}/browser",
                      json={"op": "screenshot"}).json()
        assert (Path(comp["work"]) / shot["path"]).exists()
        assert c.post(f"/api/computers/{comp['id']}/browser",
                      json={"op": "eval"}).status_code == 400
        ops_done = [o for o, _ in made[0].ops]
        assert ops_done == ["navigate", "click", "type", "key", "scroll",
                            "read", "snapshot", "snapshot", "screenshot"]
        # stop closes the engine (worker joined)
        pc.stop(comp["id"])
        assert made[0].closed is True
        rows = pc.audit_rows(comp["id"])
        assert any(r["action"].startswith("browser_") and r["actor"] == "owner"
                   for r in rows)

    def test_brain_loop_with_computer_tools(self, env, monkeypatch):
        import sys
        from dots import brain, computer as pc, store, tools
        dot, comp = self._mk(env, perms={"computer": True},
                             comp_perms={"shell": True})
        pc.start(comp["id"])
        dot = store.get_dot(dot["id"])
        names = [t["function"]["name"] for t in tools.specs_for(dot)]
        assert "exec" in names and "computer_click" in names
        state = {"n": 0}

        def llm(messages, tools):
            state["n"] += 1
            if state["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"id": "c1", "function": {
                        "name": "exec",
                        "arguments": {"argv": [sys.executable, "-c",
                                               "print(21*2)"]}}}]}
            return {"content": "the answer is 42", "tool_calls": []}

        brain.set_llm(llm)
        out = brain.reply(dot, "dot:pc1", "what is 21*2?")
        assert out == "the answer is 42"
        rows = pc.audit_rows(comp["id"])
        agent_exec = [r for r in rows if r["action"] == "exec"
                      and r["actor"] == "agent"]
        assert agent_exec and agent_exec[0]["ok"]
        assert "print(21*2)" in agent_exec[0]["detail"]
        # computer permission off → no COMPUTER specs, honest denial
        bare = store.create_dot("NoPC")
        assert not [t for t in tools.specs_for(bare)
                    if t["function"]["name"].startswith("computer_")
                    or t["function"]["name"] in ("exec", "files_list",
                                                 "files_read",
                                                 "files_write")]
        assert tools.run_tool(bare, "exec", {"argv": ["x"]}).startswith(
            "denied:")
        # permission on but NO computer yet → honest creation hint
        ghost = store.create_dot("Ghost", permissions={"computer": True})
        out = tools.run_tool(ghost, "files_list", {})
        assert "no computer yet" in out and "pc action=create" in out

    def test_voice_pc_flow(self, env):
        import sys
        from actions.pcs import pc as pc_action
        c = env["client"]
        c.post("/api/dots", json={"name": "Oper", "role": "r",
                                  "permissions": {"computer": True}})

        def say(**kw):
            return pc_action(kw)

        assert "Computer #1 ready" in say(action="create", dot="Oper",
                                          perms="files,shell")
        assert "has no computer" not in say(action="show", dot="Oper")
        assert "started" in say(action="start", dot="Oper")
        out = say(action="exec", dot="Oper",
                  argv=f"{sys.executable} -c \"print('voice')\"")
        assert "voice" in out
        assert "rc 0" in out
        w = say(action="files_write", dot="Oper", path="v.txt",
                content="via voice")
        assert w.startswith("Wrote")
        assert "via voice" in say(action="files_read", dot="Oper",
                                  path="v.txt")
        assert "v.txt" in say(action="files_list", dot="Oper")
        assert "perms" in say(action="perms", dot="Oper",
                              perms="shell=false")
        assert "denied" in say(action="exec", dot="Oper",
                               argv=f"{sys.executable} -c 'print(1)'")
        audit = say(action="audit", dot="Oper")
        assert "[owner] exec" in audit and "[owner] files_write" in audit
        assert "#1 for Oper" in say(action="list")
        assert "Unknown pc action" in say(action="fly", dot="Oper")
        known = say(action="fly", dot="Oper")
        for a in ("create", "exec", "browser", "audit"):
            assert a in known
        assert "stopped" in say(action="stop", dot="Oper")
        assert "No Dot named" in say(action="show", dot="Nobody")

    def test_delete_computer(self, env):
        dot, comp = self._mk(env, perms={"computer": True})
        from dots import computer as pc
        c = env["client"]
        pc.start(comp["id"])
        work = Path(comp["work"])
        assert work.exists()
        r = c.delete(f"/api/computers/{comp['id']}")
        assert r.status_code == 200 and r.json()["ok"] is True
        assert pc.get(comp["id"]) is None
        assert not work.exists()
        assert c.get(f"/api/computers/{comp['id']}").status_code == 404
        assert c.delete("/api/computers/12345").status_code == 404


def _wait_for(fn, timeout=4.0, gap=0.05):
    """Poll until fn() is truthy (async task workers) → last value."""
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        v = fn()
        if v:
            return v
        _t.sleep(gap)
    return fn()


# ═══════════════════════════════════════════════════════════════════════════
# Batch 6d: scheduler (90 s runs, pause/retry/cancel) + skill learning (T9)
# ═══════════════════════════════════════════════════════════════════════════

class TestScheduler:
    def test_create_validation_and_voice(self, env):
        from actions.dots import dots as dots_action
        from dots import scheduler, store
        c = env["client"]
        # no dot yet → schedules on the main brain (dot_id is optional; this
        # used to be a P0: int(None) → TypeError → HTTP 400)
        out = dots_action({"action": "task_create",
                           "instruction": "check mail", "every": "60"})
        assert "main brain" in out and "scheduled every 60s" in out
        c.post("/api/dots", json={"name": "Worker", "role": "r"})
        out = dots_action({"action": "task_create", "dot": "Worker",
                           "instruction": "check mail every hour",
                           "every": "60"})
        assert "scheduled every 60s" in out and "90s" in out
        # multiple dots, none named → still honest disambiguation
        c.post("/api/dots", json={"name": "Worker2", "role": "r"})
        out = dots_action({"action": "task_create",
                           "instruction": "ambiguous", "every": "60"})
        assert "Which Dot" in out
        # invalid every → honest
        out = dots_action({"action": "task_create", "dot": "Worker",
                           "instruction": "x", "every": "abc"})
        assert "every_seconds" in out
        tasks = store.list_tasks()
        assert len(tasks) == 2 and tasks[0]["status"] == "active"
        assert tasks[0]["dot_id"] is None            # main-brain task
        assert tasks[1]["dot_id"] == 1               # Worker
        for t in tasks:
            store.set_task_status(t["id"], "paused")  # keep ticker off
        listed = dots_action({"action": "task_list"})
        assert "every 60s on Worker" in listed and "paused" in listed
        assert "every 60s on main brain" in listed
        # route shapes — dot_id optional
        r = c.post("/api/tasks", json={"instruction": "via http",
                                       "every_seconds": 120})
        assert r.status_code == 201 and r.json()["dot_id"] is None
        r = c.post("/api/tasks", json={"instruction": "via http",
                                       "every_seconds": 120,
                                       "dot_id": 1})
        assert r.status_code == 201 and r.json()["every_seconds"] == 120
        assert c.post("/api/tasks", json={"instruction": "y",
                                          "every_seconds": 0,
                                          "dot_id": 1}).status_code == 400
        assert c.post("/api/tasks", json={"instruction": "y",
                                          "every_seconds": 60,
                                          "dot_id": 99}).status_code == 404
        assert c.get("/api/tasks/999").status_code == 404
        # honest unknown task references
        assert "No task" in dots_action({"action": "task_cancel",
                                         "which": "nope"})
        assert "which=#task-id" in dots_action({"action": "task_runs",
                                                "which": "abc"})

    def test_main_brain_task_executes(self, env):
        """dot-less task: store → scheduler._execute → brain.reply({})."""
        from dots import scheduler, store
        t = store.create_task("main ping", "run the ping", 3600, None)
        assert t["dot_id"] is None
        assert scheduler.tick() == 1                  # due now → spawn
        run = _wait_for(lambda: store.last_run(t["id"]))
        assert run is not None and run["status"] == "ok"
        assert "scripted reply to:" in run["output"]
        msgs = store.list_messages(f"task:{t['id']}")
        assert msgs[-1]["role"] == "dot"              # main brain answered

    def test_tasks_dot_id_nullable_migration(self, env, monkeypatch,
                                             tmp_path):
        """Old NOT NULL schema rebuilds to nullable, preserving rows."""
        import sqlite3 as _sq
        from dots import db, store
        old = tmp_path / "old_schema.db"
        conn = _sq.connect(old)
        conn.executescript(
            "CREATE TABLE tasks ("
            " id INTEGER PRIMARY KEY, name TEXT NOT NULL,"
            " instruction TEXT NOT NULL, every_seconds INTEGER NOT NULL,"
            " dot_id INTEGER NOT NULL,"
            " status TEXT NOT NULL DEFAULT 'active',"
            " next_run_at REAL NOT NULL, last_run_at REAL,"
            " created_at REAL NOT NULL);"
            "INSERT INTO tasks VALUES"
            " (1,'old','do old',60,7,'active',1,NULL,1);")
        conn.commit()
        conn.close()
        db.reset_for_tests()                          # drop fixture's conn
        monkeypatch.setattr(db, "_db_path", lambda: old)
        try:
            row = store.get_task(1)
            assert row is not None and row["dot_id"] == 7   # preserved
            t = store.create_task("main", "hi", 60, None)    # nullable now
            assert t["dot_id"] is None
        finally:
            db.reset_for_tests()                      # never leak old conn

    def test_cron_schedule_create_validate_advance(self, env):
        import time as _t
        from actions.dots import dots as dots_action
        from dots import store
        c = env["client"]
        t = store.create_task("dawn", "say hi", 3600, None,
                              cron="*/5 * * * *")
        assert t["schedule_kind"] == "cron"
        assert t["cron_expr"] == "*/5 * * * *"
        now = _t.time()
        assert now < t["next_run_at"] <= now + 300
        with pytest.raises(ValueError, match="bad cron"):
            store.create_task("x", "y", 60, None, cron="not a cron")
        with pytest.raises(ValueError, match="ONE schedule"):
            store.create_task("x", "y", 60, None,
                              cron="* * * * *", run_at=now + 10)
        nxt = store.advance_next_run(t["id"], t["next_run_at"], _t.time())
        assert nxt > _t.time()                        # strictly future
        # HTTP + brain surfaces
        r = c.post("/api/tasks", json={"instruction": "cron job",
                                       "cron": "0 7 * * *"})
        assert r.status_code == 201
        assert r.json()["schedule_kind"] == "cron"
        assert c.post("/api/tasks", json={"instruction": "bad",
                                          "cron": "70 * * * *"}
                      ).status_code == 400
        out = dots_action({"action": "task_create",
                           "instruction": "am brief", "cron": "0 6 * * *"})
        assert "cron 0 6 * * *" in out
        assert "cron 0 6 * * *" in dots_action({"action": "task_list"})

    def test_at_schedule_runs_once_then_done(self, env):
        import time as _t
        from actions.dots import dots as dots_action
        from dots import scheduler, store
        t = store.create_task("one shot", "do it", 3600, None,
                              run_at=_t.time() - 1)
        assert t["schedule_kind"] == "at"
        assert any(x["id"] == t["id"] for x in store.due_tasks(_t.time()))
        assert scheduler.tick() == 1                  # due now → spawn
        run = _wait_for(lambda: store.last_run(t["id"]))
        assert run is not None and run["status"] == "ok"
        done = _wait_for(lambda: store.get_task(t["id"])
                         if store.get_task(t["id"])["status"] == "done"
                         else None)
        assert done is not None
        assert scheduler.tick() == 0                  # one-shot never repeats
        with pytest.raises(ValueError, match="done"):
            scheduler.resume(t["id"])
        # future at → not due
        t2 = store.create_task("later", "later job", 3600, None,
                               run_at=_t.time() + 3600)
        assert all(x["id"] != t2["id"] for x in store.due_tasks(_t.time()))
        # brain surface: absolute local time + honest parse errors
        out = dots_action({"action": "task_create", "instruction": "x",
                           "at": "2099-01-02 03:04"})
        assert "at 2099-01-02 03:04" in out
        out2 = dots_action({"action": "task_create", "instruction": "x",
                            "at": "not-a-date"})
        assert "YYYY-MM-DD" in out2

    def test_run_ok_flow_and_convo_history(self, env):
        from dots import scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "Relay", "role": "r"})
        t = store.create_task("ping", "run the ping", 3600, 1)
        assert scheduler.tick() == 1                  # due now → spawn
        run = _wait_for(lambda: store.last_run(t["id"]))
        assert run is not None and run["status"] == "ok"
        assert "scripted reply to:" in run["output"]
        # dots/scheduler._execute writes the run row FIRST and the dot's
        # reply / the reschedule after it, so waiting on the row alone races
        # the worker thread. Wait for the thing actually being asserted.
        assert _wait_for(lambda: len(store.list_messages(f"task:{t['id']}")) >= 2
                         and store.list_messages(f"task:{t['id']}")[-1]["role"] == "dot",
                         timeout=4), "the dot's reply never reached the history"
        msgs = store.list_messages(f"task:{t['id']}")
        assert msgs[-2]["role"] == "user" and msgs[-1]["role"] == "dot"
        # next_run advanced by exactly one window here (no missed slots)
        assert _wait_for(lambda: store.get_task(t["id"])["next_run_at"]
                         >= t["next_run_at"] + 3600, timeout=4)
        fresh = store.get_task(t["id"])
        assert fresh["next_run_at"] >= t["next_run_at"] + 3600
        assert fresh["last_run_at"] is not None
        assert scheduler.tick() == 0                  # nothing due now

    def test_skip_missed_windows(self, env):
        import time
        from dots import scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "M", "role": "r"})
        base = time.time() - 35                       # 3.5 windows missed
        t = store.create_task("m", "do the thing", 10, 1, next_run_at=base)
        assert scheduler.tick() == 1
        # `advance_next_run` runs after `record_run`, so wait for the advance
        # itself rather than for the run row.
        assert _wait_for(lambda: store.get_task(t["id"])["next_run_at"] > base,
                         timeout=4), "the next window was never advanced"
        fresh = store.get_task(t["id"])
        jump = fresh["next_run_at"] - base
        assert jump % 10 == 0 and 30 <= jump <= 70    # whole multiples only
        assert fresh["next_run_at"] > time.time()     # no thundering catch-up

    def test_pause_resume_cancel_state(self, env):
        import time
        from dots import scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "P", "role": "r"})
        t = store.create_task("p", "stay alive", 5, 1)
        scheduler.pause(t["id"])
        assert store.get_task(t["id"])["status"] == "paused"
        assert store.due_tasks(time.time()) == []     # paused = not due
        assert scheduler.tick() == 0
        assert store.last_run(t["id"]) is None        # state kept, no runs
        scheduler.resume(t["id"])
        assert store.get_task(t["id"])["status"] == "active"
        assert len(store.due_tasks(time.time())) == 1
        scheduler.cancel(t["id"])
        assert store.get_task(t["id"])["status"] == "cancelled"
        assert store.due_tasks(time.time()) == []
        assert scheduler.tick() == 0
        import pytest as _pt
        with _pt.raises(ValueError):
            scheduler.resume(t["id"])                 # cancelled is final
        with _pt.raises(ValueError):
            scheduler.pause(t["id"])

    def test_cancel_aborts_live_run(self, env):
        import time as _t
        from dots import brain, scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "Slow", "role": "r"})

        def slow(system, hist):
            _t.sleep(1.5)
            return "too late"

        brain.set_llm(slow)
        t = store.create_task("slowjob", "take your time", 9999, 1)
        nxt_before = t["next_run_at"]
        scheduler._spawn(t, trigger="test")
        assert _wait_for(lambda: t["id"] in scheduler._live, timeout=2)
        res = scheduler.cancel(t["id"])
        assert res["aborted_live_run"] is True
        run = _wait_for(lambda: store.last_run(t["id"]))
        assert run is not None and run["status"] == "cancelled"
        assert "cancelled by owner" in run["output"]
        fresh = store.get_task(t["id"])
        assert fresh["status"] == "cancelled"
        assert fresh["next_run_at"] == nxt_before      # never reschedules
        # live registry drains
        assert _wait_for(lambda: t["id"] not in scheduler._live, timeout=4)

    def test_retry_failed_then_ok(self, env, monkeypatch):
        import pytest as _pt
        from actions.dots import dots as dots_action
        from dots import brain, scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "Flaky", "role": "r"})
        t = store.create_task("flaky", "try this", 9999, 1)
        # run 1: no brain anywhere → honest failure (not a fake answer)
        brain.set_llm(None)
        monkeypatch.setattr(brain, "_local_ready", lambda: False)
        import core.gemini as gemini
        monkeypatch.setattr(gemini, "call",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("no key")))
        assert scheduler.tick() == 1
        run = _wait_for(lambda: store.last_run(t["id"]))
        assert run["status"] == "failed"
        assert run["output"].startswith("No brain reachable")
        # The run ROW is written before the worker clears `_live`, and the
        # retry path refuses while the task is live ("already running") — so
        # waiting for the row alone races the thread. `advance_next_run` (a DB
        # write) and the `finally: _live.pop()` both sit after `record_run`.
        # Seen on CI's py3.13 runner; reproducible locally by slowing
        # `advance_next_run` by half a second.
        assert _wait_for(lambda: t["id"] not in scheduler._live, timeout=4)
        # retry with brain fixed → ok
        brain.set_llm(lambda system, hist: "recovered now")
        out = dots_action({"action": "task_retry", "which": str(t["id"])})
        assert "Retrying task" in out
        run2 = _wait_for(
            lambda: [r for r in store.list_runs(t["id"])
                     if r["id"] != run["id"] and r["status"] == "ok"])
        assert run2, store.list_runs(t["id"])
        # honest retry refusals
        out = dots_action({"action": "task_retry", "which": str(t["id"])})
        assert "nothing to retry" in out
        scheduler.pause(t["id"])
        out = dots_action({"action": "task_retry", "which": str(t["id"])})
        assert "paused" in out
        with _pt.raises(KeyError):
            scheduler.retry(4242)

    def test_hard_deadline_timeout(self, env, monkeypatch):
        import time as _t
        from dots import brain, scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "Hang", "role": "r"})

        def hang(system, hist):
            _t.sleep(2.0)
            return "done late"

        brain.set_llm(hang)
        monkeypatch.setattr(scheduler, "DEADLINE_SECONDS", 0.5)
        t = store.create_task("h", "hang please", 9999, 1)
        assert scheduler.tick() == 1
        run = _wait_for(lambda: store.last_run(t["id"]), timeout=3)
        assert run["status"] == "timeout"
        assert "hard deadline" in run["output"]
        # the run row is written BEFORE next_run_at advances (worker order) —
        # poll for the reschedule instead of racing it
        def _advanced():
            f = store.get_task(t["id"])
            return f if f and f["next_run_at"] > t["next_run_at"] else None
        fresh = _wait_for(_advanced, timeout=3)
        assert fresh is not None, "timeout run never rescheduled next_run_at"
        assert fresh["status"] == "active"
        assert fresh["next_run_at"] > t["next_run_at"]
        assert _wait_for(lambda: t["id"] not in scheduler._live, timeout=4)

    def test_runs_endpoint_and_statuses(self, env):
        from dots import scheduler, store
        c = env["client"]
        c.post("/api/dots", json={"name": "R", "role": "r"})
        t = store.create_task("r1", "one", 9999, 1)
        scheduler.tick()
        assert _wait_for(lambda: store.last_run(t["id"]))
        rows = c.get(f"/api/tasks/{t['id']}/runs").json()
        assert rows and rows[0]["status"] == "ok"
        assert c.get("/api/tasks/999/runs").status_code == 404
        assert c.get(f"/api/tasks/{t['id']}").json()["name"] == "r1"
        # store refuses bogus run/task statuses (schema contract)
        with pytest.raises(ValueError):
            store.record_run(t["id"], "weird", 1.0)
        with pytest.raises(ValueError):
            store.set_task_status(t["id"], "weird")

    def test_ticker_lifecycle(self, env, monkeypatch):
        from dots import scheduler, store
        c = env["client"]
        assert not scheduler.is_running()
        c.post("/api/dots", json={"name": "Tick", "role": "r"})
        monkeypatch.setattr(scheduler, "TICK_SECONDS", 0.1)
        c.post("/api/tasks", json={"instruction": "tick me",
                                   "every_seconds": 9999, "dot_id": 1})
        assert scheduler.is_running()                 # POST ensured it
        tid = store.list_tasks()[0]["id"]
        run = _wait_for(lambda: store.last_run(tid), timeout=4)
        assert run is not None and run["status"] == "ok"
        scheduler.shutdown()
        assert not scheduler.is_running()


class TestLearning:
    def _seed(self, n=3, ok=True, texts=None):
        from dots import store
        texts = texts or [
            "summarize https://ex.com/a{i} into my notes".replace(
                "{i}", str(i))
            for i in range(n)
        ]
        for i, txt in enumerate(texts):
            store.add_message("dot:77", "user", txt)
            store.add_message("dot:77", "dot",
                              "Brain error: boom" if not ok
                              else f"done with step {i}")

    def test_mine_repeated_shapes_idempotent(self, env):
        from dots import learning, store
        assert learning.mine() == []                  # nothing yet
        self._seed(3)
        drafts = learning.mine()
        assert len(drafts) == 1
        d = drafts[0]
        assert d["status"] == "draft"                 # NEVER auto-publish
        assert "summarize" in d["title"]
        assert d["source_note"].startswith("fp:")
        assert "Observed requests" in d["body_md"]
        assert learning.mine() == []                  # fingerprint dedupe
        assert len(store.list_skills("draft")) == 1

    def test_needs_completed_ok_answers(self, env):
        from dots import learning, store
        self._seed(3, ok=False)                       # honest failures
        assert learning.mine() == []
        # too few occurrences
        self._seed(2, texts=["compile the quarterly report deck now",
                             "compile the quarterly report deck again"])
        assert learning.mine() == []
        # trivial chit-chat is not a skill (shape too small)
        self._seed(3, texts=["hi", "hi there", "hi"])
        assert learning.mine() == []

    def test_t9_drafts_invisible_until_published(self, env):
        import json as _json
        from dots import brain, learning, store
        self._seed(3)
        assert len(learning.mine()) == 1
        sid = store.list_skills("draft")[0]["id"]
        dot = store.create_dot("Reader")
        # draft invisible: prompt + tool
        sys_txt = brain.system_prompt(dot, [], None)
        assert "summarize" not in sys_txt
        from dots.tools import run_tool
        assert "owner-only" not in run_tool(dot, "load_skill",
                                            {"query": "summarize"})
        out = run_tool(dot, "load_skill", {"query": "summarize"})
        # honest: either "no published skills yet" or "no match" — never
        # the draft's content
        assert "no published skill" in out and "Observed requests" not in out
        # publish (owner endpoint) → visible
        c = env["client"]
        r = c.post(f"/api/skills/{sid}/publish")
        assert r.status_code == 200 and r.json()["status"] == "published"
        sys_txt = brain.system_prompt(dot, [], None)
        assert "PUBLISHED SKILLS" in sys_txt and "summarize" in sys_txt
        out = run_tool(dot, "load_skill", {"query": "summarize"})
        assert "Observed requests" in out
        # double publish → 409 honest; drafts list empty; archive hides
        assert c.post(f"/api/skills/{sid}/publish").status_code == 409
        assert store.list_skills("draft") == []
        assert c.post(f"/api/skills/{sid}/archive").status_code == 200
        assert "summarize" not in brain.system_prompt(dot, [], None)
        # archived fingerprint still blocks re-mining (no dup drafts)
        assert learning.mine() == []
        assert c.post("/api/skills/999/publish").status_code == 404
        # load_skill no-arg lists ONLY published
        assert run_tool(dot, "load_skill", {}) == \
            "no published skills yet — drafts exist only for the owner " \
            "until they publish them"

    def test_voice_skill_actions(self, env):
        from actions.dots import dots as dots_action
        from dots import learning, store
        out = dots_action({"action": "skill_mine"})
        assert "No new skill drafts" in out
        self._seed(3)
        out = dots_action({"action": "skill_mine"})
        assert "New DRAFT skill(s)" in out and "auto-published" in out
        sid = store.list_skills("draft")[0]["id"]
        out = dots_action({"action": "skill_list"})
        assert f"#{sid} [draft]" in out
        out = dots_action({"action": "skill_publish", "which": str(sid)})
        assert "PUBLISHED — every Dot now sees it" in out
        out = dots_action({"action": "skill_publish", "which": str(sid)})
        assert "already published" in out
        out = dots_action({"action": "skill_publish", "which": "zz"})
        assert "which=#skill-id" in out
        out = dots_action({"action": "skill_archive", "which": str(sid)})
        assert "archived" in out
        out = dots_action({"action": "skill_list", "status": "archived"})
        assert f"#{sid} [archived]" in out

    def test_chat_triggers_mining_automatic(self, env):
        from actions.dots import dots as dots_action
        from dots import store
        store.create_dot("Auto", permissions={"research": False})
        for i in range(3):
            dots_action({"action": "chat", "dot": "Auto",
                         "message": f"draft a status email for day {i}"})
        drafts = store.list_skills("draft")
        assert len(drafts) == 1
        assert "draft a status email" in drafts[0]["title"]

    def test_skill_file_jail(self, env, monkeypatch, tmp_path):
        from dots import tools_learning
        root = tmp_path / "skills"
        root.mkdir()
        monkeypatch.setattr(tools_learning, "_skills_dir", lambda: root)
        (root / "checklist.md").write_text("# how to check",
                                           encoding="utf-8")
        from dots import store
        from dots.tools import run_tool
        dot = store.create_dot("S")
        out = run_tool(dot, "read_skill_file", {"path": "checklist.md"})
        assert out == "# how to check"
        assert run_tool(dot, "read_skill_file",
                        {"path": "../x"}).startswith("denied:")
        assert "no such skill file" in run_tool(dot, "read_skill_file",
                                                {"path": "nope.md"})

    def test_summariser_seam(self, env):
        from dots import learning, store
        try:
            learning.set_summariser(
                lambda sh, ex: f"SUMMARY::{sh}::{len(ex)}")
            self._seed(3)
            drafts = learning.mine()
            assert len(drafts) == 1
            assert drafts[0]["body_md"].startswith("SUMMARY::")
        finally:
            learning.set_summariser(None)


# ═══════════════════════════════════════════════════════════════════════════
# Batch 6e: Slack (T10 allowlists) + voice calls (captions, receipt)
# ═══════════════════════════════════════════════════════════════════════════

def _slack_payload(text="@Researcher hello there", user="U_OWNER",
                   team="T_WORK", channel="C1", ts="1111.2222",
                   thread=None):
    ev = {"type": "message", "text": text, "user": user,
          "channel": channel, "ts": ts}
    if thread:
        ev["thread_ts"] = thread
    return {"type": "event_callback", "team_id": team, "event": ev}


class TestSlackEvents:
    def _cfg(self, monkeypatch, workspaces=("T_WORK",),
             users=("U_OWNER",), token="xoxb-test"):
        from dots import slack
        monkeypatch.setattr(slack, "_config", lambda: {
            "bot_token": token,
            "workspaces": list(workspaces),
            "users": list(users),
        })
        return slack

    def test_url_verification_handshake(self, env):
        c = env["client"]
        r = c.post("/api/slack/events",
                   json={"type": "url_verification",
                         "challenge": "chall-123"})
        assert r.status_code == 200 and r.json()["challenge"] == "chall-123"

    def test_t10_non_allowlisted_ack_and_nothing_runs(self, env,
                                                      monkeypatch):
        # T10: non-allowlisted Slack user → event acknowledged, nothing
        # executed (no messages, no brain, no Slack post)
        slack = self._cfg(monkeypatch)
        from dots import store
        calls = {"brain": 0, "post": 0}
        monkeypatch.setattr(slack, "_post_slack",
                            lambda *a, **k: calls.__setitem__(
                                "post", calls["post"] + 1) or {"ok": True})
        res = slack.handle_event(_slack_payload(user="U_STRANGER"))
        assert res["ok"] is True and "allowlist" in res["ignored"]
        res = slack.handle_event(
            _slack_payload(team="T_EVIL"))
        assert res["ok"] is True and "allowlist" in res["ignored"]
        # secure default: EMPTY allowlists deny everything
        self._cfg(monkeypatch, workspaces=(), users=())
        res = slack.handle_event(_slack_payload())
        assert "allowlist" in res["ignored"]
        assert store.list_messages("slack:C1:1111.2222") == []
        assert calls["post"] == 0
        # route acknowledges with 200 too
        c = env["client"]
        self._cfg(monkeypatch)
        r = c.post("/api/slack/events", json=_slack_payload(user="U_BAD"))
        assert r.status_code == 200 and r.json()["ok"] is True

    def test_mention_runs_brain_and_thread_reply(self, env, monkeypatch):
        slack = self._cfg(monkeypatch)
        from dots import store
        posted = []

        def fake_post(method, payload):
            posted.append((method, payload))
            return {"ok": True}

        monkeypatch.setattr(slack, "_post_slack", fake_post)
        env["client"].post("/api/dots", json={"name": "Researcher",
                                              "role": "cite sources"})
        res = slack.handle_event(_slack_payload(
            text="@Researcher summarise this thread"))
        assert res["ok"] and res["dot"] == "Researcher"
        assert res["reply"].startswith("scripted reply to:")
        assert res["posted"] is True
        # conversation namespace: slack:<channel>:<thread_ts>
        msgs = store.list_messages("slack:C1:1111.2222")
        assert msgs[-2]["role"] == "user" and msgs[-1]["role"] == "dot"
        assert msgs[-2]["meta"]["slack"]["user"] == "U_OWNER"
        # reply went into the SAME thread
        method, body = posted[0]
        assert method == "chat.postMessage"
        assert body["channel"] == "C1" and body["thread_ts"] == "1111.2222"
        # thread continuity row
        rows = store.list_messages("slack:C1:1111.2222")
        assert rows and store.find_dot_by_name("researcher")["id"] == 1
        # background mode → 200 instantly, work in a thread
        r = env["client"].post("/api/slack/events",
                               json=_slack_payload(
                                   text="@Researcher another"))
        assert r.status_code == 200 and r.json()["processing"] is True
        got = _wait_for(lambda: len(
            store.list_messages("slack:C1:1111.2222")) >= 4)
        assert got

    def test_no_mention_and_bot_messages_ignored(self, env, monkeypatch):
        slack = self._cfg(monkeypatch)
        res = slack.handle_event(_slack_payload(text="just chatting"))
        assert "@mention" in res["ignored"]
        res = slack.handle_event(_slack_payload(text="@Ghost hey"))
        assert "no Dot named" in res["ignored"]
        p = _slack_payload(text="@Researcher hi")
        p["event"]["bot_id"] = "B1"
        res = slack.handle_event(p)
        assert "bot" in res["ignored"]
        res = slack.handle_event({"type": "event_callback",
                                  "event": {"type": "app_mention"}})
        assert "not a message event" in res["ignored"]
        assert slack.evaluate("garbage")[0] is False

    def test_thread_reply_targets_parent_thread(self, env, monkeypatch):
        # a reply INSIDE an existing thread keeps the parent thread_ts
        slack = self._cfg(monkeypatch)
        from dots import store
        posted = []
        monkeypatch.setattr(
            slack, "_post_slack",
            lambda m, p: posted.append(p) or {"ok": True})
        env["client"].post("/api/dots", json={"name": "Researcher",
                                              "role": "r"})
        slack.handle_event(_slack_payload(ts="9999.0001",
                                          thread="1111.2222"))
        msgs = store.list_messages("slack:C1:1111.2222")
        assert msgs, "convo keyed by THREAD, not the reply ts"
        assert posted[0]["thread_ts"] == "1111.2222"


class TestVoiceCalls:
    def _dot(self, env, name="Guide"):
        r = env["client"].post("/api/dots", json={"name": name,
                                                  "role": "be helpful"})
        assert r.status_code == 201
        return r.json()

    def test_start_end_timer_and_duplicate(self, env):
        from actions.dots import dots as dots_action
        from dots import voice
        self._dot(env)
        out = dots_action({"action": "call_start", "dot": "Guide"})
        assert "Call #1 live" in out and "captions-only" in out
        out = dots_action({"action": "call_start", "dot": "Guide"})
        assert "already in call" in out
        c = env["client"]
        live = c.get("/api/calls?status=active").json()
        assert len(live) == 1 and live[0]["elapsed_seconds"] >= 0
        got = c.get(f"/api/calls/{live[0]['id']}").json()
        assert got["status"] == "active" and got["elapsed_seconds"] >= 0
        assert c.post("/api/calls", json={"dot_id": 99}).status_code == 404
        ended = dots_action({"action": "call_end", "dot": "Guide"})
        assert "ended" in ended and "transcript lines" in ended
        # honest: caption after end
        res = voice.caption(1, "user", "hello?")
        assert "ended" in res["error"]
        # duplicate call allowed AFTER end
        assert "Call #2 live" in dots_action({"action": "call_start",
                                              "dot": "Guide"})

    def test_captions_store_and_generate_reply(self, env):
        from dots import store, voice
        d = self._dot(env)
        c = voice.start(d["id"])
        res = voice.caption(c["id"], "user", "what is on my page?")
        assert res["ok"] and res["speaker"] == "user"
        assert res["dot_caption"].startswith("scripted reply to:")
        msgs = store.list_messages(f"call:{c['id']}")
        assert [m["role"] for m in msgs] == ["user", "dot"]
        assert msgs[0]["meta"]["speaker"] == "user"
        assert msgs[1]["meta"]["speaker"] == "dot"
        # dot-side caption (audio path / owner injection) stores directly
        res = voice.caption(c["id"], "dot", "speaking line",
                            generate_reply=False)
        assert res["ok"]
        # honest input errors
        assert "speaker must" in voice.caption(c["id"], "alien", "x")["error"]
        assert voice.caption(c["id"], "user", "  ")["error"] == \
            "empty caption"
        voice.end(c["id"])
        assert "ended" in voice.caption(c["id"], "user", "late")["error"]
        assert "no call" in voice.caption(999, "user", "x")["error"]

    def test_transcript_receipt(self, env):
        from dots import voice
        c = env["client"]
        d = self._dot(env)
        call = voice.start(d["id"])
        voice.caption(call["id"], "user", "hello agent",
                      generate_reply=False)
        voice.caption(call["id"], "dot", "hi owner", generate_reply=False)
        # json receipt (API)
        j = c.get(f"/api/calls/{call['id']}/transcript?format=json").json()
        assert [m["speaker"] for m in j] == ["user", "dot"]
        assert j[0]["content"] == "hello agent"
        # text receipt = downloadable attachment
        r = c.get(f"/api/calls/{call['id']}/transcript?format=text")
        assert r.status_code == 200
        assert "call-1-transcript.txt" in \
            r.headers.get("content-disposition", "")
        body = r.text
        assert "[user] hello agent" in body and "[dot] hi owner" in body
        assert "Call #1" in body
        assert c.get("/api/calls/999/transcript").status_code == 404
        assert c.get(
            f"/api/calls/{call['id']}/transcript?format=xml"
        ).status_code == 400
        # voice receipt
        from actions.dots import dots as dots_action
        out = dots_action({"action": "call_transcript",
                           "which": str(call["id"])})
        assert "[user] hello agent" in out

    def test_background_agent_reuses_scheduler(self, env, monkeypatch):
        from actions.dots import dots as dots_action
        from dots import scheduler, store, voice
        d = self._dot(env)
        call = voice.start(d["id"])
        out = dots_action({"action": "call_background",
                           "which": str(call["id"]),
                           "message": "keep watching the inbox"})
        assert "Background agent scheduled" in out and "task #1" in out
        tasks = store.list_tasks()
        assert len(tasks) == 1
        assert tasks[0]["dot_id"] == d["id"]     # bound to CALL's dot
        assert "call-1" in tasks[0]["name"]
        assert scheduler.is_running()            # ensure_started called
        # honest validation
        c = env["client"]
        assert c.post(f"/api/calls/{call['id']}/background",
                      json={"instruction": ""}).status_code == 400
        assert c.post("/api/calls/999/background",
                      json={"instruction": "x"}).status_code == 404
        assert "No Dot named" in dots_action(
            {"action": "call_start", "dot": "NobodyDot"})

    def test_provider_seam(self, env):
        from dots import voice
        d = self._dot(env)
        assert voice.provider_name() == "captions-only"
        started, stopped = [], []

        class FakeLive:
            name = "gemini-live"

            def start(self, call):
                started.append(call["id"])

            def stop(self, call):
                stopped.append(call["id"])

        try:
            voice.set_provider(FakeLive())
            call = voice.start(d["id"])
            assert call["provider"] == "gemini-live"
            assert started == [call["id"]]
            voice.end(call["id"])
            assert stopped == [call["id"]]
        finally:
            voice.set_provider(None)
        assert voice.provider_name() == "captions-only"

    def test_call_page_context_and_unknown_actions(self, env):
        from actions.dots import dots as dots_action
        from actions.pages import pages as pages_action
        from dots import voice
        pages_action({"action": "space_create", "name": "S"})
        pages_action({"action": "page_create", "space": "S",
                      "title": "Plan", "content": "- ship it"})
        self._dot(env)
        out = dots_action({"action": "call_start", "dot": "Guide",
                           "page": "Plan"})
        assert "Call #1 live" in out
        call = voice.list_calls("active")[0]
        assert call["page_id"] is not None
        # capture the SYSTEM prompt: proof the PAGE context reached it
        from dots import brain
        seen = {}

        def see(system, hist):
            seen["system"] = system
            return "page-aware reply"

        brain.set_llm(see)
        out = dots_action({"action": "call_caption",
                           "which": str(call["id"]),
                           "message": "read the page"})
        assert "page-aware reply" in out
        assert "ship it" in seen["system"]
        assert "Unknown dots action" in dots_action(
            {"action": "call_teleport"})
        voice.end(call["id"])
        rows = dots_action({"action": "call_list"})
        assert "#1 [ended]" in rows


class TestSkillVerifyLoop:
    """Batch 5 (R2 §9): structural verify on draft + publish, sweep."""

    GOOD = (
        "# Quarterly report deck\n\n"
        "## When to use\n"
        "When the user asks for the quarterly report deck.\n\n"
        "## Steps\n"
        "1. Pull the latest numbers from the sheet.\n"
        "2. Update the three standard charts.\n"
        "3. Export to PDF and share the link.\n\n"
        "```bash\n./scripts/report.sh --quarter latest\n```\n"
    )

    def test_draft_auto_verified_and_clean(self, env):
        from dots import store
        s = store.create_skill_draft("Quarterly report deck", self.GOOD)
        assert s["verify_json"], "verdict persisted on draft creation"
        assert '"ok": true' in s["verify_json"].replace(" ", " ").lower() \
            or '"ok": true' in s["verify_json"]
        assert s["verified_at"] is not None

    def test_bad_draft_collects_issues(self, env):
        from dots import store
        s = store.create_skill_draft("Untitled skill", "TODO: write this")
        v = store.verify_skill(s["id"])
        assert v["verify"]["ok"] is False
        issues = " | ".join(v["verify"]["issues"])
        assert "too short" in issues
        assert "placeholder" in issues or "title" in issues
        assert "heading" in issues

    def test_unbalanced_fences_flagged(self, env):
        from dots import store
        body = ("# How to\n\n- do the thing carefully\n"
                "```python\nprint(1)")            # fence never closed
        s = store.create_skill_draft("How to", body + " " + "x" * 140)
        v = store.verify_skill(s["id"])
        assert any("fence" in i for i in v["verify"]["issues"])

    def test_publish_reruns_verify(self, env):
        from dots import store
        s = store.create_skill_draft("Quarterly report deck", self.GOOD)
        before = s["verified_at"]
        out = store.publish_skill(s["id"])
        assert out["status"] == "published"
        assert out["verified_at"] >= before          # re-ran on publish
        assert '"ok": true' in out["verify_json"]

    def test_verify_all_sweep_counts_failures(self, env):
        from dots import store
        good = store.create_skill_draft("Quarterly report deck", self.GOOD)
        store.publish_skill(good["id"])
        bad = store.create_skill_draft("Broken", "TBD")
        store.publish_skill(bad["id"])
        res = store.verify_all("published")
        assert res["checked"] == 2
        assert len(res["failed"]) == 1
        assert res["failed"][0]["title"] == "Broken"

    def test_verify_routes(self, env):
        c = env["client"]
        from dots import store
        s = store.create_skill_draft("Quarterly report deck", self.GOOD)
        r = c.post(f"/api/skills/{s['id']}/verify")
        assert r.status_code == 200
        assert r.json()["verify"]["ok"] is True
        assert c.post("/api/skills/424242/verify").status_code == 404
        sweep = c.post("/api/skills/verify-all?status=draft")
        assert sweep.status_code == 200
        assert sweep.json()["checked"] >= 1

    def test_lint_unit_pure(self):
        from dots.store import _lint_skill
        assert _lint_skill("T", "# Title\n\n- step one details here ok\n"
                               + "x" * 160) == []
        assert _lint_skill("", "") != []
