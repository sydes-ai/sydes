"""One `git worktree` per (repo, commit) for benchmark/experiment
infrastructure that needs to inspect a real repository at a specific
commit.

Motivated by a real, empirically-caught bug: the hostile-case benchmark
round (see `experiments/hostile_baseline/`) originally pointed two cases
(H2 and H3) at the same physical `strapi` clone, `git checkout`-ing between
their two target commits. Because model runs execute asynchronously
(background agents, a detached subprocess), the checkout had already moved
on to H3's commit by the time some H2 runs actually read files from disk —
silently feeding them the wrong tree. It was caught only because the
resulting analysis looked suspiciously too clean ("this file doesn't
exist"), not because anything in the harness flagged it.

`ensure_case_worktree` makes that class of bug structurally impossible: a
dedicated, commit-named directory is created (or reused) per (repo, sha), so
two different commits from the same source repo simply cannot collide on
one checkout, no matter how runs are scheduled or how long they take.

Standalone, dependency-free (`subprocess` + `pathlib` only) so any
experiment script can import it without pulling in the rest of Sydes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    """A worktree exists but is not at the commit its own directory name
    promises, or git itself failed. Never silently used as-is — a stale or
    mismatched worktree is worse than a loud failure, since a caller that
    proceeds anyway is exactly how the original bug happened."""


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(
            f"git {' '.join(args)} (cwd={cwd}) failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def ensure_case_worktree(*, source_repo: Path, sha: str, worktrees_root: Path) -> Path:
    """Return a `git worktree` directory checked out (detached HEAD) at
    exactly `sha`, creating it under `worktrees_root` if it doesn't exist
    yet.

    The directory is named after `sha` (`worktrees_root/<sha>`) — this is
    the whole safety property: two calls with different `sha`s always get
    different directories, so nothing downstream can accidentally read one
    case's diff against another case's checkout, regardless of call order
    or timing.

    Idempotent: calling this again with the same `(source_repo, sha)`
    reuses the existing worktree rather than recreating it, after verifying
    it is genuinely still at `sha` (raises `WorktreeError` if not — a
    mismatch here means something outside this function's control moved
    the checkout, which should never happen given the naming scheme, so
    treat it as a hard error rather than silently trusting the directory).

    Never touches `source_repo`'s own checked-out state — worktrees are
    independent working directories sharing the same object database, so
    concurrent cases against the same source repo never interfere with
    each other or with whatever `source_repo` itself happens to have
    checked out.
    """
    worktrees_root.mkdir(parents=True, exist_ok=True)
    target = worktrees_root / sha

    if target.exists():
        try:
            current = _git("rev-parse", "HEAD", cwd=target)
        except WorktreeError as exc:
            raise WorktreeError(
                f"worktree directory {target} exists but is not a usable git checkout: {exc}"
            ) from exc
        resolved_sha = _git("rev-parse", sha, cwd=source_repo)
        if current != resolved_sha:
            raise WorktreeError(
                f"worktree {target} is checked out at {current}, not the expected {resolved_sha} "
                f"(for sha {sha!r}) — refusing to reuse it silently."
            )
        return target

    _git("worktree", "add", "--detach", str(target), sha, cwd=source_repo)
    return target


def remove_case_worktree(*, source_repo: Path, worktree_path: Path) -> None:
    """Remove a worktree created by `ensure_case_worktree`. Not called
    automatically by anything in this module — cleanup is the caller's own
    decision, since a benchmark round may want to keep worktrees around for
    a rerun."""
    _git("worktree", "remove", "--force", str(worktree_path), cwd=source_repo)
