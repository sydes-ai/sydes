"""Generic handler symbol index builder with pluggable language adapters."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path
from typing import Any

from sydes.core.models import RepoRef
from sydes.discover.repo_map import IGNORED_DIRS
from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    classify_candidate_file_role,
)
from sydes.trace.handler_symbols.common import HandlerSymbolExtractor
from sydes.trace.handler_symbols.js_ts import JsTsHandlerSymbolExtractor
from sydes.trace.handler_symbols.python import PythonHandlerSymbolExtractor

_MAX_FILE_SIZE = 2_000_000


def _extractor_registry() -> list[HandlerSymbolExtractor]:
    # Future adapters can be added here (java/go/csharp/ruby/php/kotlin).
    return [JsTsHandlerSymbolExtractor(), PythonHandlerSymbolExtractor()]


def _extractor_by_extension() -> dict[str, HandlerSymbolExtractor]:
    mapping: dict[str, HandlerSymbolExtractor] = {}
    for extractor in _extractor_registry():
        for ext in extractor.extensions:
            mapping[ext.lower()] = extractor
    return mapping


def build_handler_symbol_index(repo: RepoRef, *, fact_cache: Any = None) -> dict:
    """Build a generic handler symbol index for one repository."""
    root = Path(repo.root).expanduser().resolve()
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

    for raw_dirpath, dirnames, filenames in os.walk(root):
        dirpath = Path(raw_dirpath)
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
            # Every ordinary source file is indexed, not only those under
            # route-shaped directories. Change attribution and call following
            # both consult this index, and a changed file that is missing from
            # it yields no symbols at all — which silently reduces a real change
            # to "no downstream effects established". Library code (`core/`,
            # `db/`, `utils/`) lives outside route directories by nature, so
            # route location cannot be a precondition for being indexed.
            #
            # Exclusions still apply and do the real filtering: IGNORED_DIRS
            # prunes vendor/build/cache trees during the walk, the role check
            # above drops tests and docs, and the size guard below skips
            # generated blobs.
            # Symbol facts embed `resolved_file`, which depends on the whole
            # path set, so the orchestrator disables reuse whenever files were
            # added or deleted. Within a stable path set a hash match is exact.
            file_symbols = fact_cache.lookup(rel) if fact_cache is not None else None
            if file_symbols is None:
                try:
                    if path.stat().st_size > _MAX_FILE_SIZE:
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                file_symbols = extractor.extract_file(root, rel, text).to_dict()
                if fact_cache is not None:
                    fact_cache.record(rel, file_symbols)
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

