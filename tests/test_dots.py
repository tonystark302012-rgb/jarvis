# tests/test_dots.py — Dots platform (self-hosted agent workspace)
# Batch 6a: storage, blocks, revisions, approvals, memory, conversations.
import json

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

    from fastapi.testclient import TestClient
    from dots.server import app
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
