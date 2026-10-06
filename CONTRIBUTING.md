# Contributing

Thanks for wanting to help. This file is the short version of how the project is
built and what a change is expected to look like when it arrives. The long
version — an audit of the whole codebase, file by file, with the reasoning behind
each decision — is in [`PROJECT_ANALYSIS.md`](PROJECT_ANALYSIS.md). The shape of
the system is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Getting set up

```bash
git clone <your fork>
cd jarvis

python bootstrap.py                      # deps for YOUR OS (+ Playwright browsers)
python -m venv .venv                     # contributors: keep deps out of your system Python
.venv/bin/pip install -r requirements-dev.txt

make test                                # the whole suite
make lint                                # ruff
python main.py --doctor                  # is this machine ready to run it
```

`make help` lists every target. `python -m pytest tests/ -q` is exactly what CI
runs; if that is green locally it should be green in CI.

You do **not** need a Gemini API key to develop. The suite is hermetic — temp
dirs for state, fakes at the seams, real logic — and no test touches the network.

## The five rules this codebase actually enforces

1. **A gate is enforced in Python, never by prompt wording.** If the model
   should not do something, the tool refuses it at the dispatch point. Prompt
   text is not a security control.
2. **Fail soft at the edges, loudly at the centre.** A missing `yt-dlp`, a dead
   audio device, a plugin that raises: the app keeps running and says so. A
   broken gate: the app must refuse and stop that path dead.
3. **Say why, in the comment.** Not `# increment counter` — the reason a thing is
   done this way, especially when it looks odd. Every non-obvious line in this
   repo has one; that is the strongest convention here.
4. **One place for one job.** One app root (`core/paths.py`), one API-key reader,
   one version string, one audit chain, one confirm gate. If you find a second
   copy, that is the bug.
5. **Tests assert behaviour, not source text.** Grepping a file for a method name
   proves a string exists, not that anything works. Drive the real function
   instead. (Some source pins remain; `PROJECT_ANALYSIS.md` Part 9 tracks them.)

## Adding a tool (the common case)

Drop one file in `actions/` (or `plugins/` for a personal skill). It self-describes:

```python
TOOL = {
    "name": "my_tool",
    "description": "One line the model reads to decide when to call it.",
    "parameters": {"type": "OBJECT", "properties": {...}, "required": [...]},
}

def my_tool(args: dict) -> str:
    ...                      # return a string the model can read
```

- Return a short string. Long output burns the context window — the dispatcher
  caps tool output centrally, and your tool should not be the reason a session
  runs out of room.
- Mark irreversible actions with the confirmation gate (`core/confirm.py`) and
  register the reverse with the undo stack (`core/undo.py`) when one exists.
- If it touches the network with user data, add it to privacy mode's list
  (`core/privacy.py`) so the gate covers it.
- Write a test that calls the handler with realistic arguments.

Because tools are loaded from disk, `python -m pytest tests/` should still pass
with your file removed — a new tool must not be a dependency of anything.

## What change should look like

- **Keep the diff readable.** No drive-by reformatting: the repo is not
  `ruff format`-clean on purpose, and `make format` (which would rewrite
  74,000 lines) is opt-in, never part of a commit.
- **One behaviour change per commit, with the reason in the message.** The git
  log is the project's second documentation channel; write it for the person
  reading `git log` in a year.
- **Update the docs you make wrong.** `README.md`, `docs/ARCHITECTURE.md`,
  `CHANGELOG.md` under `## Unreleased`, and — if you close a roadmap item —
  the checkbox in `PROJECT_ANALYSIS.md`.
- **Bump the version** (`core/version.py`) for anything user-visible.

## Review checklist (what CI and a reviewer will look at)

- [ ] `make lint` clean — ruff, no new unused imports or undefined names.
- [ ] `make test` green, new behaviour covered by a test that fails without the
      change. Verify that: break your own fix and watch the test go red.
- [ ] `make audit` still 19/19, `make doctor` still sane on a clean machine.
- [ ] No new secret-shaped file, no new `print()` in a core error path (log at
      the right level instead), no bare `except: pass` without a comment saying
      why silence is correct there.
- [ ] Every new `subprocess` call has a `timeout=`.
- [ ] Every new file-reading path goes through `core/paths.py`.

## Questions

Open an issue with the `question` label. If you are unsure whether an idea fits,
ask before building it — the answer here is usually "yes, if it can fail soft
and say why in the comment".
