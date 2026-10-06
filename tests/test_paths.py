"""One implementation of the app root, one reader for the API key.

Eighteen modules used to derive the app root themselves with the same four
lines. Every one of them was correct — each file happened to sit at the right
depth — which is what made it fragile rather than broken: the pattern only has
to be copied into a module one directory deeper to resolve to `actions/`
instead of the repo root, and then `config/api_keys.json` is simply not there.

This file pins both halves: the behaviour of the canonical implementation
(including the frozen case nothing else can reach), and the absence of new
copies.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FROZEN_RE = re.compile(r"getattr\(\s*sys\s*,\s*[\"']frozen[\"']")

#: Files allowed to ask the OS where the executable lives.
FROZEN_ALLOWED = {
    "core/paths.py",        # the one implementation
}


def _source_files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "tests/", "build/", "dist/")):
            continue
        out.append(p)
    return out


# ────────────────────────────────────────────────────────────────────────────
# the implementation
# ────────────────────────────────────────────────────────────────────────────

class TestBaseDir:
    def test_it_is_the_repo_root(self):
        from core.paths import base_dir
        assert base_dir() == ROOT
        assert (base_dir() / "main.py").is_file()

    def test_frozen_builds_anchor_to_the_executable(self, monkeypatch, tmp_path):
        """The branch that only exists in a shipped build — so this is the only
        place it is ever exercised."""
        import sys
        from core import paths
        exe = tmp_path / "dist" / "jarvis.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(exe))
        assert paths.base_dir() == exe.parent

    def test_config_paths_hang_off_it(self):
        from core.paths import api_keys_path, config_dir, base_dir
        assert config_dir() == base_dir() / "config"
        assert api_keys_path() == base_dir() / "config" / "api_keys.json"

    def test_config_package_delegates_to_the_same_place(self):
        """~30 modules still do `from config import get_base_dir`, and the name
        `config` is also shipped by OpenCV — but there must not be a second
        derivation behind it."""
        from config import get_base_dir
        from core.paths import base_dir
        assert get_base_dir() == base_dir()


class TestAPIKey:
    def _point_at(self, monkeypatch, text: str | None):
        from core import paths
        p = paths.api_keys_path().parent / "api_keys.json"
        if text is None:
            monkeypatch.setattr(paths, "api_keys_path", lambda: p)
            return p
        p.write_text(text, encoding="utf-8")
        monkeypatch.setattr(paths, "api_keys_path", lambda: p)
        return p

    def test_a_present_key_is_returned(self, tmp_path, monkeypatch):
        from core import paths
        self._point_at(monkeypatch, '{"gemini_api_key": "abc123"}')
        monkeypatch.setattr(paths, "api_keys_path",
                            lambda: tmp_path / "api_keys.json_same")
        # re-point through the helper so the file above is the one read
        f = tmp_path / "api_keys.json"
        f.write_text('{"gemini_api_key": "abc123"}', encoding="utf-8")
        monkeypatch.setattr(paths, "api_keys_path", lambda: f)
        assert paths.get_api_key() == "abc123"

    def test_a_missing_file_says_which_file(self, tmp_path, monkeypatch):
        from core import paths
        monkeypatch.setattr(paths, "api_keys_path",
                            lambda: tmp_path / "nope" / "api_keys.json")
        with pytest.raises(paths.MissingAPIKey) as e:
            paths.get_api_key()
        msg = str(e.value)
        assert "api_keys.json" in msg and "gemini_api_key" in msg

    def test_a_missing_key_says_which_key(self, tmp_path, monkeypatch):
        from core import paths
        f = tmp_path / "api_keys.json"
        f.write_text('{"assistant_name": "JARVIS"}', encoding="utf-8")
        monkeypatch.setattr(paths, "api_keys_path", lambda: f)
        with pytest.raises(paths.MissingAPIKey) as e:
            paths.get_api_key()
        assert "gemini_api_key" in str(e.value)

    def test_an_empty_key_is_not_a_credential(self, tmp_path, monkeypatch):
        from core import paths
        f = tmp_path / "api_keys.json"
        f.write_text('{"gemini_api_key": "   "}', encoding="utf-8")
        monkeypatch.setattr(paths, "api_keys_path", lambda: f)
        with pytest.raises(paths.MissingAPIKey):
            paths.get_api_key()

    def test_broken_json_is_reported_not_raised_raw(self, tmp_path, monkeypatch):
        from core import paths
        f = tmp_path / "api_keys.json"
        f.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(paths, "api_keys_path", lambda: f)
        with pytest.raises(paths.MissingAPIKey):
            paths.get_api_key()

    def test_the_soft_variant_answers_instead_of_raising(self, tmp_path, monkeypatch):
        """computer_control PROBES with this: no key means 'no vision', which is
        an answer it can act on, not an error."""
        from core import paths
        monkeypatch.setattr(paths, "api_keys_path",
                            lambda: tmp_path / "absent.json")
        assert paths.api_key_or_empty() == ""

    def test_a_handlers_key_is_not_a_config_read(self):
        """The one live call site outside main.py must go through core.paths,
        so 'no key' cannot arrive as a bare KeyError from a dict index."""
        src = (ROOT / "actions" / "computer_control.py").read_text(encoding="utf-8")
        assert "api_key_or_empty" in src
        assert '["gemini_api_key"]' not in src


# ────────────────────────────────────────────────────────────────────────────
# no new copies
# ────────────────────────────────────────────────────────────────────────────

class TestTheDedupeHolds:
    def test_only_core_paths_derives_the_app_root(self):
        offenders = []
        for p in _source_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel in FROZEN_ALLOWED:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if FROZEN_RE.search(text):
                offenders.append(rel)
        assert not offenders, (
            "these modules re-derive the app root instead of using "
            f"core.paths.base_dir(): {offenders}")

    def test_no_api_config_path_constants_survive(self):
        """`API_CONFIG_PATH = BASE_DIR / 'config' / 'api_keys.json'` was written
        out in six modules and consumed in one place — four of the six were
        already dead when this was cleaned up. The path has one owner."""
        offenders = []
        for p in _source_files():
            rel = p.relative_to(ROOT).as_posix()
            if rel == "core/paths.py":
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^API_CONFIG_PATH\s*=", text, re.M):
                offenders.append(rel)
        assert not offenders, f"duplicate api_keys.json path in: {offenders}"

    def test_the_one_derivation_is_the_one_in_paths(self):
        """Guard against the guard going stale: if core/paths.py ever stops
        containing the derivation, the test above would pass vacuously."""
        text = (ROOT / "core" / "paths.py").read_text(encoding="utf-8")
        assert FROZEN_RE.search(text), "core/paths.py no longer knows about frozen builds"
        tree = ast.parse(text)
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert {"base_dir", "config_dir", "api_keys_path",
                "get_api_key", "api_key_or_empty"} <= names
