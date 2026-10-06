# Changelog

Notable changes, newest first. The version lives in one place —
`core/version.py` — and `python main.py --version` prints it.

Entries before 1.4.0 are summaries of the update sections in
[`README.md`](README.md), not a commit-by-commit record: the project shipped
those as "Marks" (LII → LV) with their reasoning written up there. From 1.4.0 on,
entries are written as changes land.

## Unreleased

**One cap on tool results.** The caps inside the tools ranged from 2,500 to
40,000 characters and a number of tools had none at all — `file_processor` on a
large document, `data_query` over a wide table. One such call could spend most
of a session's context, and the symptom was not an error: the assistant simply
started forgetting what had been said a minute earlier. There is now a single
backstop where every result passes (`core/tool_output.py`, 40,000 characters,
about 10k tokens), and it is applied at the dispatch point so a tool added
tomorrow cannot forget it. When a result is trimmed, the model is told how much
was dropped and what to ask for instead, and so is the user — a model handed a
silently shortened document summarises the part it got and says nothing about
the rest.

**A report for the silent `except: pass` handlers.** There are 501 of them, and
most are correct: a teardown path must not raise a second exception while
handling the first. `tools/silent_except_audit.py` sorts them into 114 with a
comment explaining the silence, 50 on teardown/probe-shaped paths, and 337 with
no log and no comment on a feature-shaped path — the number that should come
down (`make silent`, and CI prints it). High-value spots were fixed first: if
the autonomy gate cannot answer, the call still proceeds (a corrupt config must
not brick every tool) but it now logs at error level and lands in the audit
chain, because "the gate let something through" should be a fact you can look up.

**`python main.py --doctor --fix`.** `core/installer.py` was 200 lines of
unreachable code whose docstring claimed it ran "automatically on first launch" —
nothing called it, so the packages offline transcription needs were installed by
nothing while the README promised a Meeting Recorder that transcribes with
faster-whisper. The doctor already reports exactly what is missing, so installing
happens there now, when the user asks for it, with their own dependency table
(`paho-mqtt` installs `paho.mqtt`, not `paho_mqtt`) and honest failure output.

## 1.4.0 — the audit release

A full line-by-line pass over the repository, [`PROJECT_ANALYSIS.md`](PROJECT_ANALYSIS.md),
turned into fixes. The pattern in almost every one: a failure that was *silent* —
no error, no log, nothing to grep for.

**Security**

- `revoke_devices` now clears the token store as well as the sessions. Revoking
  from the phone left the tokens valid, so a stolen one kept working.
- Dashboard tokens carry a TTL and are swept. They never expired before.
- The dashboard's login and PIN paths compare in constant time.

**Bugs found and fixed**

- The display screen never opened. `ui` was a module, so
  `from ui.display_panel import DisplayPanel` raised `'ui' is not a package`,
  the panel swallowed the exception, and the screen stayed empty with no error
  anywhere. `ui` is a package now and the submodule import is the fix; the
  by-path loader that worked around it is gone.
- `actions/send_message.py` referenced a `plugins/_whatsapp_core.py` that was not
  in the repository. WhatsApp linking is a real feature, and it was dead.
- Every `subprocess` call in `actions/computer_settings.py` now passes a
  `timeout=`: a hung `pactl`/`brightnessctl` used to hang the tool.
- README numbers that disagreed with the code (tool count, memory budget) were
  corrected.

**Structure — one place for one job**

- `core/paths.py` is the only place that derives the app root and reads an API
  key. Eighteen modules re-derived the root themselves; ten defined their own
  `_get_api_key`, eight of which were dead code. Each of the surviving local
  helpers now delegates, because ~125 call sites and the tests reach them by
  module name.
- `actions/` and `tools/` are packages.
- `ui/` — `ui.py` became `ui/app.py` inside a real package, with lazy exports so
  `import ui` no longer drags in Qt.
- `main.py` imports Qt and sounddevice where they are used. `--version` works on
  a machine with no PortAudio and no GL stack; a fake `ui` module no longer has
  to be installed before `import main`.

**Operations**

- `python main.py --version` / `--doctor` / `--diagnostics`, and
  **EXPORT DIAGNOSTICS** in the HUD's controls drawer: a zip of the log, the
  redacted config and the audit tail — the thing to attach to a bug report.
- `core/logging_setup.py`: a rotating log under `~/.jarvis/logs`
  (`JARVIS_LOG_DIR` overrides), a print mirror so the console output is
  unchanged while the transcript becomes complete, an excepthook that records
  crashes and still calls the previous hook, and `redact()` so nothing
  secret-shaped leaves in a bundle.
- Core error paths log at the right level with the module name instead of
  printing a bracketed prefix.
- `setup.py` → `bootstrap.py`. It is a dependency installer, not a packaging
  script, and the old name collided with the one filename every Python tool
  treats as a build script.

**Tests**

- 1,273 passing (was 1,209 at the start of the audit). New suites for the
  logging/diagnostics path, the `ui` package, and the app-root/key-reader
  dedupe. Writing them found three real defects: diagnostics exported an empty
  log when `JARVIS_LOG_DIR` was set, a second `setup()` left the print mirror
  on the old file, and the HUD's new button used an icon name that did not exist
  (`set_icon` swallows unknown names, so the button would have come up bare).

## 1.3.0 — Mark LV, the Agentic Update

Mission Control timeline, the agentic task engine, rules and automation, macro
recorder, scanner modes, window layouts, clipboard history, scrape, research
reports, SVG charts and diagrams, focus sessions, process manager, screen
mirror, git snapshots after `dev_agent`, reminder list/cancel.

## 1.2.0 — the HUD v2

The desktop app rebuilt on one design system: vector icons, the two-drawer
layout, the live dashboard panels, video on the avatar's surface, the wake-word
state check moved off the UI thread.

## 1.1.0 — the Integration Update

Everything wired in: the local LLM stack, memory that records and recalls, the
phone dashboard, presence detection, privacy mode, the confirm gate and the undo
journal.

## 1.0.0 — the Foundation Update (back-ported to every Mark from LII)

The floor the project stands on: memory storage separated from the prompt
budget with `recall_memory` over the full store, per-OS audio device
resolution, no hardcoded language or OS assumptions, no bundled assets
required to boot.
