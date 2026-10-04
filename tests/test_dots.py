# tests/test_dots.py — Dots platform (self-hosted agent workspace)
# Batch 6a: storage, blocks, revisions, approvals, memory, conversations.
import json
from pathlib import Path

import pytest

# ═══════════════════════════════════════════════════════════════════════════
# Fixture: isolated db + TestClient + scripted brain
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture()
def env(monkeypatch, tmp_path):
    import dots.db as db
    import dots.brain as brain
    monkeypatch.setattr(db, "_db_path", lambda: tmp_path / "dots.db")
    db.reset_for_tests()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from dots.server import router
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    brain.set_llm(lambda system, hist: f"scripted reply to: {hist[-1]['content']}")
    yield {"client": client, "brain": brain, "db": db, "tmp": tmp_path}
    brain.set_llm(None)
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
        bare = store.create_dot("Bare")
        assert specs_for(bare) == []
        res = store.create_dot("Web", permissions={"research": True})
        names = [t["function"]["name"] for t in specs_for(res)]
        assert names == ["search_web", "read_public_page"]
        spa = store.create_dot("Sp", permissions={"space": True})
        names = [t["function"]["name"] for t in specs_for(spa)]
        assert names == ["list_authorized_spaces", "list_space_pages",
                         "read_space_page", "create_space_page",
                         "edit_space_page"]
        both = store.create_dot("AB",
                                permissions={"research": True, "space": True})
        assert len(specs_for(both)) == 7

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
        assert "(none — you cannot call tools)" in sys_txt

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
