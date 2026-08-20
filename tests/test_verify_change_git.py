"""Git change resolution tests for verify-change."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from sydes.verify.git_change import GitChangeError, resolve_change_set
from sydes.verify.models import CHANGE_ADDED, CHANGE_DELETED, CHANGE_MODIFIED, CHANGE_RENAMED


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Create a small git repository with one commit on `main`."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "keep.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
    (root / "gone.py").write_text("def gone():\n    return 2\n", encoding="utf-8")
    (root / "old_name.py").write_text("def moved():\n    return 3\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")
    return root


def test_resolve_change_set_handles_add_modify_delete_and_rename(repo: Path) -> None:
    """All four git change types are classified from a committed branch diff."""
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "keep.py").write_text("def keep():\n    return 42\n", encoding="utf-8")
    (repo / "added.py").write_text("def added():\n    return 4\n", encoding="utf-8")
    (repo / "gone.py").unlink()
    _git(repo, "mv", "old_name.py", "new_name.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")

    change = resolve_change_set(repo_name="api", repo_root=repo, base="main")

    by_path = {item.path: item for item in change.files}
    assert by_path["keep.py"].change_type == CHANGE_MODIFIED
    assert by_path["added.py"].change_type == CHANGE_ADDED
    assert by_path["gone.py"].change_type == CHANGE_DELETED
    assert by_path["new_name.py"].change_type == CHANGE_RENAMED
    assert by_path["new_name.py"].old_path == "old_name.py"


def test_resolve_change_set_records_hunk_ranges(repo: Path) -> None:
    """Modified files carry post-image hunk ranges, not whole-file spans."""
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "keep.py").write_text(
        "def keep():\n    return 1\n\n\ndef extra():\n    return 9\n", encoding="utf-8"
    )
    _git(repo, "commit", "-aqm", "append")

    change = resolve_change_set(repo_name="api", repo_root=repo, base="main")
    hunks = change.files[0].hunks

    assert hunks
    assert all(hunk.start_line >= 3 for hunk in hunks)


def test_resolve_change_set_includes_working_tree_by_default(repo: Path) -> None:
    """Uncommitted edits are analyzable without forcing a commit first."""
    (repo / "keep.py").write_text("def keep():\n    return 7\n", encoding="utf-8")

    change = resolve_change_set(repo_name="api", repo_root=repo, base="main")

    assert change.includes_working_tree is True
    assert [item.path for item in change.files] == ["keep.py"]


def test_resolve_change_set_can_ignore_working_tree(repo: Path) -> None:
    """`--no-working-tree` restricts the diff to committed work."""
    (repo / "keep.py").write_text("def keep():\n    return 7\n", encoding="utf-8")

    change = resolve_change_set(
        repo_name="api", repo_root=repo, base="main", include_working_tree=False
    )

    assert change.files == []


def test_resolve_change_set_rejects_unknown_base(repo: Path) -> None:
    """An unresolvable base revision produces a clear error, not a traceback."""
    with pytest.raises(GitChangeError):
        resolve_change_set(repo_name="api", repo_root=repo, base="no-such-branch")


def test_resolve_change_set_rejects_non_repository(tmp_path: Path) -> None:
    """A non-git directory is rejected up front."""
    with pytest.raises(GitChangeError):
        resolve_change_set(repo_name="api", repo_root=tmp_path, base="main")
