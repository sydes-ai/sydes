"""Bounded context preparation helpers for downstream flow expansion."""

from __future__ import annotations

import re
from pathlib import Path

from sydes.core.models import (
    EndpointCandidate,
    ExpansionContextFile,
    FlowExpansionContext,
    RepoRef,
)
from sydes.ingest.inventory import build_repo_inventory
from sydes.ingest.readers import read_text_file_for_flow_expansion

DEFAULT_RELATED_FILE_LIMIT = 4
DEFAULT_INVENTORY_MAX_FILES = 8_000
RELATED_FILE_KEYWORDS = {
    "service",
    "services",
    "client",
    "clients",
    "db",
    "database",
    "model",
    "models",
    "repository",
    "repositories",
    "repo",
    "dao",
    "store",
    "query",
    "queries",
}


def _repo_root_map(repos: list[RepoRef]) -> dict[str, str]:
    """Map repo name to normalized root path."""
    return {repo.name: repo.root for repo in repos}


def _tokenize_symbol(value: str | None) -> set[str]:
    """Extract conservative symbol tokens from a handler or evidence symbol."""
    if not value:
        return set()
    tokens = {part.lower() for part in re.split(r"[^A-Za-z0-9_]+", value) if part}
    return {token for token in tokens if len(token) >= 3}


def _collect_symbol_tokens(endpoint: EndpointCandidate) -> set[str]:
    """Collect symbol tokens from endpoint handler and evidence symbols."""
    tokens = _tokenize_symbol(endpoint.handler)
    for ref in endpoint.evidence:
        tokens.update(_tokenize_symbol(ref.symbol))
    return tokens


def _score_related_file(
    path: str,
    *,
    anchor_parts: tuple[str, ...],
    anchor_dir: str,
    anchor_suffix: str,
    anchor_stem: str,
    symbol_tokens: set[str],
) -> tuple[float, list[str]]:
    """Score one file as a nearby candidate for selective expansion context."""
    score = 0.0
    reasons: list[str] = []
    candidate = Path(path)
    candidate_parts = tuple(part.lower() for part in candidate.parts)
    candidate_name = candidate.name.lower()
    candidate_stem = candidate.stem.lower()
    candidate_dir = candidate.parent.as_posix().lower()

    if candidate_dir == anchor_dir:
        score += 3.0
        reasons.append("same_directory")

    if candidate.suffix.lower() == anchor_suffix:
        score += 0.7
        reasons.append("same_extension")

    if candidate_parts and anchor_parts and candidate_parts[0] == anchor_parts[0]:
        score += 0.6
        reasons.append("same_top_level_dir")

    keyword_hits = [token for token in RELATED_FILE_KEYWORDS if token in candidate_parts or token in candidate_name]
    if keyword_hits:
        score += min(2.0, 0.9 + 0.3 * len(keyword_hits))
        reasons.append("related_filename_keyword")

    if anchor_stem and anchor_stem in candidate_name and candidate_stem != anchor_stem:
        score += 0.7
        reasons.append("name_matches_anchor")

    symbol_hits = [token for token in symbol_tokens if token in candidate_name or token in candidate_stem]
    if symbol_hits:
        score += min(2.4, 1.0 + 0.4 * len(symbol_hits))
        reasons.append("name_matches_symbol")

    return score, reasons


def _select_related_files(
    endpoint: EndpointCandidate,
    repo_root: str,
    *,
    max_related_files: int,
    inventory_max_files: int,
) -> list[tuple[str, list[str]]]:
    """Select a bounded set of files near the anchor endpoint file."""
    inventory = build_repo_inventory(
        repo_name=endpoint.repo,
        repo_root=repo_root,
        include_sizes=False,
        max_files=inventory_max_files,
    )
    anchor_path = endpoint.file
    anchor = Path(anchor_path)
    anchor_parts = tuple(part.lower() for part in anchor.parts)
    anchor_dir = anchor.parent.as_posix().lower()
    anchor_suffix = anchor.suffix.lower()
    anchor_stem = anchor.stem.lower()
    symbol_tokens = _collect_symbol_tokens(endpoint)

    scored: list[tuple[float, str, list[str]]] = []
    for item in inventory.files:
        file_path = item.path
        if file_path == anchor_path:
            continue
        score, reasons = _score_related_file(
            file_path,
            anchor_parts=anchor_parts,
            anchor_dir=anchor_dir,
            anchor_suffix=anchor_suffix,
            anchor_stem=anchor_stem,
            symbol_tokens=symbol_tokens,
        )
        if score <= 0:
            continue
        scored.append((score, file_path, sorted(set(reasons))))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return [(path, reasons) for _, path, reasons in scored[:max_related_files]]


def _build_context_file(repo: str, path: str, reasons: list[str], repo_root: str) -> ExpansionContextFile:
    """Create one expansion context file entry with bounded read metadata."""
    read_result = read_text_file_for_flow_expansion(repo=repo, repo_root=repo_root, relative_path=path)
    truncated = read_result.snippet.truncated if read_result.snippet is not None else None
    return ExpansionContextFile(
        repo=repo,
        file=path,
        selection_reasons=reasons,
        read=read_result,
        truncated=truncated,
    )


def prepare_flow_expansion_context(
    matched_endpoint: EndpointCandidate,
    repos: list[RepoRef],
    *,
    max_related_files: int = DEFAULT_RELATED_FILE_LIMIT,
    inventory_max_files: int = DEFAULT_INVENTORY_MAX_FILES,
) -> FlowExpansionContext:
    """Prepare bounded contextual files anchored on a matched endpoint file."""
    root_by_repo = _repo_root_map(repos)
    repo_root = root_by_repo.get(matched_endpoint.repo)
    if repo_root is None:
        return FlowExpansionContext(
            anchor_repo=matched_endpoint.repo,
            anchor_file=matched_endpoint.file,
            notes=[f"Repo root for '{matched_endpoint.repo}' was not provided."],
        )

    files: list[ExpansionContextFile] = []
    notes: list[str] = []

    files.append(
        _build_context_file(
            repo=matched_endpoint.repo,
            path=matched_endpoint.file,
            reasons=["anchor_endpoint_file"],
            repo_root=repo_root,
        )
    )

    related = _select_related_files(
        matched_endpoint,
        repo_root,
        max_related_files=max_related_files,
        inventory_max_files=inventory_max_files,
    )
    for related_path, reasons in related:
        files.append(
            _build_context_file(
                repo=matched_endpoint.repo,
                path=related_path,
                reasons=reasons,
                repo_root=repo_root,
            )
        )

    notes.append(f"Selected {len(files)} contextual files for flow expansion.")
    if related:
        notes.append(f"Included {len(related)} nearby files beyond the anchor endpoint file.")
    else:
        notes.append("No nearby related files were selected beyond the anchor endpoint file.")

    skipped = [entry for entry in files if entry.read is not None and entry.read.skipped]
    if skipped:
        notes.append(f"{len(skipped)} contextual file reads were skipped due to reader safety checks.")

    truncated_count = sum(1 for entry in files if entry.truncated)
    if truncated_count:
        notes.append(f"{truncated_count} contextual files were truncated by bounded read caps.")

    return FlowExpansionContext(
        anchor_repo=matched_endpoint.repo,
        anchor_file=matched_endpoint.file,
        files=files,
        notes=notes,
    )
