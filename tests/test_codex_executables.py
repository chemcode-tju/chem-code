"""Version-aware local and remote Codex executable discovery and PATH propagation."""

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from chemistry_agent.backends import executables
from chemistry_agent.backends.codex import CodexBackend
from chemistry_agent.hosting import remote_helper


def _make_fake_cli(path: Path, version_output: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho {version_output!r}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_parse_semver_ordering() -> None:
    v144 = executables.parse_semver("codex-cli 0.144.0")
    v164 = executables.parse_semver("codex-cli 0.164.0")
    v171_alpha2 = executables.parse_semver("codex-cli 0.171.0-alpha.2")
    v171_alpha13 = executables.parse_semver("codex-cli 0.171.0-alpha.13")
    v171 = executables.parse_semver("codex-cli 0.171.0")
    assert v144 is not None and v164 is not None and v171 is not None
    assert v144 < v164 < v171_alpha2 < v171_alpha13 < v171


def test_default_codex_prefers_newer_version_even_when_stale_is_earlier_in_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executables._VERSION_CACHE.clear()
    fake_home = tmp_path / "home"
    stale_dir = fake_home / ".local/bin"
    newer_dir = tmp_path / "usr_local/bin"

    stale = _make_fake_cli(stale_dir / "codex", "codex-cli 0.144.0")
    newer = _make_fake_cli(newer_dir / "codex", "codex-cli 0.171.0-alpha.13")

    monkeypatch.setattr(executables, "codex_search_dirs", lambda home=None: (newer_dir, stale_dir))
    # Even when ~/.local/bin appears BEFORE /usr/local/bin on PATH, newest wins.
    path_env = f"{stale_dir}{os.pathsep}{newer_dir}"
    resolved = executables.resolve_codex_executable("codex", path_env=path_env, home=fake_home)
    assert resolved == str(newer)

    candidates = executables.discover_codex_candidates(path_env=path_env, home=fake_home)
    assert candidates == [
        {"path": str(newer), "version": "0.171.0-alpha.13"},
        {"path": str(stale), "version": "0.144.0"},
    ]


def test_default_codex_picks_newer_standalone_over_older_homebrew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executables._VERSION_CACHE.clear()
    fake_home = tmp_path / "home"
    brew_dir = tmp_path / "homebrew/bin"
    local_dir = fake_home / ".local/bin"

    _make_fake_cli(brew_dir / "codex", "codex-cli 0.164.0")
    newer = _make_fake_cli(local_dir / "codex", "codex-cli 0.171.0")

    monkeypatch.setattr(executables, "codex_search_dirs", lambda home=None: (brew_dir, local_dir))
    path_env = f"{brew_dir}{os.pathsep}{local_dir}"
    assert executables.resolve_codex_executable("codex", path_env=path_env, home=fake_home) == str(
        newer
    )


def test_explicit_codex_path_is_respected_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executables._VERSION_CACHE.clear()
    old_dir = tmp_path / "old/bin"
    new_dir = tmp_path / "new/bin"
    old_bin = _make_fake_cli(old_dir / "codex", "codex-cli 0.144.0")
    _make_fake_cli(new_dir / "codex", "codex-cli 0.171.0")

    monkeypatch.setattr(executables, "codex_search_dirs", lambda home=None: (new_dir, old_dir))
    monkeypatch.setenv("PATH", f"{new_dir}{os.pathsep}{old_dir}")
    config = SimpleNamespace(host_id="local", codex_executable=str(old_bin))
    assert CodexBackend.executable(config) == str(old_bin)


def test_symlink_deduplication_and_tie_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executables._VERSION_CACHE.clear()
    target = _make_fake_cli(tmp_path / "releases/0.171.0/bin/codex", "codex-cli 0.171.0")
    dir_a = tmp_path / "a/bin"
    dir_b = tmp_path / "b/bin"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    link_a = dir_a / "codex"
    link_b = dir_b / "codex"
    link_a.symlink_to(target)
    link_b.symlink_to(target)

    monkeypatch.setattr(executables, "codex_search_dirs", lambda home=None: (dir_a, dir_b))
    candidates = executables.discover_codex_candidates(path_env=f"{dir_a}{os.pathsep}{dir_b}")
    assert candidates == [{"path": str(link_a), "version": "0.171.0"}]


def test_prepend_executable_dir_keeps_chosen_directory_first(tmp_path: Path) -> None:
    env = {"PATH": "/Users/test/.local/bin:/usr/local/bin:/usr/bin"}
    updated = executables.prepend_executable_dir(env, "/usr/local/bin/codex")
    assert updated["PATH"] == "/usr/local/bin:/Users/test/.local/bin:/usr/bin"


def test_remote_helper_discover_prefers_newest_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_dir = tmp_path / "local/bin"
    new_dir = tmp_path / "usr_local/bin"
    _make_fake_cli(old_dir / "codex", "codex-cli 0.144.0")
    newer = _make_fake_cli(new_dir / "codex", "codex-cli 0.171.0-alpha.13")

    monkeypatch.setenv("PATH", f"{old_dir}{os.pathsep}{new_dir}")
    found = remote_helper._discover_executable(
        "codex", prefer_newest=True, fallback_dirs=(new_dir, old_dir)
    )
    assert found == str(newer)
