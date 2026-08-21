"""Thin adapter over Sydes' shared ingest layer.

Test-runner detection and runtime-dependency inference need to look at files by
path and content. This module supplies that view by composing the existing
shared ingest primitives — it deliberately owns no walking, ignore list, or role
classification of its own, so there remains exactly one repository-reading
implementation in Sydes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    classify_candidate_file_role,
)
from sydes.ingest.inventory import build_repo_inventory
from sydes.ingest.readers import read_text_file_safely

SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rb", ".php", ".cs", ".kt"}
CONFIG_EXTENSIONS = {".yml", ".yaml", ".toml", ".ini", ".properties", ".json", ".tf"}

_MAX_READ_BYTES = 1_000_000
_MAX_READ_CHARS = 400_000
_MAX_READ_LINES = 20_000


@dataclass(slots=True)
class SourceFile:
    """One readable repository file, addressed by repo-relative path."""

    repo: str
    path: str
    text: str
    role: str
    extension: str

    @property
    def is_source(self) -> bool:
        """True for language source files, tests included."""
        return self.extension in SOURCE_EXTENSIONS

    @property
    def is_test(self) -> bool:
        """True when the path is classified as a test/spec file."""
        return self.role == FILE_ROLE_TEST_USAGE_CANDIDATE

    @property
    def is_app_source(self) -> bool:
        """True for non-test language source files."""
        return self.is_source and self.role == FILE_ROLE_SOURCE_ROUTE_CANDIDATE


@dataclass(slots=True)
class RepoFiles:
    """Readable files for one repository."""

    repo: str
    root: Path
    files: list[SourceFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_path(self) -> dict[str, SourceFile]:
        """Index files by repo-relative path."""
        return {item.path: item for item in self.files}

    def text_of(self, relative_path: str) -> str | None:
        """Return one file's text, if it was read."""
        for item in self.files:
            if item.path == relative_path:
                return item.text
        return None

    def tests(self) -> list[SourceFile]:
        """Return test/spec source files."""
        return [item for item in self.files if item.is_test and item.is_source]


def _is_relevant(path: str) -> bool:
    """Return True when a file is worth reading for verification support."""
    suffix = Path(path).suffix.lower()
    if suffix in SOURCE_EXTENSIONS or suffix in CONFIG_EXTENSIONS:
        return True
    name = Path(path).name.lower()
    return name.startswith(".env") or name in {"dockerfile", "makefile", "procfile"}


def load_repo_files(repo_name: str, repo_root: Path | str, *, max_files: int = 8_000) -> RepoFiles:
    """Load repository files through the shared inventory and reader."""
    root = Path(repo_root).expanduser().resolve()
    inventory = build_repo_inventory(
        repo_name, root, include_sizes=False, max_files=max_files
    )
    loaded = RepoFiles(repo=repo_name, root=root)

    for item in inventory.files:
        if not _is_relevant(item.path):
            continue
        read = read_text_file_safely(
            repo=repo_name,
            repo_root=root,
            relative_path=item.path,
            max_read_bytes=_MAX_READ_BYTES,
            max_read_chars=_MAX_READ_CHARS,
            max_read_lines=_MAX_READ_LINES,
        )
        if read.skipped or read.snippet is None:
            continue
        loaded.files.append(
            SourceFile(
                repo=repo_name,
                path=item.path,
                text=read.snippet.text,
                role=classify_candidate_file_role(item.path),
                extension=Path(item.path).suffix.lower(),
            )
        )

    loaded.files.sort(key=lambda entry: entry.path)
    loaded.notes.append(f"files_loaded={len(loaded.files)}")
    return loaded
