"""Git-backed change resolution for `sydes verify-change`.

Resolves the diff against a base revision into files + hunks. Symbol-level
attribution happens later, once the symbol index knows line spans.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from sydes.ingest.file_roles import classify_candidate_file_role
from sydes.verify.models import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_MODIFIED,
    CHANGE_RENAMED,
    ChangedFile,
    ChangeSet,
    Hunk,
)

_HUNK_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@")
_GIT_TIMEOUT_SECONDS = 60


class GitChangeError(RuntimeError):
    """Raised when git is unavailable or the base revision cannot be resolved."""


def _run_git(repo_root: Path, args: list[str]) -> str:
    """Run a git command in a repo root and return stdout, raising on failure."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment dependent
        raise GitChangeError("git executable not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - environment dependent
        raise GitChangeError(f"git command timed out: git {' '.join(args)}") from exc
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout or "").strip()
        raise GitChangeError(f"git {' '.join(args)} failed: {message}")
    return completed.stdout


def _try_git(repo_root: Path, args: list[str]) -> str | None:
    """Run a git command, returning None instead of raising on failure."""
    try:
        return _run_git(repo_root, args)
    except GitChangeError:
        return None


def _classify_status(code: str) -> str:
    """Map a git name-status code to a Sydes change type."""
    letter = code[:1].upper()
    if letter == "A":
        return CHANGE_ADDED
    if letter == "D":
        return CHANGE_DELETED
    if letter == "R":
        return CHANGE_RENAMED
    return CHANGE_MODIFIED


def _parse_name_status(output: str) -> list[tuple[str, str, str | None]]:
    """Parse `git diff --name-status -M` into (change_type, path, old_path)."""
    rows: list[tuple[str, str, str | None]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        code = parts[0].strip()
        change_type = _classify_status(code)
        if change_type == CHANGE_RENAMED and len(parts) >= 3:
            rows.append((change_type, parts[2].strip(), parts[1].strip()))
        elif len(parts) >= 2:
            rows.append((change_type, parts[1].strip(), None))
    return rows


def _parse_numstat(output: str) -> dict[str, tuple[int, int, bool]]:
    """Parse `git diff --numstat -M` into path -> (added, removed, is_binary)."""
    stats: dict[str, tuple[int, int, bool]] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, removed_raw, path = parts[0], parts[1], parts[-1].strip()
        binary = added_raw == "-" or removed_raw == "-"
        added = 0 if binary else int(added_raw or 0)
        removed = 0 if binary else int(removed_raw or 0)
        stats[path] = (added, removed, binary)
    return stats


def _parse_hunks(output: str) -> list[Hunk]:
    """Parse post-image hunk ranges from a `git diff -U0` patch for one file."""
    hunks: list[Hunk] = []
    for line in output.splitlines():
        match = _HUNK_RE.match(line)
        if match is None:
            continue
        start = int(match.group("new"))
        count = int(match.group("new_count") or 1)
        if count == 0:
            # Pure deletion: anchor to the surrounding line so symbol lookup works.
            hunks.append(Hunk(start_line=max(start, 1), end_line=max(start, 1)))
            continue
        hunks.append(Hunk(start_line=start, end_line=start + count - 1))
    return hunks


def _merge_hunks(existing: list[Hunk], extra: list[Hunk]) -> list[Hunk]:
    """Merge and sort hunk ranges, collapsing exact duplicates."""
    seen: set[tuple[int, int]] = set()
    merged: list[Hunk] = []
    for hunk in [*existing, *extra]:
        key = (hunk.start_line, hunk.end_line)
        if key in seen:
            continue
        seen.add(key)
        merged.append(hunk)
    return sorted(merged, key=lambda item: (item.start_line, item.end_line))


def is_git_repository(repo_root: Path) -> bool:
    """Return True when the path is inside a git work tree."""
    output = _try_git(repo_root, ["rev-parse", "--is-inside-work-tree"])
    return bool(output and output.strip() == "true")


def _diff_ranges(repo_root: Path, base: str) -> tuple[str | None, list[tuple[str, list[str]]], list[str]]:
    """Resolve the merge base and the diff ranges to inspect."""
    notes: list[str] = []
    merge_base = None
    merge_base_output = _try_git(repo_root, ["merge-base", base, "HEAD"])
    if merge_base_output and merge_base_output.strip():
        merge_base = merge_base_output.strip()
    else:
        rev = _try_git(repo_root, ["rev-parse", "--verify", f"{base}^{{commit}}"])
        if rev is None:
            raise GitChangeError(
                f"Base revision '{base}' could not be resolved in {repo_root}."
            )
        merge_base = rev.strip()
        notes.append(f"merge_base_fallback=rev-parse:{base}")

    ranges: list[tuple[str, list[str]]] = [("committed", [merge_base, "HEAD"])]
    # Working tree (staged + unstaged) relative to HEAD, so uncommitted work is
    # analyzable without forcing a commit first.
    ranges.append(("working_tree", ["HEAD"]))
    return merge_base, ranges, notes


def resolve_change_set(
    *,
    repo_name: str,
    repo_root: Path,
    base: str,
    include_working_tree: bool = True,
) -> ChangeSet:
    """Resolve the change of HEAD (+ working tree) against a base revision."""
    root = Path(repo_root).expanduser().resolve()
    if not is_git_repository(root):
        raise GitChangeError(f"Not a git repository: {root}")

    merge_base, ranges, notes = _diff_ranges(root, base)
    head = (_try_git(root, ["rev-parse", "HEAD"]) or "").strip() or None

    by_path: dict[str, ChangedFile] = {}
    working_tree_seen = False

    for label, rev_args in ranges:
        if label == "working_tree" and not include_working_tree:
            continue
        name_status = _try_git(root, ["diff", "--name-status", "-M", "--relative", *rev_args])
        if name_status is None:
            continue
        rows = _parse_name_status(name_status)
        if not rows:
            continue
        if label == "working_tree":
            working_tree_seen = True
        numstat = _parse_numstat(_try_git(root, ["diff", "--numstat", "-M", "--relative", *rev_args]) or "")

        for change_type, path, old_path in rows:
            added, removed, binary = numstat.get(path, (0, 0, False))
            hunks: list[Hunk] = []
            if change_type != CHANGE_DELETED and not binary:
                patch = _try_git(root, ["diff", "-U0", "-M", "--relative", *rev_args, "--", path])
                if patch:
                    hunks = _parse_hunks(patch)

            existing = by_path.get(path)
            if existing is None:
                by_path[path] = ChangedFile(
                    repo=repo_name,
                    path=path,
                    old_path=old_path,
                    change_type=change_type,
                    role=classify_candidate_file_role(path),
                    added_lines=added,
                    removed_lines=removed,
                    hunks=hunks,
                    binary=binary,
                )
                continue

            existing.added_lines += added
            existing.removed_lines += removed
            existing.hunks = _merge_hunks(existing.hunks, hunks)
            existing.binary = existing.binary or binary
            if existing.change_type == CHANGE_ADDED and change_type == CHANGE_DELETED:
                existing.change_type = CHANGE_DELETED
            elif change_type == CHANGE_ADDED and existing.change_type == CHANGE_MODIFIED:
                existing.change_type = CHANGE_ADDED
            if old_path and not existing.old_path:
                existing.old_path = old_path

    files = sorted(by_path.values(), key=lambda item: item.path)
    if working_tree_seen:
        notes.append("includes_working_tree=true")

    return ChangeSet(
        base=base,
        head=head,
        merge_base=merge_base,
        includes_working_tree=working_tree_seen,
        files=files,
        notes=notes,
    )


def read_unified_diff(
    *,
    repo_root: Path,
    base_rev: str,
    paths: list[str] | None = None,
    context_lines: int = 3,
    max_chars: int = 20_000,
) -> str:
    """Read a bounded unified diff for LLM context (committed + working tree)."""
    root = Path(repo_root).expanduser().resolve()
    chunks: list[str] = []
    for rev_args in ([base_rev, "HEAD"], ["HEAD"]):
        args = ["diff", f"-U{context_lines}", "-M", "--relative", *rev_args]
        if paths:
            args.extend(["--", *paths])
        patch = _try_git(root, args)
        if patch and patch.strip():
            chunks.append(patch)
    combined = "\n".join(chunks)
    if len(combined) <= max_chars:
        return combined
    return combined[:max_chars] + "\n... [diff truncated by Sydes budget]\n"
