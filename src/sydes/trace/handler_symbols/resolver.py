"""Lightweight local import resolution for JS/TS-style modules."""

from __future__ import annotations

from pathlib import Path


def resolve_local_import(
    repo_root: Path, importer_relative_path: str, source: str
) -> str | None:
    """Resolve local relative import to a repo-relative source file path."""
    if not source.startswith("."):
        return None
    importer_dir = (repo_root / importer_relative_path).parent
    source_path = (importer_dir / source).resolve()
    candidates: list[Path] = []
    if source_path.suffix:
        candidates.append(source_path)
    else:
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            candidates.append(source_path.with_suffix(ext))
        for index_name in ("index.ts", "index.tsx", "index.js", "index.jsx"):
            candidates.append(source_path / index_name)
    for candidate in candidates:
        try:
            relative = candidate.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if candidate.is_file():
            return relative
    return None

