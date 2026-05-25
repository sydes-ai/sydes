"""Generic handler symbol index builder with pluggable language adapters."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.repo_map import IGNORED_DIRS, build_repo_map
from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    classify_candidate_file_role,
)
from sydes.trace.handler_symbols.common import HandlerSymbolExtractor
from sydes.trace.handler_symbols.js_ts import JsTsHandlerSymbolExtractor

_MAX_FILE_SIZE = 2_000_000


def _select_preferred_dirs(repo_map_payload: dict) -> set[str]:
    preferred_dirs: set[str] = set()
    for key in (
        "candidate_route_dirs",
        "candidate_backend_dirs",
        "candidate_controller_dirs",
    ):
        for item in repo_map_payload.get(key, []):
            if isinstance(item, str) and item and item != ".":
                preferred_dirs.add(item)
    for entry in repo_map_payload.get("entrypoint_candidates", []):
        if isinstance(entry, str) and entry:
            parent = str(Path(entry).parent).replace("\\", "/")
            if parent and parent != ".":
                preferred_dirs.add(parent)
    return preferred_dirs


def _should_include(relative_path: str, preferred_dirs: set[str]) -> bool:
    if not preferred_dirs:
        return True
    for directory in preferred_dirs:
        if relative_path == directory or relative_path.startswith(directory + "/"):
            return True
    parent = str(Path(relative_path).parent).replace("\\", "/")
    for directory in preferred_dirs:
        if parent == directory or parent.startswith(directory + "/"):
            return True
    return False


def _extractor_registry() -> list[HandlerSymbolExtractor]:
    # Future adapters can be added here (python/java/go/csharp/ruby/php/kotlin).
    return [JsTsHandlerSymbolExtractor()]


def _extractor_by_extension() -> dict[str, HandlerSymbolExtractor]:
    mapping: dict[str, HandlerSymbolExtractor] = {}
    for extractor in _extractor_registry():
        for ext in extractor.extensions:
            mapping[ext.lower()] = extractor
    return mapping


def build_handler_symbol_index(repo: RepoRef) -> dict:
    """Build a generic handler symbol index for one repository."""
    root = Path(repo.root).expanduser().resolve()
    repo_map_payload = build_repo_map(repo)
    preferred_dirs = _select_preferred_dirs(repo_map_payload)
    by_ext = _extractor_by_extension()

    files: list[dict] = []
    summary_counter = Counter(
        {
            "files_indexed": 0,
            "classes": 0,
            "class_methods": 0,
            "functions": 0,
            "imports": 0,
            "exports": 0,
            "symbols": 0,
        }
    )

    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
        for filename in filenames:
            path = dirpath / filename
            rel = path.relative_to(root).as_posix()
            ext = path.suffix.lower()
            extractor = by_ext.get(ext)
            if extractor is None:
                continue
            role = classify_candidate_file_role(rel)
            if role != FILE_ROLE_SOURCE_ROUTE_CANDIDATE:
                continue
            if not _should_include(rel, preferred_dirs):
                continue
            try:
                if path.stat().st_size > _MAX_FILE_SIZE:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            file_symbols = extractor.extract_file(root, rel, text).to_dict()
            files.append(file_symbols)
            summary_counter["files_indexed"] += 1
            summary_counter["imports"] += len(file_symbols["imports"])
            summary_counter["exports"] += len(file_symbols["exports"])
            summary_counter["symbols"] += len(file_symbols["symbols"])
            for symbol in file_symbols["symbols"]:
                kind = symbol.get("kind")
                if kind == "class":
                    summary_counter["classes"] += 1
                elif kind == "class_method":
                    summary_counter["class_methods"] += 1
                elif kind == "function":
                    summary_counter["functions"] += 1

    files.sort(key=lambda item: item["path"])
    return {
        "version": "v1",
        "repo": repo.name,
        "root": str(root),
        "files": files,
        "summary": dict(summary_counter),
    }


def build_handler_symbol_index_batch(repos: list[RepoRef]) -> dict:
    """Build handler symbol indexes for all repositories."""
    repo_indexes = [build_handler_symbol_index(repo) for repo in repos]
    summary_counter = Counter(
        {
            "files_indexed": 0,
            "classes": 0,
            "class_methods": 0,
            "functions": 0,
            "imports": 0,
            "exports": 0,
            "symbols": 0,
        }
    )
    for repo_index in repo_indexes:
        repo_summary = repo_index.get("summary", {})
        for key in summary_counter:
            summary_counter[key] += int(repo_summary.get(key, 0))
    return {"version": "v1", "repos": repo_indexes, "summary": dict(summary_counter)}

