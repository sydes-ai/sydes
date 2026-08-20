"""Single-pass repository scan shared by every verify-change analysis stage.

Reuses the existing Sydes ignore list and file-role classifier rather than
introducing a second repository indexer. Files are read once and handed to the
symbol index, route extraction, event detection, test index, and runtime
dependency inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sydes.discover.repo_map import IGNORED_DIRS
from sydes.ingest.file_roles import (
    FILE_ROLE_SOURCE_ROUTE_CANDIDATE,
    FILE_ROLE_TEST_USAGE_CANDIDATE,
    classify_candidate_file_role,
)

SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".kt",
}

CONFIG_EXTENSIONS = {".yml", ".yaml", ".toml", ".ini", ".properties", ".json", ".tf"}

CONFIG_BASENAMES = {
    ".env",
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.local",
    ".env.development",
    ".env.test",
    "dockerfile",
    "makefile",
    "procfile",
}

MAX_FILE_BYTES = 1_500_000
MAX_SCANNED_FILES = 8_000

# Directories that hold vendored or bundled assets rather than project source.
# Indexing them produces convincing-looking but bogus call edges: a bundled
# frontend theme defining `query()` will happily absorb a `db.query()` call.
VENDORED_DIRS = {
    "public",
    "static",
    "vendor",
    "vendors",
    "assets",
    "third_party",
    "third-party",
    "site-packages",
    "bower_components",
    "migrations",
}

# A bundled/minified file has very long lines; treat that as the signal rather
# than relying on a `.min.` naming convention that many bundlers skip.
MAX_MEAN_LINE_LENGTH = 400


@dataclass(slots=True)
class ScannedFile:
    """One text file read during the repository scan."""

    repo: str
    path: str
    text: str
    role: str
    extension: str
    line_count: int = 0

    @property
    def is_source(self) -> bool:
        """True for language source files (tests included)."""
        return self.extension in SOURCE_EXTENSIONS

    @property
    def is_test(self) -> bool:
        """True when the path looks like a test/spec file."""
        return self.role == FILE_ROLE_TEST_USAGE_CANDIDATE

    @property
    def is_app_source(self) -> bool:
        """True for non-test language source files."""
        return self.is_source and self.role == FILE_ROLE_SOURCE_ROUTE_CANDIDATE


@dataclass(slots=True)
class RepoScan:
    """Result of scanning one repository root."""

    repo: str
    root: Path
    files: list[ScannedFile] = field(default_factory=list)
    skipped_files: int = 0
    notes: list[str] = field(default_factory=list)

    def by_path(self) -> dict[str, ScannedFile]:
        """Index scanned files by repo-relative path."""
        return {item.path: item for item in self.files}

    def source_files(self) -> list[ScannedFile]:
        """Return non-test language source files."""
        return [item for item in self.files if item.is_app_source]

    def test_files(self) -> list[ScannedFile]:
        """Return test/spec files."""
        return [item for item in self.files if item.is_test and item.is_source]


def _is_vendored(relative_path: str) -> bool:
    """Return True when a path sits under a vendored/bundled asset directory."""
    parts = relative_path.lower().split("/")[:-1]
    return any(part in VENDORED_DIRS for part in parts)


def _is_bundled(text: str, line_count: int) -> bool:
    """Return True when file content looks machine-generated/minified."""
    if line_count <= 0:
        return False
    return len(text) / line_count > MAX_MEAN_LINE_LENGTH


def _is_interesting(path: Path) -> bool:
    """Return True when a file is worth reading for change verification."""
    ext = path.suffix.lower()
    if ext in SOURCE_EXTENSIONS or ext in CONFIG_EXTENSIONS:
        return True
    name = path.name.lower()
    if name in CONFIG_BASENAMES or name.startswith(".env"):
        return True
    return ext in {".md", ".adoc"} and name in {"readme.md", "readme.adoc"}


def scan_repository(repo_name: str, repo_root: Path | str, *, max_files: int = MAX_SCANNED_FILES) -> RepoScan:
    """Walk a repository once, reading text files relevant to verification."""
    root = Path(repo_root).expanduser().resolve()
    scan = RepoScan(repo=repo_name, root=root)

    for dirpath, dirnames, filenames in root.walk():
        dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
        for filename in filenames:
            path = dirpath / filename
            if not _is_interesting(path):
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix.lower() in SOURCE_EXTENSIONS and _is_vendored(relative):
                scan.skipped_files += 1
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    scan.skipped_files += 1
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                scan.skipped_files += 1
                continue
            line_count = text.count("\n") + 1
            if path.suffix.lower() in SOURCE_EXTENSIONS and _is_bundled(text, line_count):
                scan.skipped_files += 1
                continue
            scan.files.append(
                ScannedFile(
                    repo=repo_name,
                    path=relative,
                    text=text,
                    role=classify_candidate_file_role(relative),
                    extension=path.suffix.lower(),
                    line_count=line_count,
                )
            )
            if len(scan.files) >= max_files:
                scan.notes.append(f"scan_truncated_at={max_files}_files")
                scan.files.sort(key=lambda item: item.path)
                return scan

    scan.files.sort(key=lambda item: item.path)
    return scan
