"""Lightweight bounded file-reading utilities for selective exploration."""

from __future__ import annotations

from itertools import islice
from pathlib import Path
from typing import Iterable

from sydes.core.models import CandidateFileRead, RankedFileCandidate, ReadFileSnippet

DEFAULT_MAX_FILE_SIZE_BYTES = 1_000_000
DEFAULT_MAX_READ_BYTES = 200_000
DEFAULT_MAX_READ_CHARS = 60_000
DEFAULT_MAX_READ_LINES = 1_200
DISCOVERY_MAX_FILE_SIZE_BYTES = 250_000
DISCOVERY_MAX_READ_BYTES = 24_000
DISCOVERY_MAX_READ_CHARS = 12_000
DISCOVERY_MAX_READ_LINES = 220
FLOW_EXPANSION_MAX_FILE_SIZE_BYTES = 180_000
FLOW_EXPANSION_MAX_READ_BYTES = 6_000
FLOW_EXPANSION_MAX_READ_CHARS = 3_500
FLOW_EXPANSION_MAX_READ_LINES = 90
DETERMINISTIC_ROUTE_MAX_FILE_SIZE_BYTES = 2_000_000
DETERMINISTIC_ROUTE_MAX_READ_BYTES = 2_000_000
DETERMINISTIC_ROUTE_MAX_READ_CHARS = 2_000_000
DETERMINISTIC_ROUTE_MAX_READ_LINES = 100_000

BINARY_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".bz2",
    ".7z",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".class",
    ".jar",
    ".pyc",
}


def _is_binary_like(path: Path) -> bool:
    """Return true when a file appears to be binary data."""
    if path.suffix.lower() in BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as handle:
            probe = handle.read(1024)
    except OSError:
        return False
    return b"\x00" in probe


def read_text_file_safely(
    repo: str,
    repo_root: Path | str,
    relative_path: str,
    *,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    max_read_chars: int = DEFAULT_MAX_READ_CHARS,
    max_read_lines: int = DEFAULT_MAX_READ_LINES,
) -> CandidateFileRead:
    """Read one file with bounded size, lines, and chars for safe exploration."""
    root = Path(repo_root).expanduser().resolve()
    file_path = root / relative_path

    if not file_path.exists() or not file_path.is_file():
        return CandidateFileRead(
            repo=repo,
            relative_path=relative_path,
            role=None,
            skipped=True,
            skip_reason="missing_file",
        )

    if _is_binary_like(file_path):
        return CandidateFileRead(
            repo=repo,
            relative_path=relative_path,
            role=None,
            skipped=True,
            skip_reason="binary_file",
        )

    try:
        size_bytes = file_path.stat().st_size
    except OSError:
        return CandidateFileRead(
            repo=repo,
            relative_path=relative_path,
            role=None,
            skipped=True,
            skip_reason="unreadable_file",
        )

    if size_bytes > max_file_size_bytes:
        return CandidateFileRead(
            repo=repo,
            relative_path=relative_path,
            role=None,
            skipped=True,
            skip_reason="file_too_large",
        )

    truncated = False
    try:
        with file_path.open("rb") as handle:
            raw = handle.read(max_read_bytes + 1)
    except OSError:
        return CandidateFileRead(
            repo=repo,
            relative_path=relative_path,
            role=None,
            skipped=True,
            skip_reason="unreadable_file",
        )

    if len(raw) > max_read_bytes:
        raw = raw[:max_read_bytes]
        truncated = True

    text = raw.decode("utf-8", errors="replace")
    if len(text) > max_read_chars:
        text = text[:max_read_chars]
        truncated = True

    lines = text.splitlines()
    if len(lines) > max_read_lines:
        text = "\n".join(lines[:max_read_lines])
        truncated = True
        lines = text.splitlines()

    snippet = ReadFileSnippet(
        repo=repo,
        relative_path=relative_path,
        truncated=truncated,
        text=text,
        line_count=len(lines),
        char_count=len(text),
    )
    return CandidateFileRead(
        repo=repo,
        relative_path=relative_path,
        role=None,
        snippet=snippet,
        skipped=False,
    )


def read_ranked_candidate_files(
    repo: str,
    repo_root: Path | str,
    ranked_candidates: Iterable[RankedFileCandidate],
    *,
    top_n: int = 25,
    max_file_size_bytes: int = DEFAULT_MAX_FILE_SIZE_BYTES,
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES,
    max_read_chars: int = DEFAULT_MAX_READ_CHARS,
    max_read_lines: int = DEFAULT_MAX_READ_LINES,
) -> list[CandidateFileRead]:
    """Batch-read a bounded top-N set of ranked candidate files."""
    results: list[CandidateFileRead] = []
    for candidate in islice(ranked_candidates, top_n):
        target_repo = candidate.repo or repo
        read_result = read_text_file_safely(
            repo=target_repo,
            repo_root=repo_root,
            relative_path=candidate.file,
            max_file_size_bytes=max_file_size_bytes,
            max_read_bytes=max_read_bytes,
            max_read_chars=max_read_chars,
            max_read_lines=max_read_lines,
        )
        read_result.role = candidate.role
        results.append(read_result)
    return results


def read_ranked_candidate_files_for_discovery(
    repo: str,
    repo_root: Path | str,
    ranked_candidates: Iterable[RankedFileCandidate],
    *,
    top_n: int = 5,
) -> list[CandidateFileRead]:
    """Batch-read candidates with tighter caps for endpoint discovery prompts."""
    return read_ranked_candidate_files(
        repo=repo,
        repo_root=repo_root,
        ranked_candidates=ranked_candidates,
        top_n=top_n,
        max_file_size_bytes=DISCOVERY_MAX_FILE_SIZE_BYTES,
        max_read_bytes=DISCOVERY_MAX_READ_BYTES,
        max_read_chars=DISCOVERY_MAX_READ_CHARS,
        max_read_lines=DISCOVERY_MAX_READ_LINES,
    )


def read_text_file_for_flow_expansion(
    repo: str,
    repo_root: Path | str,
    relative_path: str,
) -> CandidateFileRead:
    """Read one file with flow-expansion caps for bounded context building."""
    return read_text_file_safely(
        repo=repo,
        repo_root=repo_root,
        relative_path=relative_path,
        max_file_size_bytes=FLOW_EXPANSION_MAX_FILE_SIZE_BYTES,
        max_read_bytes=FLOW_EXPANSION_MAX_READ_BYTES,
        max_read_chars=FLOW_EXPANSION_MAX_READ_CHARS,
        max_read_lines=FLOW_EXPANSION_MAX_READ_LINES,
    )


def read_ranked_candidate_files_for_deterministic_routes(
    repo: str,
    repo_root: Path | str,
    ranked_candidates: Iterable[RankedFileCandidate],
    *,
    top_n: int = 80,
) -> list[CandidateFileRead]:
    """Batch-read candidates with high caps for deterministic route declaration scanning."""
    return read_ranked_candidate_files(
        repo=repo,
        repo_root=repo_root,
        ranked_candidates=ranked_candidates,
        top_n=top_n,
        max_file_size_bytes=DETERMINISTIC_ROUTE_MAX_FILE_SIZE_BYTES,
        max_read_bytes=DETERMINISTIC_ROUTE_MAX_READ_BYTES,
        max_read_chars=DETERMINISTIC_ROUTE_MAX_READ_CHARS,
        max_read_lines=DETERMINISTIC_ROUTE_MAX_READ_LINES,
    )
