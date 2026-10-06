# ── MARK LV / JARVIS — developer commands ────────────────────────────────────
#
#   make help          every target
#   make test          the suite CI runs
#   make lint          ruff
#   make audit         the feature audit (19 checks)
#   make doctor        is this machine ready to run the app
#
# Uses .venv when it exists, the system interpreter otherwise, so the same
# commands work on a fresh clone and on a contributor's machine.

PY      := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python)
RUFF    := $(shell [ -x .venv/bin/ruff ] && echo .venv/bin/ruff || echo ruff)
PYTEST  := $(PY) -m pytest

.DEFAULT_GOAL := help
.PHONY: help test test-fast lint format format-check audit doctor typecheck \
        smoke ci lock clean install precommit check

help:                                    ## show this list
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) \
	  | awk -F':.*##' '{printf "  %-14s %s\n", $$1, $$2}'

test:                                    ## full suite (what CI runs)
	QT_QPA_PLATFORM=offscreen $(PYTEST) tests/ -q

test-fast:                               ## suite, stopping at the first failure
	QT_QPA_PLATFORM=offscreen $(PYTEST) tests/ -x -q --tb=short

lint:                                    ## ruff — the gate that must stay green
	$(RUFF) check .

format:                                  ## ruff format every file (opt-in; big diff)
	$(RUFF) format .

format-check:                            ## would `make format` change anything
	$(RUFF) format --check .

typecheck:                               ## mypy on the modules that are clean
	$(PY) -m mypy --config-file pyproject.toml

audit:                                   ## feature audit — 19 user-facing checks
	$(PY) tools/feature_audit.py

doctor:                                  ## dependency/display/audio/key report
	$(PY) main.py --doctor

smoke:                                   ## offscreen HUD end-to-end smoke
	QT_QPA_PLATFORM=offscreen $(PY) tools/ui_smoke.py

ci: lint test audit                      ## exactly what a green CI run means

lock:                                    ## regenerate requirements.lock from dev reqs
	$(PY) -m pip install -q -r requirements-dev.txt
	$(PY) tools/make_lock.py

precommit:                               ## run the pre-commit hooks on everything
	pre-commit run --all-files

install:                                 ## one-time: OS-aware dependency install
	$(PY) bootstrap.py

check: ci                               ## alias for `make ci`

clean:                                   ## remove caches and export artifacts
	find . -name __pycache__ -type d -not -path "./.venv/*" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache jarvis-diagnostics-*.zip
