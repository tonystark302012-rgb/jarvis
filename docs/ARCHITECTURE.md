# JARVIS — Agent Architecture (HLD + LLD)

> Status: living design document for the "voice assistant → AI agent" transition.
> **Delivery (batch 4, all shipped on `arena/01a101af-jarvis`):**
> 0 architecture (this doc) + 1 replan brain + 2 cooperative cancel + 3 autonomy
> modes = `833c8cf`; 4 MCP native flatten = `2ed0624`; 5 GUI agent (§2.5,
> T13–T16 green) = `a870a21`; 6 self-mining rules suggest (§2.6, T17–T18 green)
> = `5eb257b`. **Batch 5 (pending-list close-out)**: identity rebrand;
> rules interval triggers + suggest mapping fix; taskstore boot sweep;
> history recency ranking; browser agent on runtime keys; gui_agent
> local-VLM provider (Ollama); MCP Streamable HTTP; world_view (NASA
> GIBS + OSM); screen_mirror as a thin tool over the EXISTING dashboard
> mirror (duplicate rejected — see §2.8). Gates at final: **693/693**,
> ruff clean, mypy clean on the curated module list (127 of 160 modules), 50 actions discovered.
> §4 backlog below now holds only what is deliberately still open.
> Everything here maps to real code paths (file:line cited where it matters).
> Nothing in this document is a prototype: each subsystem ships implemented
> and tested in the same change that introduces it.

---

## 1. High-Level Architecture

### 1.1 As-is planes (what already exists)

```
┌──────────────────────────────────────────────────────────────────────┐
│ PRESENTATION                                                         │
│   ui/ (PyQt HUD)    ·  dashboard/ (phone: FastAPI + WS)  ·  voice    │
├──────────────────────────────────────────────────────────────────────┤
│ SESSION                                                               │
│   main.py — Gemini Live session, audio I/O, barge-in (EchoGuard),     │
│   presence gate, proactive loop, event engine, rules tick (30s)       │
├──────────────────────────────────────────────────────────────────────┤
│ TOOL PLANE                                                            │
│   ActionRegistry (core/action_loader) — 76 bundled tools (TOOL dicts) │
│   PluginRegistry (core/plugin_loader) — gmail, calendar              │
│   ToolTiers (core/tool_tiers) — 21 declared + `toolbox` router,      │
│     ~55 deferred; measured 23.4k → 10.6k tokens per connection       │
│     (`tools/count_tools.py` regenerates those numbers)               │
│   confirm (unforgeable UI token) · undo journal · privacy gate       │
├──────────────────────────────────────────────────────────────────────┤
│ AGENT PLANE (today: three bespoke loops)                              │
│   task_agent  → episode_memory.lessons_for(goal) → planner LLM        │
│              → core/orchestrator (sequential steps)                   │
│   browser agent → snapshot→reason→act→verify (browser_control)       │
│   multi_agent → planner→coder→tester (dry-run diffs)                 │
├──────────────────────────────────────────────────────────────────────┤
│ PERSISTENCE & SIGNALS                                                 │
│   taskstore (sqlite runs) · history_search (sqlite FTS) · memory     │
│   episode_memory (reads taskstore runs → planner lessons; no store   │
│     of its own — delete a run and its lesson goes with it)           │
│   activity (Mission Control timeline) · events bus · rules JSON      │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.2 To-be planes (what THIS design adds)

```
┌──────────────────────────────────────────────────────────────────────┐
│ TOOL PLANE  (upgraded)                                                │
│   + MCP NATIVE FLATTEN — server tools appear as mcp__srv__tool       │
│     function declarations in the Live session (first-class)          │
├──────────────────────────────────────────────────────────────────────┤
│ AGENT PLANE  (unified semantics)                                      │
│   + REPLAN BRAIN — planner ⇄ executor loop: reflect on failure,      │
│     re-plan remaining steps (bounded), clarify ambiguous goals        │
│   + COOPERATIVE CANCEL — core/agent_runtime cancel registry,          │
│     checked at step boundaries; works because tool calls dispatch     │
│     via asyncio.gather (parallel) — main.py:1313                     │
│   + GUI AGENT — perceive (screen) → reason (Gemini SMART+image) →     │
│     act (pyautogui primitives) → verify; preview mode, cancel,        │
│     privacy-gated (screenshots are content)                          │
├──────────────────────────────────────────────────────────────────────┤
│ POLICY PLANE  (new)                                                   │
│   + core/autonomy — observe | ask | auto modes with TTL, consulted    │
│     at ONE interface choke point (_execute_tool) + task_agent start; │
│     never-auto set is absolute (shutdown, send_message, vault, …)    │
│   confirm token design unchanged (it stays the only forge-proof gate)│
├──────────────────────────────────────────────────────────────────────┤
│ SELF-IMPROVEMENT  (new)                                               │
│   + rules action=suggest — mines history turns + taskstore runs for   │
│     recurring patterns → proposes exact rules (never auto-installs)  │
└──────────────────────────────────────────────────────────────────────┘
```

### 1.3 Design principles (the "brain" rules)

1. **One choke point per concern.** Presence → `_fire_phrase_rules`. Privacy →
   `privacy.gate()` first statement. Autonomy → `_execute_tool` entry. If a
   policy has two entry points it has none.
2. **Loops are cooperative, never preemptive.** Cancel/replan check at step
   boundaries; a running terminal command finishes its timeout. No thread
   kills, no partial writes without a report.
3. **Bounded everything.** Replans ≤ 2, total steps ≤ 16, GUI steps ≤ 15,
   cancel registry entries expire when a run ends. An agent that can loop
   forever is a bug, not a feature.
4. **Honest degradation.** No LLM → plan says so; no playwright → browser
   says so; privacy on → screenshot tools refuse before capturing. Failures
   are strings the user can act on, never silent no-ops.
5. **Interface-issued authority, model-never-issued.** The model cannot type
   `confirm=yes` and be believed (core/confirm docstring), cannot forge
   autonomy mode (only the `autonomy` tool + UI set it), and cannot escalate
   `allow_destructive` in observe mode (mode gate runs before dispatch).

---

## 2. Subsystem LLDs

### 2.1 Autonomy modes — `core/autonomy.py` + `actions/autonomy.py`

**State** (persisted in config/api_keys.json, same file as privacy_mode):

```json
{ "autonomy_mode": "ask", "autonomy_expires": 1770000000.0 }
```

**API**

```python
MODES = ("observe", "ask", "auto")
def get_mode(now: float | None = None) -> str
    # expired TTL → falls back to "ask" (safe default)
def set_mode(mode: str, minutes: float | None = None) -> dict
def gate(tool: str, args: dict) -> str | None
    # observe + mutating(tool, args) → honest refusal string; else None
def enhancing(tool: str, args: dict) -> dict
    # auto mode ONLY: returns args with interface-level confirm=yes /
    # allow_destructive injected for the UNDOABLE set; returns args
    # unchanged for everything else (incl. NEVER_AUTO).
def mutating(tool: str, args: dict) -> bool
```

**Classification tables**

| Set | Members | Rule |
|---|---|---|
| `NEVER_AUTO` | `shutdown_jarvis`, `send_message`, `vault`, `procman` (kill/end), `computer_settings` (power/shutdown/restart), `macro` (replay) | observe blocks, auto does NOT enhance — always needs the existing gate |
| `UNDOABLE` (auto-enhance) | `file_controller` write ops (undo journal), `desktop_control` task=clean (organize undo), `task_agent` (allow_destructive injection) | auto may proceed unattended |
| `MUTATING` (observe blocks) | `orchestrator.DESTRUCTIVE` + `_DESTRUCTIVE_ARGS` + `send_message` + `smart_home` pub + `terminal` write verbs + `rules` add/remove | observe = read-only agent |
| everything else | — | read: always allowed |

**Why one choke point works:** `main._execute_tool` is the only path a model
tool call takes (special-cases → `elif registry.has` → plugin). Rules,
orchestrator and the GUI agent dispatch through `registry.run` *directly* —
so they additionally consult `autonomy.gate` at their own loop head
(task_agent start; gui_agent before each act). Two callers, same module.

**Cross-questions**

- *Q: Doesn't auto re-open the forge hole confirm.py closed?*
  A: No. The hole was **the model** writing `confirmed=yes`. Here the
  **interface** (`_execute_tool`, running in main, not a tool) injects the
  param after checking the persisted mode the **user** set via the
  `autonomy` tool/UI. NEVER_AUTO is not injectable under any mode.
- *Q: TTL expires mid-run?*
  A: Mode is read per-dispatch, not cached per-run; a run's later steps
  degrade to `ask` semantics. Documented, tested (`now` injected).
- *Q: What about rules firing while observe is on?*
  A: Rules run via `registry.run` (not `_execute_tool`) → they consult
  `autonomy.gate` through the runner installed by main (one line in the
  `_agent_runner`-style wrapper). A rule whose tool mutates while observe
  is on reports the refusal honestly in `rules action=health`.

### 2.2 Replan brain — upgrades to `actions/task_agent.py` + `core/orchestrator.py`

**Orchestrator (additive, existing tests keep passing):**

```python
def run_task(..., should_stop: Callable[[], bool] | None = None) -> TaskReport
    # checked at each step boundary → report.cancelled = True
@dataclass TaskReport: cancelled: bool = False   # new field, default False
def replan_prompt(goal, done, failed_step, failed_result, tool_names) -> str
    # returns instructions to emit ONLY the REMAINING steps as JSON array
```

**Planner reply protocol (task_agent):**

```
goal → gemini.call(planner_prompt)
     ├─ {"clarify": "one specific question"}  → return [NEEDS CLARIFY] text;
     │    the model asks the user, re-calls with description+answer
     │    (stateless — no schema change, no stale rows)
     └─ [steps...]                            → execute
```

**Execution loop:**

```
remaining = plan[:max_steps]
chunks = 0
while remaining:
    report = run_task(remaining, should_stop=cancel_check)
    merge results into taskstore (existing merge pattern)
    if report.cancelled      → status=cancelled, break
    if report.ok             → status=done, break
    if replans < 2 and failure is a tool error (not destructive-refusal):
        remaining = _replan_llm(goal, merged_results, failed_step)
        if not remaining    → fall through to partial/failed
        replans += 1; continue
    status = partial|failed (existing rules), break
```

**Budgets:** `max_replans=2` (param, clamped 0–3); cumulative executed steps
≤ `max_steps * 2 + 2` (≤ 18 worst case) — a bound the report prints.

**Cross-questions**

- *Q: Infinite ping-pong between two plans?*
  A: replans hard-capped at 2 and each replan only receives the **remaining**
  work; identical-plan detection: if replan output equals the remaining old
  plan (tool+args sequence), we stop and report honestly instead of looping.
- *Q: Destructive-refusal — replan or stop?*
  A: STOP. A refusal is a policy boundary, not a technical failure;
  re-planning around a "no" is exactly the behaviour that must not exist.
  Replan triggers only on exceptions / non-zero tool failures.
- *Q: Crash between chunks?*
  A: taskstore is updated after every chunk (not just at the end) —
  kill -9 leaves `running`→ next app start can mark stale `running` as
  `partial` … v1 keeps existing semantics: resume treats it via
  `_pending_indices`; adding stale-recovery sweep is listed in §4.
- *Q: Parallel `gather` cancels?*
  A: cancel tool call arrives on a parallel gather slot, sets the flag;
  the executor thread sees it at next boundary (≤ one step latency).

### 2.3 Cooperative cancel — `core/agent_runtime.py`

```python
begin(key: str) -> None            # key = str(run_id) | "gui"
cancel(key: str) -> bool           # request flag (idempotent)
is_cancelled(key: str) -> bool     # polled by executors
finish(key: str) -> None           # clears flag + entry
active() -> set[str]               # for `task_agent action=cancel` listing
```

Thread-safe (module lock), entries removed on `finish` (bounded memory).
**task_agent wiring:** `begin(str(run_id))` before the loop; `should_stop =
lambda: is_cancelled(str(run_id))`; `finally: finish(...)`. New action
`cancel` — with id → cancel that run; without id → cancel the single active
run (error if ambiguous/none). taskstore status `cancelled` added to
`resumable()` (resume = continue pending steps, same as partial).

**Cross-questions**

- *Q: Cancel arrives after last step?*
  A: flag consumed at `finish` — `cancel` on a finished run returns
  "already finished", no status flip on `done` runs.
- *Q: Two runs at once?*
  A: keys are per-run ids; cancel without id refuses when >1 active
  (explicitness over guesswork).

### 2.4 MCP native flatten — `actions/mcp.py` + main wiring

```python
def native_declarations() -> list[dict]
    # per configured server (cache TTL 300s already in _list_tools):
    #   {"name": "mcp__{srv}__{tool}", "description": …, "parameters": schema}
    # sanitise [^A-Za-z0-9_] → "_" ; collisions get _2/_3 suffixes
    # unreachable server → skipped (appears in `mcp list` as unreachable)
def call_native(full_name: str, args: dict) -> str
    # index lookup (rebuilt if stale) → _session(_server_argv(server))
    # → tools/call → concatenate TextContent blocks → honest error strings
```

main.py wiring (both source-indexed in tests, since main isn't importable
without PyQt):

* `_build_config`: `_all_decls = base + actions + plugins + native_declarations()`,
  then `core/tool_tiers.split_declarations()` keeps the core tier and the
  `toolbox` router and stores the rest on `self._deferred_decls` for the router
  to search. The Live API fixes its tool list when the socket opens (no
  `update_tools` on `AsyncSession`), so a tool that is not declared here can
  only ever be reached through the router.
* `_execute_tool`: new `elif name.startswith("mcp__"):` **before** the
  registry check → `await loop.run_in_executor(call_native, …)`.
* `_execute_tool`: `toolbox` is intercepted at the top and, for `action=run`,
  **re-enters `_execute_tool`** with the real tool name. Routing is therefore
  not a second dispatch path — the autonomy gate, confirm gate, activity
  timeline and audit chain see the real tool exactly as they do for a direct
  call (`tests/test_tool_tiers.py::TestRoutedCallsAreNotUnsandboxed`).

**Cross-questions**

- *Q: Session start latency — spawning N servers at config build?*
  A: `initialize` handshake per server at declaration time; timeouts are the
  existing `_DEFAULT_TIMEOUT`, unreachable servers skip fast (connection
  refused). Lazy: cache makes subsequent rebuilds (reconnect) cheap.
- *Q: Schema shape mismatch (JSON-schema lowercase vs OBJECT uppercase)?*
  A: MCP schemas pass through **unchanged** — the Live API accepts standard
  JSON schema; we only upper-case for our own action validation, not here.
- *Q: Model calls mcp tool when server died since boot?*
  A: `call_native` re-spawns on demand (session is per-call by design) —
  failure returns the stdio error honestly; `mcp list` shows status.
- *Q: Name collision `mcp__a__b` vs a native tool literally named that?*
  A: native action names are identifiers without `__` prefix patterns from
  users; registry tools are checked first only if `registry.has` — we place
  the `mcp__` branch first, and `mcp__` is reserved (loader `reserved_names`
  unaffected since this isn't a file-based action).

### 2.5 GUI agent — `actions/gui_agent.py`

**Perception → Decision → Act loop (one tool call = one bounded run):**

```
gui_agent(goal, max_steps≤15, confirm="yes"|preview…)
  privacy.gate("gui_agent")            # screenshots are user content
  observe mode / no confirm → PREVIEW: 1 screenshot → one decide call with
      plan_only → returns intended first actions, NOTHING executed
  else:
    loop i in 1..max_steps:
        if agent_runtime.is_cancelled("gui"): break honestly
        img = screen_processor._capture_screen()      # real capture
        decision = _decide(img, goal, history[-5:])   # Gemini SMART, JSON
        act  → pyautogui primitives from computer_control
               (_click/_press/_type/_scroll/_hotkey) — imported directly
        history.append(decision)
        decision.type == done/fail → break
    report: steps taken + final state → show_content("GUI AGENT — goal", …)
```

**Decision contract (strict JSON, fence-tolerant parse, one retry on
garbage then honest fail):**

```json
{"say": "…", "act": {"type": "click|x|y | key|k | type|text | scroll|dir |
 down| hotkey|keys | done|proof | fail|reason"}}
```

**Image path:** `{"inline_data": {"mime_type": "image/jpeg", "data": b64}}`
dict — `core/gemini._to_live_parts` accepts dicts as-is (line ~368).

**Cross-questions**

- *Q: Privacy? Screenshots to cloud.*
  A: `gui_agent` ∈ CLOUD_TOOLS — gate runs BEFORE capture; honest refusal.
  (Local-VLM via Ollama chat `images[]` is the designated follow-up; not
  faked here — provider seam `_decide` is where it plugs in.)
- *Q: `type` can type into any window (bank page?)*
  A: three-layer: (1) confirm=yes required unless autonomy=auto+UNDOABLE
  path — actually gui act is never in NEVER_AUTO… wait: gui typing is
  MUTATING → observe blocks; ask mode → needs confirm=yes param (model-
  written, so treated like macro replay: gated + capped steps + preview);
  (2) step budget 15; (3) every act logged to history shown in the report;
  (4) cancellation by voice between steps.
- *Q: pyautogui/cv2/mss missing (CI)?*
  A: optional imports; every test injects `_capture` + `_decide` + act
  fakes — same pattern as region_ocr/browser. Real path needs a display.
- *Q: Clicks keep missing (resolution change, animation)?*
  A: loop re-perceives every step (no blind chains), `fail` after 2
  identical consecutive decisions (stuck detector), report says so.

### 2.6 Rule mining — `rules action=suggest`

**Sources (both persisted, both already tested seams):**

* `history_search` turns (ts, speaker, text) — detector A: the user asks the
  same normalized question ≥3 times on ≥3 distinct days → propose
  `phrase` rule (keyword from the question head) or a `time` rule if the
  ask times cluster (hour histogram).
* `taskstore` runs (goal, plan_json, created) — detector B: the same tool
  appears in plans of ≥3 runs whose start hour differs by ≤1 → propose
  `time HH:MM → that tool`.

**Output contract:** human-readable suggestions, each with the exact
`rules action=add …` arguments. **Never auto-installs** — installing a rule
is the user's (or an explicit model call after asking) decision.

**Cross-questions:** false positives (≥3 threshold + distinct days), privacy
(mining is local-only; `suggest` ∈ read tools, allowed under privacy),
stale patterns (7-day window on history queries).

### 2.7 Future-proofing (what this architecture enables next)

| Future feature | Lands on |
|---|---|
| ~~Local-VLM GUI decisions~~ | **shipped** — `gui_provider=ollama` routes `_decide`; §2.8 documents the keys |
| ~~Browser agent onto shared runtime~~ | **shipped** — `agent_runtime` key `browser`, boundary cancel, `action=cancel` |
| Rules running goals (not just tools) | rules runner already dispatches any registered tool → `task_agent(goal)` composable |
| A2A / multi-host agents | tool plane is transport-agnostic (MCP native shows the pattern: declare → dispatch) |
| ~~Stale-`running` sweep | **shipped** — `taskstore.sweep_stale()` at boot (main runner) |
| Autonomy UI toggle | `actions/autonomy` state readable by settings drawer (same pattern as privacy status) — still open |

---

### 2.8 Batch-5 extensions

**MCP Streamable HTTP (`_HttpClient`)** — same interface as the stdio
client; POST with `Accept: application/json, text/event-stream`;
parses single-JSON **or** SSE answers; captures `Mcp-Session-Id` on
initialize and echoes it; 404 → one transparent re-handshake + retry;
close() sends the spec's DELETE. `_connect(name)` dispatches on the
config entry (`url` → HTTP, `command` → stdio), so `_list_tools`,
`action=call` and `call_native` are transport-agnostic. Handshake uses
`protocolVersion 2025-06-18` on HTTP, keeps `2024-11-05` on stdio.

**Rules interval triggers** — `trigger {type: interval, value: 30m|2h|
1d|1w|<minutes>}`; validated at add-time; `_due_interval` arms on the
first tick (file-trigger contract), persists `last_fired` so restarts
don't reset the clock, fires exactly once per period.

**Boot sweep** — `taskstore.sweep_stale(max_age_hours=24)` flips runs
whose `updated` is older than the cutoff from `running` → `cancelled`
(resumable). Called once in `main().runner` before `JarvisLive`; plan
and per-step results untouched (index-based pending still converges).

**History recency ranking** — `search()` fetches a bm25 candidate pool,
re-ranks with `rel × (0.5 + 0.5·e^(−age/30d))` in Python, ties break
newest-first, limit applied after the re-rank.

**gui_agent local VLM (provider docs)** — config keys in
`config/api_keys.json`:
`gui_provider` = `gemini` (default) | `ollama`; `gui_vlm_model`
(default `qwen2.5vl`); `ollama_url` (default `http://127.0.0.1:11434`).
Local setup: install from ollama.com → `ollama serve` →
`ollama pull qwen2.5vl` (any Ollama vision model works — set
`gui_vlm_model`). Traffic never leaves the machine; failures land in
`_LAST_DECIDE_ERR` and append to the run/preview report.

**world_view** — satellite = NASA EOSDIS GIBS VIIRS true colour
(free, no key, zoom ≤ 8, walks back ≤ 5 granule days), map = OSM
tiles (zoom ≤ 19, ≤16 tiles/ask, identifying UA). `world_view` ∈
CLOUD_TOOLS with `gate()` as the first handler statement.

**screen_mirror = adapter, not implementation** — the mirror itself is
`dashboard/server.py`'s websocket stream (shipped first, tests intact).
Duplicate check rejected the MJPEG re-implementation; the tool only
adds `mirror_set_sync` (run_coroutine_threadsafe onto the dashboard
loop) + `ACTIVE` handle + `mirror_on`. A source-index test bans any
capture stack from reappearing in `actions/screen_mirror.py`.

## 3. Failure modes → test matrix

| # | Injected failure | Expected behaviour | Test level |
|---|---|---|---|
| T1 | planner LLM returns garbage / empty | honest "could not build a plan", no run created | unit (scripted gemini.call) |
| T2 | planner returns `{"clarify":…}` | `[NEEDS CLARIFY]` text; no steps run; second call with answer plans | unit |
| T3 | step 2/4 throws | replan #1 with remaining; if new plan ok → run continues; report shows both chunks | unit (scripted sequence) |
| T4 | replan returns same plan / garbage | stop, status partial/failed, resume hint — never loops | unit |
| T5 | destructive step in plan, no allow | refused in report, NO replan (policy ≠ failure) | unit |
| T6 | cancel flag set before step 3 | steps 1-2 run, step 3 not, `cancelled` status, resume still possible | unit |
| T7 | cancel on finished run | "already finished", status untouched | unit |
| T8 | autonomy TTL expired mid-session | mode falls back to ask | unit (`now` injected) |
| T9 | observe + mutating tool | refusal string at `_execute_tool` gate (source-index) + unit gate | both |
| T10 | auto + NEVER_AUTO tool | NOT enhanced (args unchanged) | unit |
| T11 | MCP server dies after declare | call_native re-spawns; error honest; native list skips unreachable | integration (real subprocess fake server) |
| T12 | MCP name sanitisation collision | unique suffixed names, index resolves both | unit |
| T13 | GUI decide returns prose not JSON | retry once → `fail` decision, report honest, 0 acts executed | unit |
| T14 | GUI capture raises (no display) | immediate honest failure, no loop | unit |
| T15 | GUI stuck (same decision twice) | stop with "stuck" reason ≤ budget | unit |
| T16 | privacy on + gui_agent | refusal before capture (capture fake asserts not called) | unit |
| T17 | suggest on empty history/runs | "no patterns yet" — no fake proposals | unit |
| T18 | suggest thresholds not met (2 asks) | nothing proposed | unit |
| T19 | parallel gather: cancel during run | flag honoured at next boundary | unit (fake runner sleeps steps) |
| T20 | registry invariant | every handler still accepts `parameters` kwarg; 46→47→48 tools discover | existing suite |

---

## 4. Backlog (what remains deliberately open)

Batch 4/5 cleared the previous backlog (sweep, VLM provider, interval
triggers, browser runtime migration, recency ranking, MCP Streamable
HTTP, satellite/map view, screen-mirror reach — all shipped with
tests). What is honestly still open:

* Autonomy UI toggle (settings drawer reads `core/autonomy` state)
* Extra local-VLM providers (LM Studio / llama.cpp vision endpoints) —
  Ollama landed; the seam + config keys are in §2.8
* Higher-zoom satellite (GIBS caps at z8; anything closer needs a
  non-free tile source — deliberately not added under free-only)
* A2A / multi-host agents
* Rules running full `task_agent` goals on interval triggers
  (composition exists; confirm-gating policy to design first)

## 5. Change-management rules

1. Additive dataclass fields only (`cancelled: bool = False`) — old tests
   must pass untouched.
2. Every new public function gets failure-path tests, not just happy path.
3. main.py changes are paired with source-index tests (PyQt absent in CI).
4. No new required dependencies. Optional imports degrade with instructions.
5. LLM-shaped seams (`planner`, `replan`, `_decide`) are injectable callables —
   tests script exact JSON, never the network.
