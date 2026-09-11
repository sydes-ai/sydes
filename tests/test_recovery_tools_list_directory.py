"""Regression test for a real crash: `RepoTools.list_directory` raised
`ValueError` on its first entry whenever constructed with a relative repo
root (exactly what every `--repo name=.` CLI invocation passes) -- found
against a real repository whose first alphabetically-sorted dotfile was
`.env.sample`, which looked file-specific but was actually a general
absolute-vs-relative `Path.relative_to` mismatch."""

from __future__ import annotations

import os
from pathlib import Path

from sydes.recovery.tools import RepoTools


def test_list_directory_does_not_crash_with_a_relative_repo_root(tmp_path: Path, monkeypatch):
    (tmp_path / ".env.sample").write_text("X=1\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("pass\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path.parent)
    relative_root = Path(os.path.relpath(tmp_path, tmp_path.parent))
    assert not relative_root.is_absolute()

    tools = RepoTools(relative_root)
    out = tools.list_directory(".")

    assert "ERROR" not in out
    assert ".env.sample" in out
    assert "src" in out


def test_list_directory_entries_are_relative_not_absolute(tmp_path: Path, monkeypatch):
    (tmp_path / "a.txt").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path.parent)
    relative_root = Path(os.path.relpath(tmp_path, tmp_path.parent))

    tools = RepoTools(relative_root)
    out = tools.list_directory(".")

    assert str(tmp_path) not in out
    assert "a.txt" in out
