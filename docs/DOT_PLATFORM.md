# DOTS INSIDE JARVIS — specialist agents + workspace, integrated

> Status: authoritative design for bringing the "Dots" feature set INTO
> JARVIS — turning the Python voice assistant into a real-life agent.
> **There is no separate Dots product**: `dots/` is an internal engine
> package (like `dashboard/`), `actions/dots.py` + `actions/pages.py` are
> the voice/tool surface, the dashboard (JARVIS's one web server, one
> login) serves the workspace routes. Rule zero: **every component is
> free/self-hosted** (mapping table below).
> 
> Supersedes the earlier standalone framing (batch 6a's storage/blocks/
> approval engine is unchanged — only the surfaces moved into JARVIS).

---

## 0. What lands inside JARVIS

JARVIS (voice assistant) gains, as native capabilities:

1. **Specialist Dots** — persistent personas with own name, role
   instructions, permissions and SEPARATE conversations, created and
   talked to BY VOICE (`dots action=chat dot=Researcher …`).
2. **Spaces & Pages** — Notion-like workspace (nested pages, markdown +
   block JSON, autosave, honest revisions, history) reachable by voice
   (`pages action=…`) and served by the JARVIS dashboard for the
   visual editor + slash commands (UI batch).
3. **Human-in-the-loop review** — Dot edits arrive as proposals;
   approve/decline by voice (`dots action=approve`) or dashboard card.
   Stale proposals never overwrite (T3).
4. **Dot Computers** — per-Dot isolated persistent workspace with
   browser/files/shell, audit log, human takeover — built on JARVIS's
   existing sandbox/computer/browser stacks behind per-Dot permission
   gates (not a second computer stack).
5. **Web research** — Dot-permitted use of JARVIS's EXISTING
   `research`/`web_search`/`scrape` engines (parallel DDG, public page
   reader, cited sources) — duplicate-check: never rebuilt.
6. **Voice calls** — JARVIS IS the voice assistant (Gemini Live);
   call sessions add captions/timer/transcript + a background compute
   agent bound to the call.
7. **Slack** — mention → JARVIS Dot thread, workspace/user allowlists.
8. **Background/scheduled tasks** — recurring instructions on the
   server with pause/retry/cancel and a 90 s run cap, layered on
   JARVIS's scheduler patterns.
9. **Memory** — Dot-readable preferences with per-Dot allowlists,
   editable by the owner (alongside JARVIS's existing long-term memory).
10. **Automatic learning** — conversations mine reusable skill drafts;
    the owner publishes them (never auto-approved).

Reuse-first (duplicate-check done): research engines, browser control,
sandboxed terminal, rules/taskstore scheduling, Gemini voice, memory,
dashboard auth — all exist in JARVIS; Dots ADD permissions, proposals,
persistence and the workspace around them.

---

## 1. Free-only mapping (spec → what we actually build)

| Spec says | We build (free, self-hosted) | Why |
|---|---|---|
| LLM brain | `core/llm_client.py` local tool-calling loop (Ollama / OpenAI-compatible local servers) first; `core/gemini.py` free tier as cloud fallback, same seam | free-only rule; local-first = works offline |
| "OpenBot containers" for Dot Computers | Docker **if installed** (free engine) else local sandbox workspace (per-computer dir jail + timeouts + no network scope creep); one `Computer` interface either way | Docker must not be a hard requirement |
| "CopilotKit Intelligence" skills | Native skill miner: mine conversations → skill **draft** → human **publish** (never auto-approve) | CopilotKit cloud is not free/self-hosted; the spec's real requirement is review-gated learning |
| OpenAI Realtime voice calls | Provider interface; default = Gemini Live (already integrated, free tier) via server relay; OpenAI adapter only if user configures a key | free-only; seam makes both possible |
| Slack integration | Official Slack app (free) — events API + allowlisted workspaces/users; thread continuity persisted | Slack bots are free |
| Web search | DDG (`ddgs`) parallel fan-out (1–3 queries), `read_public_page` via requests+bs4 — same code paths JARVIS already ships | proven, keyless |
| Notion-like editor | Vanilla-JS single-page app, block JSON canonical + markdown source mode | no SaaS, no build toolchain |

---

## 2. High-Level Architecture

```
Voice / model tools — actions/dots.py, actions/pages.py (registry)
Browser (JARVIS dashboard UI: spaces tree, block editor + /, approval
         cards, call panel — UI batch)
   │  fetch/JSON (same origin, dashboard bearer auth)
API layer — dots/server.py (APIRouter) MOUNTED INTO
         dashboard/server.py `_build_app`: routes, approval gates
   │
   ├─ Brain      dots/brain.py  — dot conversation loop: role+memory+page
   │               context → LLM tool-calls → permission gate → tools →
   │               reply / pending_change (approvals)   [seams injectable]
   ├─ Tools      dots/tools_*.py — spaces, research, computer, learning
   ├─ Sandbox    dots/computer.py — per-dot workspace, browser/files/exec,
   │               audit log, takeover, start/stop persistence
   ├─ Scheduler  dots/scheduler.py — recurring instructions, 90 s cap,
   │               pause/retry/cancel
   ├─ Learning   dots/learning.py — skill drafts from conversations,
   │               human publish
   ├─ Slack      dots/slack.py — mention → thread, allowlists
   └─ Voice      dots/voice.py — WebRTC/signaling → provider (phased)
Storage — dots/db.py: ONE sqlite file `memory/dots.db` (gitignored),
          WAL, migrate-on-open
```

Data never leaves the machine except: LLM provider calls (user's choice),
web research fetches, Slack API when enabled.

---

## 3. LLDs

### 3.1 Data model (sqlite, `dots/db.py`)

```
dots(id, name UNIQUE, role_instructions, permissions_json, created_at, updated_at)
spaces(id, name, created_at)
pages(id, space_id, parent_id NULL, title, content_json, content_md,
      rev, created_by, created_at, updated_at)
page_revisions(page_id, rev, title, content_json, content_md,
               author 'owner'|'dot:<id>', note, created_at)
pending_changes(id, kind 'create'|'edit', page_id NULL, space_id,
                parent_id, dot_id, base_rev, title, content_json,
                content_md, reason, status 'pending'|'approved'|
                'declined'|'stale', created_at, decided_at)
messages(id, convo_key, role 'user'|'dot'|'system', content, meta_json,
         created_at)          -- convo_key: "dot:<id>" | "page:<id>" |
                               --           "slack:<chan>:<thread>" | "call:<id>"
preferences(id, key UNIQUE, value, allowed_json '*'|[dot names], timestamps)
computers(id, dot_id, status 'stopped'|'running', root_path,
          perms_json {browser,files,shell}, created_at)
computer_audit(id, computer_id, actor 'owner'|'agent', action, detail,
               ok, created_at)
tasks(id, name, instruction, every_seconds, dot_id, status 'active'|
      'paused'|'cancelled', next_run_at, last_run_at, created_at)
task_runs(id, task_id, started_at, finished_at, status 'ok'|'timeout'|
          'failed'|'cancelled', output, created_at)
skills(id, title, body_md, source_note, status 'draft'|'published'|
       'archived', created_at, published_at)
slack_threads(convo_key, channel, thread_ts, updated_at)
```

`conversations are separate per Dot and per Page` = `convo_key`
namespacing; no cross-reads unless a tool explicitly fetches.

### 3.2 Pages, revisions, autosave & conflict rule (T3/T4)

* Canonical content = `content_json` (block list: text/bullet/numbered/
  checklist/quote/code/divider/table/title). `content_md` is derived and
  also accepted on input (source mode) via `dots/blocks.py`
  (`blocks_to_md` / `md_to_blocks`, deterministic, tested).
* **Every save carries `base_rev`.** Owner save: `base_rev == page.rev`
  → apply (rev+1, write revision); else → conflict reply containing the
  current rev/content — a stale edit NEVER overwrites (spec requirement).
* **Dot writes are proposals**: `create_space_page`/`edit_space_page`
  insert a `pending_changes` row ONLY. The page is written when (and only
  when) the owner approves.
* Approve: `pending.base_rev == page.rev` → apply + revision (author
  `dot:<id>`), status `approved`; mismatch → status `stale`, nothing
  written. Decline → status `declined`, nothing written.
* Revision endpoint lists history; `GET .../revisions/{rev}` returns one
  (read-only; restore = owner save with that content + explicit base_rev).

### 3.3 Brain + tool router (`dots/brain.py`)

Loop (bounded, max 8 tool rounds): build messages =
  system(role_instructions + permission summary + skill list)
  + allowed preferences + (page context: title, md, rev) + history
  → LLM with tool schemas (`dots/tools/*.json` style dicts)
  → for each tool call: **permission gate first** (dot permissions,
  computer perms, space grants) → execute → append result → continue
  → final text reply stored in `messages`.
  * `edit_space_page`/`create_space_page` tool results = "proposed —
    pending approval #N" (the HITL card appears; nothing saved).
  * Refusals are honest strings (`denied: …`), never silent.
  * Seams: `_llm(messages, tools)` injectable — tests script exact
    tool-call sequences, zero network.
  * `review_space_page` in the spec's table = the OWNER-side approve/
    decline endpoints; Dots never approve their own work (self-approval
    is rejected server-side, T5).

### 3.4 Dot Computers (`dots/computer.py`)

* One workspace dir per computer: `memory/dots_pcs/<id>/` — persists
  across stop/start by construction (stop = kill procs + mark stopped;
  start = spawn browser profile + accept tools). Browser profile lives
  INSIDE the workspace (`profile/`), files obviously inside `work/`.
* **Files jail**: every path resolves via `Path.resolve()` and must be
  inside `work/` — `..`, symlinks pointing out, absolute escapes all
  rejected (`_jail` tested, T7).
* **Shell**: argv list only (no shell string), cwd = `work/`, timeout
  30 s default / hard cap 60 s (spec), output capped; executes only when
  perms.shell is on.
* **Browser**: playwright if installed (`sync_api` in an executor thread);
  navigate/snapshot/screenshot/click/type/key/scroll against the profile
  dir; honest refusal when playwright missing. Human takeover = owner
  calls the same endpoints directly → audit `actor='owner'`; agent calls →
  `actor='agent'`; every action appends `computer_audit` (T8).
* Stop/start + permissions toggle endpoints; per-tool gate reads
  `perms_json` live (change takes effect next call).

### 3.5 Research (`search_web`, `read_public_page`)

* `search_web(queries 1..3)` → ThreadPoolExecutor fan-out over DDG,
  deduped by URL, returns `{title,url,snippet}` (free, keyless).
* `read_public_page(url)` → http(s)-only, size cap, bs4→main text;
  returns text + links. `research_mode` config: `parallel` (default) |
  `browser` (page reader only) | `disabled` (both tools refuse honestly).
* Pages saved from research carry `sources` (links) in meta → rendered
  as a Sources section by the UI (spec: sources ke links).

### 3.6 Scheduler (`dots/scheduler.py`)

* Thread ticks every 5 s: tasks with `status='active'` and
  `next_run_at <= now` → spawn worker (daemon) → brain.run(instruction)
  with **hard 90 s deadline** (spec) → `task_runs` row (ok/timeout/
  failed) → `next_run_at += every_seconds` (skip missed windows — no
  thundering catch-up). `pause` stops future scheduling without losing
  state; `retry` re-runs the last failed run now; `cancel` flips status
  and aborts a live worker if its deadline hasn't fired (cooperative flag).
* Every run is visible (`GET /api/tasks/{id}/runs`).

### 3.7 Memory (preferences)

* `GET/PUT/DELETE /api/memory`; each pref has `allowed`: `*` or list of
  dot names. Brain injects only allowed prefs. Dots cannot write memory
  in v1 (owner-managed; spec says user edits/deletes) — a later batch can
  add `save_preference` proposals through the same approval gate.

### 3.8 Skill learning (`dots/learning.py`)

* Miner scans `messages` conversations for repeated successful patterns
  (≥3 occurrences of a task shape with `status ok` markers — deterministic
  heuristics, no LLM required for v1; LLM-assisted summariser optional
  seam) → INSERT `skills(status='draft')`.
* `POST /api/skills/{id}/publish` (owner only) → `published`.
  `GET /api/skills?status=` + review UI. **Nothing auto-publishes**
  (spec: "automatic approve nahi hote" — enforced, T9).
* Brain injects published skill titles+bodies into system context;
  tools `load_skill`/`read_skill_file` expose them to Dots (spec's
  `copilotkit_*` names, vendor prefix dropped — no CopilotKit).

### 3.9 Slack (`dots/slack.py`)

* `POST /api/slack/events` — URL-verification handshake + message
  events; only `{workspace_id ∈ allowlist, user_id ∈ allowlist}` are
  processed (else 200-ignore, logged). `@DotName mention` → resolve dot →
  message into `slack:<channel>:<thread_ts>` conversation → brain →
  reply into the SAME thread (`chat.postMessage` with `thread_ts`).
* Config in `config/dots_slack.json` (bot token + allowlists) — gitignored.

### 3.10 Voice calls (phased — last)

* Phase 1 (implemented with core): call session table + REST start/end,
  live timer, per-speaker captions (user/dot) stored as transcript
  messages under `call:<id>`, mute/minimize are UI-side, receipt =
  transcript download.
* Phase 2: audio path via provider interface — default Gemini Live
  (server-side websocket, browser mic via WebRTC or PCM frames), OpenAI
  Realtime adapter behind the same interface for users who configure it.
* Background compute agent during call = a task bound to the call's dot
  (scheduler machinery reused), shown in the call panel with live timer.

### 3.11 Auth

No new auth system: dots routes are `include_router`-ed with a
dependency running the dashboard's existing bearer-token `_auth`
(one login, one token store). Voice/tool access goes through JARVIS's
session (model tools are owner-directed). Slack allowlist + approval
gates protect agent writes (T10).

---

## 4. Cross-questions (asked & decided)

1. **Where does it live?** New package `dots/` in this repo — shares
   deps/keys/clients with JARVIS, ships in the same clone, independent
   entrypoint. Decided: no new repo, no framework.
2. **Canonical editor format?** Block JSON (matches slash-command UI);
   markdown is a view/import path. Decided: JSON canonical or source
   mode lossy-imports — never the reverse (keeps revisions honest).
3. **Do Dot creates also need approval?** Yes — a create is a write.
   Owner creates directly; Dots propose. Spec's HITL is about all saves.
4. **Docker dependency?** Optional. Local sandbox default so a fresh
   clone works; Docker path behind the same interface when present.
5. **Which brain by default?** Local tool-calling LLM (privacy, offline);
   Gemini free tier as fallback. Provider chosen by config, one seam.
6. **Self-approval?** Impossible by construction: approve/decline
   endpoints are owner-session only; brain has no such tool.
7. **90 s task limit** is a hard server-side deadline, not a prompt hint.

## 5. Failure modes → test matrix (grows per batch)

| # | Injected failure | Expected behaviour |
|---|---|---|
| T1 | save with stale `base_rev` | conflict reply, page untouched, current rev returned |
| T2 | dot calls `edit_space_page` | only a `pending_changes` row; page bytes unchanged |
| T3 | approve a proposal whose base_rev moved | status `stale`, page untouched, honest message |
| T4 | decline | status `declined`, nothing written |
| T5 | brain attempts approve (self-approval) | no such tool; endpoint rejects non-owner |
| T6 | files path escape (`../../etc/passwd`) | refused, audited, nothing read |
| T7 | exec > 60 s | killed at cap, `timeout` run row / honest error |
| T8 | agent vs owner actions | audit rows carry correct actor |
| T9 | miner proposes skill | stays `draft`; publish endpoint required |
| T10 | non-allowlisted Slack user | event acknowledged, nothing executed |
| T11 | no LLM configured | honest "no brain configured" reply, message stored |
| T12 | concurrent page edits | one wins, other gets conflict — never merged silently |

## 6. Build batches (in order)

* **6a** ✅ engine (storage, blocks, revisions, approvals, memory,
  conversations) + JARVIS integration: `dots/` → APIRouter mounted in
  the dashboard (auth-riding), `actions/dots.py` + `actions/pages.py`
  voice surface, standalone entry REMOVED
* **6b** ✅ brain tool-loop (`dots/brain.py`, ≤8 rounds, seam
  `(messages, tools)`; legacy `(system, hist)` kept) + router
  `dots/tools.py` (permission gate FIRST, honest `denied:`) + space
  tools (`dots/tools_spaces.py`: read/list + `create_space_page`/
  `edit_space_page` = pending-only proposals) + research tools
  (`dots/tools_research.py`: 1–3 query DDG fan-out over
  `actions/web_search`, `read_public_page` over `actions/scrape`;
  `research_mode` parallel|browser|disabled) + local-first default path
  (`core/llm_client` → gemini fallback) + **sources on pages**
  (proposal → approve → `sources_json`, owner saves never clobber) +
  voice `dot_create perms=` parsing
* **6c** ✅ Dot Computers (`dots/computer.py`): per-computer
  `memory/dots_pcs/<id>/work` jail + `profile/` persistence, `_jail`
  via resolve (T7), argv-only exec 30/60 s + output cap + stop kills
  live procs, browser = optional playwright on ONE worker thread per
  computer (honest refusal when missing), live perms toggles (read
  from the row every call), every op → `computer_audit` actor
  owner|agent (T8). Brain tools (`dots/tools_computer.py`, dot perms
  `computer`), owner HTTP takeover surface (13 routes: start/stop/
  perms/exec/files/browser/audit), voice `pc` action (shlex argv —
  never a shell)
* **6d** ✅ scheduler (`dots/scheduler.py`): lazy daemon ticker (5 s
  tick), runs on convo `task:<id>` with HARD 90 s deadline →
  `task_runs` (ok|timeout|failed|cancelled, honest failure prefixes),
  next_run skips missed windows (whole multiples, no catch-up),
  pause keeps state / resume / cancel aborts the live worker
  cooperatively (result discarded, never reschedules) / retry re-runs
  only failed|timeout runs; visibility `GET /api/tasks/{id}/runs`.
  Skill learning (`dots/learning.py`): deterministic miner (≥3
  same-shape user messages with completed dot replies, fingerprint in
  `source_note` → idempotent), drafts NEVER auto-publish (T9) —
  owner `POST /api/skills/{id}/publish|archive`; published-only
  injection into the brain prompt + tools `load_skill` /
  `read_skill_file` (jail `memory/dots_skills/`); optional
  `set_summariser` seam. Voice: `task_*` + `skill_*` on the `dots`
  action; auto-mine hook after each chat (drafts only)
* **6e** ✅ Slack (`dots/slack.py`): `POST /api/slack/events` —
  url_verification handshake; `evaluate()` gates FIRST (workspace ∪
  user allowlists, EMPTY list = deny-all, T10: rejected → 200 ack +
  log only, nothing executes); `@DotName` → convo
  `slack:<channel>:<thread_ts>` → brain → `chat.postMessage` back into
  the SAME thread (seams `_config`/`_post_slack`, config gitignored).
  Voice calls phase 1 (`dots/voice.py` + `calls` table): start/end +
  live timer, per-speaker captions under `call:<id>` (user captions
  generate the Dot's reply with optional page context), transcript
  receipt (json list / text download), background agent = scheduler
  task bound to the call's Dot, provider seam (`set_provider`, honest
  `captions-only` without phase-2 audio). Mute/minimize = UI-side (6f).
  Voice: `call_start|call_list|call_end|call_caption|call_transcript|
  call_background` on the `dots` action
* **6f** ✅ full dashboard UI redesign (`dashboard/static/`): shared
  design system `css/jarvis.css` (dark indigo tokens, rail nav, panes,
  tree/blocks/slash/approval/call components) + shell `app.html`
  (ported core intact: session redirect, AES-256-CBC, WS handlers,
  `showTab`/`_renderSurface`/`_onContent`/`_onMotion`, upload XHR,
  mic AudioWorklet → `/ws/phone-audio`, mirror + camera) with an
  11-section rail; JS modules register via `window.JV` —
  `workspaces.js` (spaces tree, page gallery, block editor with `/`
  slash menu + source mode + debounced autosave, 409 conflict banner
  that never overwrites, revisions restore, approval cards, per-page
  Dot chat), `agents.js` (Dots CRUD + conversations/dot chat +
  permission grants + owner memory), `computers.js` (jail-scoped file
  browser with image preview/edit, argv-only shell, agentic browser
  ops, perms toggles, audit trail), `calls.js` (live timer, speech →
  captions → Dot reply, transcript download, background agent),
  `agenda.js` (tasks pause/resume/cancel/retry + runs, skill mining →
  publish/archive)
