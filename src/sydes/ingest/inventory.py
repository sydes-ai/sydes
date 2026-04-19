"""Shallow repository file inventory helpers."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from sydes.core.models import InventoryFile, RepoInventory

DEFAULT_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "coverage",
    ".pytest_cache",
    ".mypy_cache",
}

DEFAULT_BINARY_SUFFIXES = {
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


def _is_binary_like(path: Path, binary_suffixes: set[str]) -> bool:
    """Return true when a file likely contains binary content."""
    if path.suffix.lower() in binary_suffixes:
        return True

    try:
        with path.open("rb") as handle:
            chunk = handle.read(1024)
    except OSError:
        return True
    return b"\x00" in chunk


def build_repo_inventory(
    repo_name: str,
    repo_root: Path | str,
    *,
    include_sizes: bool = True,
    max_files: int | None = None,
    skip_dirs: Iterable[str] | None = None,
    binary_suffixes: Iterable[str] | None = None,
) -> RepoInventory:
    """Collect a shallow file inventory for a repository root."""
    root = Path(repo_root).expanduser().resolve()
    skip = set(skip_dirs or DEFAULT_SKIP_DIRS)
    binary = {suffix.lower() for suffix in (binary_suffixes or DEFAULT_BINARY_SUFFIXES)}
    files: list[InventoryFile] = []
    total_size = 0

    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name not in skip]

        for filename in filenames:
            file_path = dirpath / filename
            if _is_binary_like(file_path, binary):
                continue

            rel_path = file_path.relative_to(root).as_posix()
            size = None
            if include_sizes:
                try:
                    size = file_path.stat().st_size
                except OSError:
                    size = None
                if size is not None:
                    total_size += size

            files.append(InventoryFile(path=rel_path, size_bytes=size))
            if max_files is not None and len(files) >= max_files:
                return RepoInventory(
                    repo=repo_name,
                    root=str(root),
                    files=files,
                    file_count=len(files),
                    total_size_bytes=total_size if include_sizes else None,
                )

    return RepoInventory(
        repo=repo_name,
        root=str(root),
        files=files,
        file_count=len(files),
        total_size_bytes=total_size if include_sizes else None,
    )
