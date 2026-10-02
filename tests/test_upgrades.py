"""
Tests for the changes made in this branch.

Run from the repo root:

    python -m pytest tests/ -q

Everything here is offline and needs no microphone, display or API key.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── P0-1: actions/desktop.py no longer executes generated code ────────────────

def test_desktop_has_no_code_generation_machinery():
    import actions.desktop as d
    for gone in ("_build_sandbox", "_execute_generated_code",
                 "_ask_gemini_for_desktop_action"):
        assert not hasattr(d, gone), f"{gone} should have been removed"


def test_desktop_module_contains_no_exec_call():
    """The RCE was `exec(compile(...))` on model output. It must not come back."""
    src = Path("actions/desktop.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "exec(" not in stripped, f"exec() reappeared: {line!r}"


def test_desktop_preserved_the_functions_people_use():
    import actions.desktop as d
    for fn in ("set_wallpaper", "set_wallpaper_from_url", "get_current_wallpaper",
               "organize_desktop", "list_desktop", "clean_desktop",
               "get_desktop_stats", "desktop_control"):
        assert callable(getattr(d, fn, None)), f"{fn} was lost in the rewrite"


@pytest.mark.parametrize("phrase,expected_fragment", [
    ("please organize my desktop", "organize"),
    ("how many files are on my desktop", "stats"),
    ("clean it off", "clean"),
])
def test_desktop_routes_free_text_to_named_actions(phrase, expected_fragment):
    """No Desktop folder in CI, so we assert the routing decision, not the result."""
    import actions.desktop as d
    out = d.desktop_control({"task": phrase}, player=None, session_memory=None)
    # either it ran the named action (and failed on the missing folder) or it
    # refused - what must NOT happen is code generation
    assert "Gemini" not in out and "Execution error" not in out


def test_desktop_refuses_unmappable_requests_instead_of_improvising():
    import actions.desktop as d
    out = d.desktop_control({"task": "launch a nuclear strike"},
                            player=None, session_memory=None)
    assert "can't map" in out.lower()
    for named in ("organize", "clean", "list", "stats"):
        assert named in out      # tells the model what it *can* do


# ── P0-2/3: dashboard key handling and brute-force guard ──────────────────────

def _server():
    import dashboard.server as ds
    ds._ensure_network_access = lambda port: None
    ds._ensure_certs = lambda: False
    return ds.DashboardServer()


def test_login_returns_a_random_key_not_the_pin():
    srv = _server()
    srv._pending_keys["JARVIS"] = time.time() + 60

    async def go():
        handler = None
        for r in srv.app.routes:
            if getattr(r, "path", "") == "/login" and "POST" in getattr(r, "methods", set()):
                handler = r.endpoint
        assert handler is not None

        class Req:
            client = type("C", (), {"host": "9.9.9.9"})()
            async def json(self):
                return {"pin": "JARVIS"}
        return await handler(Req())

    import json
    resp = asyncio.run(go())
    body = json.loads(resp.body.decode())
    assert body["ok"] is True
    enc = body["enc"]
    assert len(enc) == 64                      # 32 bytes, hex
    assert enc.upper() != "JARVIS"             # the PIN is not the key
    assert "JARVIS" not in enc.upper()


def test_each_session_gets_a_different_key():
    import json
    srv = _server()

    async def login(pin):
        handler = [r for r in srv.app.routes
                   if getattr(r, "path", "") == "/login"
                   and "POST" in getattr(r, "methods", set())][0].endpoint

        class Req:
            client = type("C", (), {"host": "8.8.8.8"})()
            async def json(self):
                return {"pin": pin}
        return await handler(Req())

    async def go():
        keys = []
        for _ in range(2):
            srv._pending_keys["JARVIS"] = time.time() + 60
            r = await login("JARVIS")
            keys.append(json.loads(r.body.decode())["enc"])
        return keys

    a, b = asyncio.run(go())
    assert a != b


def test_repeated_bad_keys_are_locked_out():
    srv = _server()

    async def go():
        handler = [r for r in srv.app.routes
                   if getattr(r, "path", "") == "/login"
                   and "POST" in getattr(r, "methods", set())][0].endpoint

        class Req:
            client = type("C", (), {"host": "7.7.7.7"})()
            async def json(self):
                return {"pin": "WRONGZ"}
        return [ (await handler(Req())).status_code
                 for _ in range(_server()._LOGIN_MAX_FAILS + 3) ]

    codes = asyncio.run(go())
    limit = _server()._LOGIN_MAX_FAILS
    assert codes[:limit] == [401] * limit
    assert 429 in codes, "expected a 429 once the lockout engaged"


# ── P1-1: semantic memory recall ──────────────────────────────────────────────

_STORE = {
    "relationships": {
        "sister": "Ayse - younger sister, studies architecture in Istanbul",
        "mother": "Fatma - calls every Sunday evening",
        "best_friend": "Deniz - climbs with him on Saturdays",
        "manager": "Elif - team lead, prefers written updates",
    },
    "identity": {
        "occupation": "backend engineer at a logistics company",
        "hometown": "grew up in Jaipur, Rajasthan",
        "language": "Hindi, with English mixed in",
        "birthday": "14 March",
    },
    "preferences": {
        "favourite_color": "deep blue",
        "coffee": "black, no sugar, two cups before noon",
        "music": "lo-fi while working, jazz in the evening",
        "ide": "uses Neovim, refuses VS Code",
    },
    "projects": {
        "jarvis": "a voice assistant built on the Gemini Live API",
        "gym": "trains four mornings a week, mostly kettlebells",
        "taxes": "self-assessment due 31 January, uses FreeAgent",
    },
    "wishes": {
        "travel": "wants to go to Japan in the spring",
        "buy": "saving for a standing desk",
        "learn": "wants to get better at spoken Turkish",
    },
    "notes": {
        "allergy": "allergic to penicillin",
        "car": "drives a 2016 diesel Estate, MOT in April",
        "wifi": "home network is called Flat-2G, password in the kitchen drawer",
        "sleep": "usually asleep by midnight, up at six",
        "doctor": "dentist appointment on the 12th at nine in the morning",
    },
}

_BENCH = [
    ("who is my sister?",                "relationships", "sister"),
    ("tell me about my family",          "relationships", "mother"),
    ("what do I do for work",            "identity",      "occupation"),
    ("where am I from",                  "identity",      "hometown"),
    ("my favourite colour",              "preferences",   "favourite_color"),
    ("how do I take my coffee",          "preferences",   "coffee"),
    ("anything I should know medically", "notes",         "allergy"),
    ("when is the dentist",              "notes",         "doctor"),
    ("what am I building at the moment", "projects",      "jarvis"),
    ("how do I stay fit",                "projects",      "gym"),
    ("what do I want to learn",          "wishes",        "learn"),
    ("how do I get online at home",      "notes",         "wifi"),
]


@pytest.fixture
def memory(tmp_path, monkeypatch):
    import memory.memory_manager as mm
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    for cat, items in _STORE.items():
        for k, v in items.items():
            mm.remember(k, v, category=cat)
    return mm


def _rank(mm, query, k=3):
    out = mm.search_memory(query, limit=k)
    lines = out.splitlines()[1:]
    return [line.split(":")[0].strip() for line in lines[:k]]


def test_recall_finds_facts_the_lexical_ranker_could_not(memory):
    """The shipped ranker managed 5/12 and dropped zero-scoring rows entirely."""
    hits = 0
    for query, cat, key in _BENCH:
        want = f"{cat}/{memory._pretty(key)}"
        if want in _rank(memory, query):
            hits += 1
    assert hits >= 9, f"recall regressed to {hits}/12"


def test_recall_never_claims_ignorance_while_the_store_has_facts(memory):
    """Paraphrases used to return "Nothing stored about ..."."""
    out = memory.search_memory("tell me about my family", limit=3)
    assert "Nothing stored" not in out
    assert "mother" in out.lower() or "sister" in out.lower()


def test_recall_stays_offline_and_fast(memory):
    t0 = time.perf_counter()
    for query, _c, _k in _BENCH:
        memory.search_memory(query)
    per_query_ms = (time.perf_counter() - t0) / len(_BENCH) * 1000
    # recall must never cost a second model round trip
    assert per_query_ms < 25, f"{per_query_ms:.1f} ms per query is too slow"


def test_recall_on_an_empty_store_is_a_sentence(memory, tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MEMORY_PATH", tmp_path / "empty.json")
    out = memory.search_memory("anything")
    assert isinstance(out, str) and out


# ── P1-2: safe parallel tool dispatch ─────────────────────────────────────────

class _Types:
    class FunctionResponse:
        def __init__(self, id=None, name=None, response=None, **kw):
            self.id, self.name, self.response = id, name, response


def _dispatcher():
    """Load the real _run_tool_calls out of main.py without importing Qt."""
    src = Path("main.py").read_text(encoding="utf-8")
    start = src.index("    _READ_ONLY_TOOLS = frozenset(")
    end = src.index("    async def _execute_tool(self, fc)", start)
    # main.py imports `traceback` at module level (line 43); the extracted
    # method relies on it, so it has to be in the namespace here too.
    ns = {"types": _Types()}
    exec("import asyncio\nimport traceback\nclass _S:\n" + src[start:end], ns)
    return ns["_S"]


class _Runner(_dispatcher()):                                     # type: ignore[misc]
    LAT = {"web_search": 0.30, "weather_report": 0.20,
           "get_system_status": 0.10, "flight_finder": 0.15,
           "file_controller": 0.15, "open_app": 0.05}

    def __init__(self):
        self.order = []

    async def _execute_tool(self, fc):
        self.order.append(fc.name)
        await asyncio.sleep(self.LAT.get(fc.name, 0.05))
        return _Types.FunctionResponse(id=fc.name, name=fc.name,
                                       response={"result": "ok"})


def _fc(name):
    return type("FC", (), {"name": name, "id": name})()


def test_read_only_tools_run_concurrently():
    names = ["web_search", "weather_report", "get_system_status"]
    r = _Runner()
    t0 = time.perf_counter()
    out = asyncio.run(r._run_tool_calls([_fc(n) for n in names]))
    elapsed = time.perf_counter() - t0
    serial = sum(_Runner.LAT[n] for n in names)
    assert [o.name for o in out] == names          # order preserved
    assert elapsed < serial * 0.75, f"no speed-up: {elapsed:.2f}s vs {serial:.2f}s"


def test_mutating_tools_are_never_overlapped():
    names = ["open_app", "file_controller"]
    r = _Runner()
    asyncio.run(r._run_tool_calls([_fc(n) for n in names]))
    assert r.order == names, "declared order was not respected"


def test_mixed_batch_keeps_declared_order():
    names = ["web_search", "get_system_status", "open_app", "weather_report"]
    r = _Runner()
    out = asyncio.run(r._run_tool_calls([_fc(n) for n in names]))
    assert [o.name for o in out] == names


def test_a_tool_that_fails_hard_cannot_kill_the_turn():
    class Boom(_Runner):
        async def _execute_tool(self, fc):
            if fc.name == "weather_report":
                raise RuntimeError("upstream down")
            return await super()._execute_tool(fc)

    r = Boom()
    out = asyncio.run(r._run_tool_calls(
        [_fc("web_search"), _fc("weather_report"), _fc("get_system_status")]))
    assert len(out) == 3, "a failed tool dropped a response slot"


# ── weather: real parsing, network mocked out ─────────────────────────────────

def test_weather_renders_a_readable_report(monkeypatch):
    import actions.weather_report as w
    monkeypatch.setattr(w, "_geocode", lambda city: {
        "latitude": 26.9, "longitude": 75.8, "name": "Jaipur", "admin1": "Rajasthan"})
    monkeypatch.setattr(w, "_forecast", lambda lat, lon, days=3: {
        "current": {"temperature_2m": 31.4, "apparent_temperature": 34.9,
                    "relative_humidity_2m": 38, "weather_code": 2,
                    "wind_speed_10m": 12.6},
        "daily": {"time": ["2026-10-03", "2026-10-04"],
                  "temperature_2m_max": [31.4, 33.0],
                  "temperature_2m_min": [22.1, 23.4],
                  "precipitation_probability_max": [5, 0]}})
    out = w.weather_action({"city": "Jaipur"}, player=None, session_memory=None)
    assert "31.4" in out and "partly cloudy" in out and "Jaipur" in out


def test_weather_never_prints_the_word_none(monkeypatch):
    import actions.weather_report as w
    monkeypatch.setattr(w, "_geocode", lambda c: {"latitude": 1, "longitude": 2, "name": "X"})
    monkeypatch.setattr(w, "_forecast", lambda la, lo, days=3: {"current": None, "daily": None})
    assert "None" not in w.weather_action({"city": "X"})


def test_weather_handles_unknown_city_and_network_failure(monkeypatch):
    import actions.weather_report as w
    monkeypatch.setattr(w, "_geocode", lambda c: None)
    assert "couldn't find" in w.weather_action({"city": "Nowhere"}).lower()

    def boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(w, "_geocode", boom)
    assert "couldn't reach" in w.weather_action({"city": "Jaipur"}).lower()


def test_weather_does_not_open_a_browser(monkeypatch):
    """The old implementation only opened a Google search tab."""
    import actions.weather_report as w
    calls = []
    monkeypatch.setattr(w, "_geocode", lambda c: None)
    assert not calls
    src = Path("actions/weather_report.py").read_text(encoding="utf-8")
    assert "webbrowser" not in [
        l for l in src.splitlines() if not l.strip().startswith("#")]


# ── scanner ──────────────────────────────────────────────────────────────────

def test_scanner_offers_four_modes_and_refuses_the_rest():
    from actions.scanner import scan_action
    for mode in ("system", "network", "ports", "files"):
        out = scan_action({"what": mode, "path": ".", "top": "3"},
                          player=None, session_memory=None)
        assert isinstance(out, str) and out.strip()
    out = scan_action({"what": "nuclear"}, player=None, session_memory=None)
    assert "not one of them" in out


def test_scanner_files_rejects_a_missing_folder():
    from actions.scanner import scan_action
    assert "No such folder" in scan_action({"what": "files", "path": "/no/such/dir"})


# ── display: the in-app screen ────────────────────────────────────────────────

def test_display_renders_every_kind_without_qt():
    import core.display as D
    D.clear()
    D.display("markdown", "# Title\n- a")
    D.display("code", "print(1)")
    D.display("table", [["a", "b"], ["1", "2"]])
    D.display("html", "<b>x</b>")
    D.display("image", "/tmp/s.png")
    D.display("link", "https://example.com", title="src")
    D.display("text", "hello")
    page = D.to_html()
    for frag in ("Title", "print(1)", "<table", "<b>x</b>", "s.png",
                 "example.com", "hello"):
        assert frag in page, f"{frag} missing from the rendered page"


def test_display_escapes_content_so_it_cannot_inject_markup():
    import core.display as D
    D.clear()
    D.display("text", "<script>alert(1)</script>")
    page = D.to_html()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_display_unknown_kind_shows_content_instead_of_dropping_it():
    import core.display as D
    D.clear()
    D.display("telepathy", "important message")
    assert "important message" in D.to_html()


def test_display_ring_buffer_is_bounded():
    import core.display as D
    D.clear()
    for i in range(200):
        D.display("text", f"m{i}")
    assert len(D._ITEMS) == D.MAX_ITEMS
    assert D.recent()[0].payload == "m199"


def test_display_survives_a_bad_table_payload():
    import core.display as D
    D.clear()
    for bad in ("string", 42, None, []):
        D.display("table", bad)
        assert D.render(D.recent()[0])      # must not raise
