"""Regression coverage for `ensure_case_worktree` — the fix for a real bug
where two benchmark cases (H2/H3 in `experiments/hostile_baseline/`) shared
one physical checkout and a `git checkout` between them silently fed some
runs the wrong commit's tree. The one property under test throughout: two
different commits from the same source repo can never end up sharing a
directory, no matter what order or timing calls happen in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from git_worktree import WorktreeError, ensure_case_worktree


def _init_repo_with_two_commits(path: Path) -> tuple[Path, str, str]:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "marker.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v1"], cwd=path, check=True)
    sha1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()

    (path / "marker.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "v2"], cwd=path, check=True)
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return path, sha1, sha2


def test_two_different_commits_get_two_different_directories(tmp_path: Path) -> None:
    repo, sha1, sha2 = _init_repo_with_two_commits(tmp_path / "repo")
    worktrees_root = tmp_path / "worktrees"

    wt1 = ensure_case_worktree(source_repo=repo, sha=sha1, worktrees_root=worktrees_root)
    wt2 = ensure_case_worktree(source_repo=repo, sha=sha2, worktrees_root=worktrees_root)

    assert wt1 != wt2
    assert (wt1 / "marker.txt").read_text() == "v1\n"
    assert (wt2 / "marker.txt").read_text() == "v2\n"


def test_switching_the_source_repos_own_checkout_does_not_affect_existing_worktrees(
    tmp_path: Path,
) -> None:
    """The exact scenario that caused the original bug: something else
    changes what `source_repo` itself has checked out, after a worktree for
    an earlier commit was already created. The worktree must be unaffected
    -- this is the whole point of using `git worktree` instead of
    `git checkout` in a shared directory."""
    repo, sha1, sha2 = _init_repo_with_two_commits(tmp_path / "repo")
    worktrees_root = tmp_path / "worktrees"

    wt1 = ensure_case_worktree(source_repo=repo, sha=sha1, worktrees_root=worktrees_root)
    assert (wt1 / "marker.txt").read_text() == "v1\n"

    # Simulate later, unrelated work moving the source repo's own checkout
    # (exactly what happened when a second case's setup ran `git checkout`
    # in the shared strapi clone).
    subprocess.run(["git", "checkout", "-q", sha2], cwd=repo, check=True)

    assert (wt1 / "marker.txt").read_text() == "v1\n"  # still v1, untouched


def test_calling_it_again_for_the_same_sha_reuses_the_worktree(tmp_path: Path) -> None:
    repo, sha1, _sha2 = _init_repo_with_two_commits(tmp_path / "repo")
    worktrees_root = tmp_path / "worktrees"

    wt_first = ensure_case_worktree(source_repo=repo, sha=sha1, worktrees_root=worktrees_root)
    wt_second = ensure_case_worktree(source_repo=repo, sha=sha1, worktrees_root=worktrees_root)

    assert wt_first == wt_second
    assert (wt_second / "marker.txt").read_text() == "v1\n"


def test_a_worktree_directory_at_the_wrong_commit_raises_rather_than_being_trusted(
    tmp_path: Path,
) -> None:
    """Defense in depth: if something outside this function's control ever
    moves a worktree's own checkout (it shouldn't, but a stale/corrupted
    directory is not impossible), refuse to reuse it silently."""
    repo, sha1, sha2 = _init_repo_with_two_commits(tmp_path / "repo")
    worktrees_root = tmp_path / "worktrees"

    wt1 = ensure_case_worktree(source_repo=repo, sha=sha1, worktrees_root=worktrees_root)
    subprocess.run(["git", "checkout", "-q", sha2], cwd=wt1, check=True)

    with pytest.raises(WorktreeError, match="not the expected"):
        ensure_case_worktree(source_repo=repo, sha=sha1, worktrees_root=worktrees_root)


def test_accepts_a_short_sha_and_resolves_it_for_the_verification_check(tmp_path: Path) -> None:
    repo, sha1, _sha2 = _init_repo_with_two_commits(tmp_path / "repo")
    worktrees_root = tmp_path / "worktrees"
    short_sha = sha1[:10]

    wt = ensure_case_worktree(source_repo=repo, sha=short_sha, worktrees_root=worktrees_root)
    assert (wt / "marker.txt").read_text() == "v1\n"

    # Calling again with the short form must still recognize the existing,
    # correctly-checked-out worktree rather than erroring or duplicating.
    wt_again = ensure_case_worktree(source_repo=repo, sha=short_sha, worktrees_root=worktrees_root)
    assert wt == wt_again
