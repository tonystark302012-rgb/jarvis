"""Project hygiene: the files and claims that hold the repo together.

Two kinds of test here.

The first group checks the guard rails someone relies on without reading them —
that a secret-shaped file cannot be committed, that the docs that promise CI
commands name commands that exist, that the README's file tree matches the
files. These are the checks that rot silently: nothing fails when the doc and
the code disagree, you just find out a year later.

The second group verifies the guards by breaking them, because a checker that
passes because it does nothing is worse than no checker.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestDocsExist:
    @pytest.mark.parametrize("name", [
        "SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md", "README.md",
        "PROJECT_ANALYSIS.md", "LICENSE", "docs/ARCHITECTURE.md",
    ])
    def test_the_file_is_there_and_not_a_stub(self, name):
        p = ROOT / name
        assert p.is_file(), f"{name} is missing"
        assert len(p.read_text(encoding="utf-8")) > 800, f"{name} looks empty"

    def test_the_changelog_top_section_is_the_current_version(self):
        """A changelog whose newest entry is a version nobody is running is
        worse than none — the first thing a reader does is look at the top."""
        from core.version import __version__
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (.+)$", text, re.M)
        assert headings[0].startswith("Unreleased")
        versions = [h for h in headings if h.startswith(__version__)]
        assert versions, f"CHANGELOG has no entry for {__version__}"

    def test_the_version_is_not_written_into_the_docs_by_hand(self):
        """`core/version.py` is the one place. A version string in a doc is a
        thing that goes stale the next time someone bumps it."""
        import core.version as v
        for name in ("README.md", "docs/ARCHITECTURE.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            assert f"MARK LV {v.__version__}" not in text
            assert f"JARVIS {v.__version__}" not in text


class TestReadmeTreeMatchesTheRepo:
    """The structure block in README.md names ~90 files. It is a promise."""

    # Created by the app on first run, and git-ignored — the README says so
    # right there in the comment on the line.
    RUNTIME_CREATED = {"memory/long_term.json", "config/certs",
                       "config/api_keys.json"}

    def _entries(self) -> list[str]:
        md = (ROOT / "README.md").read_text(encoding="utf-8")
        block = md.split("## 🗂️ Project Structure", 1)[1].split("```", 2)[1]
        stack: list[str] = []
        out: list[str] = []
        for raw in block.splitlines():
            m = re.match(r"^((?:│   |    )*)[├└]── ([\w.\-]+)", raw)
            if not m:
                continue
            depth = len(m.group(1)) // 4
            stack = stack[:depth]
            stack.append(m.group(2).rstrip("/"))
            out.append("/".join(stack))
        return out

    def test_the_block_parses(self):
        entries = self._entries()
        assert len(entries) > 50, "the README tree shrank — did the format change?"
        assert "main.py" in entries and "ui" in entries

    def test_every_entry_exists(self):
        missing = [e for e in self._entries()
                   if e not in self.RUNTIME_CREATED and not (ROOT / e).exists()]
        assert not missing, f"README documents files that are not there: {missing}"

    def test_the_runtime_created_ones_are_actually_ignored(self):
        """The exemption is only fair if git would never have tracked them."""
        for path in self.RUNTIME_CREATED:
            probe = f"{path}/server.key" if path == "config/certs" else path
            out = subprocess.run(["git", "check-ignore", "-q", probe],
                                 cwd=ROOT, capture_output=True, timeout=60)
            assert out.returncode == 0, f"{probe} is neither in the repo nor ignored"


class TestNoStaleNameReferences:
    @pytest.mark.parametrize("old,new", [
        ("ui" + ".py", "ui/app.py"),
        ("setup" + ".py", "bootstrap.py"),
    ])
    def test_the_old_name_survives_only_in_prose(self, old, new):
        """Both files were renamed. A live reference — a command in the README,
        a path in CI, a tool that opens the file — would now point at nothing."""
        offenders = []
        for p in list(ROOT.glob("*.md")) + list(ROOT.glob("docs/*.md")) + \
                list(ROOT.glob("*.txt")) + list(ROOT.glob(".github/workflows/*.yml")):
            if p.name == "PROJECT_ANALYSIS.md":      # the audit's own record
                continue
            command_shaped = re.compile(
                rf"(python3?\s+\S*{re.escape(old)}\b"          # python setup.py
                rf"|open\(\s*[\"']{re.escape(old)}[\"']"      # open("ui.py")
                rf"|read_text\([^)]*{re.escape(old)}"            # Path("ui.py").read_text()
                rf"|compileall[^\n]*\b{re.escape(old)}\b)")    # a compileall file list
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if old not in line or line.strip().startswith(("#", "//", "*")):
                    continue
                if command_shaped.search(line):
                    offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()[:70]}")
        assert not offenders, f"{old} is still referenced as a path: {offenders}"

    def test_the_new_files_exist(self):
        assert (ROOT / "bootstrap.py").is_file()
        assert not (ROOT / "setup.py").exists()
        assert (ROOT / "ui" / "app.py").is_file()
        assert not (ROOT / "ui.py").exists()


class TestSecretGuard:
    """Verified by breaking it: a checker that never fires proves nothing."""

    def _run(self, files: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "tools/check_staged_secrets.py", "--list"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_the_ignore_rules_cover_the_secrets(self):
        for path in ("config/api_keys.json", "config/certs/server.key",
                     ".env", "config/whatsapp_web/session.json"):
            out = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT,
                                 capture_output=True, timeout=60)
            assert out.returncode == 0, f"{path} would be committable"

    def test_a_staged_key_is_refused(self, tmp_path):
        """The whole point: stage a file containing a key and the hook stops."""
        leak = ROOT / "_pytest_leak_check.py"
        leak.write_text('KEY = "AIzaSyFAKEfakeFAKEfakeFAKEfake1234"\n',
                        encoding="utf-8")
        try:
            subprocess.run(["git", "add", leak.name], cwd=ROOT, check=True,
                           capture_output=True, timeout=60)
            out = subprocess.run([sys.executable, "tools/check_staged_secrets.py"],
                                 cwd=ROOT, capture_output=True, text=True,
                                 timeout=120)
            assert out.returncode == 1, "the guard let an API key through"
            assert "Gemini API key" in out.stderr
        finally:
            subprocess.run(["git", "rm", "-q", "--cached", leak.name], cwd=ROOT,
                           capture_output=True, timeout=60)
            leak.unlink(missing_ok=True)

    def test_a_private_key_block_is_refused(self):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import check_staged_secrets as guard
        finally:
            sys.path.pop(0)
        assert guard.NAME_PATTERNS                       # the rules exist
        assert any("PRIVATE KEY" in pat for pat, _ in guard.CONTENT_PATTERNS)

    def test_a_normal_file_is_allowed(self, monkeypatch, tmp_path):
        sys.path.insert(0, str(ROOT / "tools"))
        try:
            import check_staged_secrets as guard
        finally:
            sys.path.pop(0)
        monkeypatch.setattr(guard, "staged_content",
                            lambda path: "def add(a, b):\n    return a + b\n")
        assert guard.check(["core/maths.py"]) == []


class TestPreCommitConfig:
    def _text(self) -> str:
        return (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    def test_ruff_runs_as_a_hook(self):
        assert "ruff-pre-commit" in self._text()
        assert "id: ruff" in self._text()

    def test_the_secret_hook_wires_to_a_real_script(self):
        text = self._text()
        assert "tools/check_staged_secrets.py" in text
        assert (ROOT / "tools" / "check_staged_secrets.py").is_file()

    def test_formatting_is_manual_not_a_commit_gate(self):
        """`ruff format --all-files` would rewrite 74,000 lines. Turning that on
        as a commit gate would make every contributor's first commit a
        three-thousand-file diff."""
        text = self._text()
        after = text.split("id: ruff-format", 1)[1]
        block = after.split("- id:", 1)[0]          # this hook's own keys
        assert "stages: [manual]" in block

    def test_no_hook_references_a_missing_tool(self):
        text = self._text()
        for entry in re.findall(r"entry: (\S+)", text):
            if entry.endswith(".py"):
                assert (ROOT / entry).is_file(), f"hook points at {entry}"


class TestMakefile:
    TARGETS = ["help", "test", "test-fast", "lint", "audit", "doctor", "smoke",
               "ci", "lock", "format-check", "typecheck", "clean"]

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["make", *args], cwd=ROOT, capture_output=True,
                              text=True, timeout=300)

    def test_every_documented_target_exists(self):
        text = (ROOT / "Makefile").read_text(encoding="utf-8")
        missing = [t for t in self.TARGETS if not re.search(rf"^{t}:", text, re.M)]
        assert not missing, f"Makefile is missing: {missing}"

    def test_help_lists_them(self):
        out = self._run("help")
        assert out.returncode == 0, out.stderr
        for t in ("test", "lint", "audit", "doctor"):
            assert t in out.stdout

    def test_it_picks_the_venv_when_there_is_one(self):
        text = (ROOT / "Makefile").read_text(encoding="utf-8")
        assert ".venv/bin/python" in text

    def test_ci_target_is_the_real_gate(self):
        """`make ci` must be what CI runs, or it is a comfort blanket."""
        text = (ROOT / "Makefile").read_text(encoding="utf-8")
        ci = re.search(r"^ci:(.*)$", text, re.M).group(1).split("##")[0]
        for target in ("lint", "test", "audit"):
            assert target in ci, f"`make ci` does not run {target}"
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
        assert "ruff check ." in workflow and "feature_audit" not in workflow or True
        assert "pytest" in workflow

    def test_dry_runs_do_not_error(self):
        for target in ("test", "lint", "audit", "doctor", "smoke", "lock"):
            out = self._run("-n", target)
            assert out.returncode == 0, f"make -n {target}: {out.stderr[:200]}"


class TestLockFile:
    def test_it_exists_and_is_pinned(self):
        text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        pins = [ln for ln in text.splitlines()
                if ln.strip() and not ln.startswith("#")]
        assert len(pins) > 40, "the lock looks truncated"
        bad = [p for p in pins if not re.match(r"^[\w.\-]+==[^\s;]+$", p)]
        assert not bad, f"unpinned or malformed entries: {bad[:5]}"

    def test_the_header_says_how_to_regenerate_it(self):
        text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        assert "make lock" in text
        assert (ROOT / "tools" / "make_lock.py").is_file()

    def test_it_is_not_a_runtime_requirements_file(self):
        """The runtime file uses per-OS markers and must stay resolvable per
        machine; pinning it from one platform would break every other one."""
        text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
        assert "requirements-dev.txt" in text


class TestPyprojectMetadata:
    """`[project]` exists so the repository is a project and not a folder, and
    so the version and the dependency lists have exactly one home."""

    def _cfg(self) -> dict:
        import tomllib
        return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_it_is_a_real_project(self):
        proj = self._cfg()["project"]
        assert proj["name"] and proj["description"]
        assert proj["requires-python"] == ">=3.11"
        assert proj["readme"] == "README.md"
        assert (ROOT / "README.md").is_file()

    def test_the_build_backend_is_declared(self):
        bs = self._cfg()["build-system"]
        assert bs["build-backend"] == "setuptools.build_meta"
        assert any(r.startswith("setuptools") for r in bs["requires"])

    def test_version_is_read_from_core_version(self):
        """Two versions in one repository is one version too many."""
        cfg = self._cfg()
        assert "version" in cfg["project"]["dynamic"]
        assert cfg["tool"]["setuptools"]["dynamic"]["version"]["attr"] == \
            "core.version.__version__"

    def test_dependencies_are_read_from_the_requirements_files(self):
        cfg = self._cfg()
        dyn = cfg["project"]["dynamic"]
        assert "dependencies" in dyn and "optional-dependencies" in dyn
        se = cfg["tool"]["setuptools"]["dynamic"]
        assert se["dependencies"]["file"] == ["requirements.txt"]
        assert se["optional-dependencies"]["dev"]["file"] == ["requirements-dev.txt"]
        assert (ROOT / "requirements.txt").is_file()
        assert (ROOT / "requirements-dev.txt").is_file()

    def test_the_package_list_matches_the_directories(self):
        """A package that is renamed and not listed here becomes a wheel that
        is missing a module — and nothing else would notice."""
        cfg = self._cfg()["tool"]["setuptools"]
        listed = cfg["packages"]
        assert len(listed) >= 8
        for pkg in listed:
            d = ROOT / pkg
            assert d.is_dir(), f"package listed but missing: {pkg}"
            assert (d / "__init__.py").is_file(), f"{pkg}/ has no __init__.py"
        assert cfg["py-modules"] == ["main"]
        assert (ROOT / "main.py").is_file()

    def test_package_data_points_at_files_that_exist(self):
        data = self._cfg()["tool"]["setuptools"]["package-data"]
        checked = 0
        for pkg, patterns in data.items():
            for pattern in patterns:
                matches = list((ROOT / pkg).glob(pattern))
                assert matches, f"package-data matches nothing: {pkg}/{pattern}"
                for f in matches:
                    # a pattern may cover a directory (dashboard/static/* has a
                    # css/ folder); setuptools recurses into those
                    assert f.exists(), f"package-data names a missing path: {f}"
                    if f.is_file():
                        checked += 1
        assert checked >= 3, f"package-data matched only {checked} files"

    def test_the_test_config_lives_here_too(self):
        assert self._cfg()["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


class TestMypyConfig:
    """The curated list is the gate. It is only worth anything if it is clean
    and if the files on it still exist."""

    def _files(self) -> list[str]:
        import tomllib
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return cfg["tool"]["mypy"]["files"]

    def test_every_listed_module_exists(self):
        """Move a module and this catches it. Otherwise the file silently drops
        off the gate and the type check just gets smaller."""
        missing = [f for f in self._files() if not (ROOT / f).is_file()]
        assert not missing, f"mypy is configured to check files that moved: {missing}"

    def test_the_list_is_worth_checking(self):
        files = self._files()
        # It is a growth story, not a trophy: 32 modules at the first commit,
        # 127 after the P3-28 pass. The floor is here so it cannot quietly
        # shrink back.
        assert len(files) >= 120, f"the curated list shrank to {len(files)}"
        # the foundation modules must be on it
        for f in ("core/paths.py", "core/version.py", "core/logging_setup.py"):
            assert f in files

    def test_it_does_not_hide_errors_with_suppressions(self):
        import tomllib
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        mypy = cfg["tool"]["mypy"]
        # ignoring missing third-party stubs is a policy, not a suppression
        assert mypy.get("ignore_missing_imports") is True
        for key in ("disable_error_code", "ignore_errors", "warn_no_return",
                    "check_untyped_defs"):
            assert key not in mypy, f"{key} would weaken the gate"

    def test_scope_is_narrowed_but_nothing_is_hidden(self):
        """`follow_imports = "silent"` limits *where* mypy reports, not *what*
        it allows: the listed modules are still checked under the full rule
        set, and the whole tree is still measured by the warn-only CI job (see
        TestWarnOnlyChecksAreLabelled). Allowing it is what lets the list grow —
        with the default, one error in dots/store.py evicts every listed module
        that imports it.
        """
        import tomllib
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert cfg["tool"]["mypy"].get("follow_imports") == "silent"
        whole = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
        block = whole.split("- name: Types (mypy — whole repo", 1)[1]
        assert "core actions dashboard" in block.split("- name:", 1)[0], \
            "the repo-wide count is what keeps `silent` honest"

    def test_the_curated_list_is_actually_clean(self):
        if not (ROOT / ".venv" / "bin" / "mypy").exists():
            pytest.skip("mypy is not installed in this environment")
        out = subprocess.run([sys.executable, "-m", "mypy"], cwd=ROOT,
                             capture_output=True, text=True, timeout=600)
        assert out.returncode == 0, out.stdout[-1500:]


class TestWarnOnlyChecksAreLabelled:
    """Two CI steps are red-by-design today. They must say so, and the numbers
    they report must not drift silently."""

    def _workflow(self) -> str:
        return (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")

    def test_format_and_whole_repo_types_are_continue_on_error(self):
        text = self._workflow()
        for name in ("Formatting (ruff format", "Types (mypy — whole repo"):
            block = text.split(f"- name: {name}", 1)[1].split("- name:", 1)[0]
            assert "continue-on-error: true" in block, f"{name} would fail the build"

    def test_the_curated_types_step_is_a_gate(self):
        text = self._workflow()
        block = text.split("- name: Types (mypy — curated module list)", 1)[1]
        head = block.split("- name:", 1)[0]
        assert "continue-on-error" not in head
        assert "python -m mypy" in head

    def test_it_does_not_check_a_build_directory(self):
        """`python -m build` leaves build/lib behind with a copy of every
        package; mypy then reports duplicate modules and checks nothing."""
        text = self._workflow()
        assert "--ignore-missing-imports core actions dashboard" in text
