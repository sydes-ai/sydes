"""Shallow repository sensing helpers for discovery prioritization."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sydes.core.models import RepoInventory, RepoSenseSummary

MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Pipfile",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
}

BACKEND_SIGNAL_NAMES = {
    "api",
    "server",
    "backend",
    "controllers",
    "routes",
    "handlers",
    "main.py",
    "app.py",
    "server.js",
    "server.ts",
}

LANGUAGE_FAMILY_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "dotnet",
    ".scala": "scala",
}


def _top_level_entries(root: Path) -> tuple[list[str], list[str]]:
    """Return sorted top-level file and directory names."""
    files: list[str] = []
    dirs: list[str] = []

    for entry in root.iterdir():
        if entry.is_dir():
            dirs.append(entry.name)
        elif entry.is_file():
            files.append(entry.name)

    return sorted(files), sorted(dirs)


def sense_repo(repo: str, root: str, inventory: RepoInventory) -> RepoSenseSummary:
    """Infer shallow repo signals from top-level structure and extension mix."""
    root_path = Path(root)
    top_files, top_dirs = _top_level_entries(root_path)
    manifests = [name for name in top_files if name in MANIFEST_NAMES]

    extension_counts: Counter[str] = Counter()
    for item in inventory.files:
        suffix = Path(item.path).suffix.lower()
        if suffix:
            extension_counts[suffix] += 1

    dominant = dict(extension_counts.most_common(8))
    families = {
        LANGUAGE_FAMILY_BY_EXTENSION[suffix]
        for suffix in extension_counts
        if suffix in LANGUAGE_FAMILY_BY_EXTENSION
    }

    backend_signals: list[str] = []
    for signal in sorted(BACKEND_SIGNAL_NAMES):
        if signal in top_files or signal in top_dirs:
            backend_signals.append(signal)
            continue
        if any(signal in Path(item.path).parts for item in inventory.files):
            backend_signals.append(signal)

    notes: list[str] = []
    if not inventory.files:
        notes.append("No non-binary source files captured in shallow inventory.")

    return RepoSenseSummary(
        repo=repo,
        root=root,
        top_level_files=top_files,
        top_level_dirs=top_dirs,
        manifests=manifests,
        dominant_extensions=dominant,
        likely_language_families=sorted(families),
        backend_signals=backend_signals,
        notes=notes,
    )

