"""Deterministic repository map builder for hierarchical discovery planning."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from sydes.core.models import RepoRef

IGNORED_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    ".next",
    "coverage",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "target",
    "out",
    "bin",
    "obj",
    "vendor",
}

ROUTE_PATH_TOKENS = {"routes", "router", "routers", "api", "apis", "endpoints"}
CONTROLLER_PATH_TOKENS = {"controllers", "controller", "handlers", "views"}
BACKEND_PATH_TOKENS = {
    "routes",
    "router",
    "routers",
    "api",
    "apis",
    "controllers",
    "controller",
    "server",
    "backend",
    "app",
    "src",
    "handlers",
    "views",
    "endpoints",
}
DOCS_TEST_TOKENS = {"docs", "test", "tests", "__tests__", "examples", "fixtures", "mocks"}

MANIFEST_FILES = {
    "package.json",
    "tsconfig.json",
    "nest-cli.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "manage.py",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "go.mod",
    "cargo.toml",
    "docker-compose.yml",
    "dockerfile",
}

ENTRYPOINT_BASENAMES = {
    "app.ts",
    "app.js",
    "server.ts",
    "server.js",
    "main.py",
    "app.py",
    "manage.py",
    "application.java",
    "main.go",
}

TEXT_SOURCE_EXTS = {
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".java",
    ".go",
    ".rb",
    ".php",
    ".cs",
    ".kt",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".xml",
    ".md",
    ".rst",
}


def _folder_signals(relative_dir: str) -> list[str]:
    parts = [part.lower() for part in Path(relative_dir).parts if part and part != "."]
    signals: list[str] = []
    for token in parts:
        if token in BACKEND_PATH_TOKENS:
            signals.append(f"path:{token}")
        if token in DOCS_TEST_TOKENS:
            signals.append(f"path:{token}")
    return sorted(set(signals))


def _is_entrypoint(path: str) -> bool:
    lower = path.lower()
    name = Path(lower).name
    if name in ENTRYPOINT_BASENAMES:
        return True
    return "src/main" in lower


def build_repo_map(repo: RepoRef) -> dict:
    """Build deterministic structural map for a repository root."""
    root = Path(repo.root).expanduser().resolve()
    extension_counts: Counter[str] = Counter()
    manifests: list[str] = []
    files: list[dict] = []
    folders: list[dict] = []
    candidate_backend_dirs: set[str] = set()
    candidate_route_dirs: set[str] = set()
    candidate_controller_dirs: set[str] = set()
    entrypoint_candidates: set[str] = set()

    total_files_seen = 0
    total_dirs_seen = 0
    files_included = 0
    files_skipped = 0

    for dirpath, dirnames, filenames in root.walk():
        rel_dir = dirpath.relative_to(root).as_posix()
        total_dirs_seen += 1

        original_dirnames = list(dirnames)
        dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
        skipped_children = len(original_dirnames) - len(dirnames)
        if skipped_children > 0:
            files_skipped += skipped_children

        signals = _folder_signals(rel_dir)
        if rel_dir != ".":
            parts = [part.lower() for part in Path(rel_dir).parts]
            if any(token in parts for token in BACKEND_PATH_TOKENS):
                candidate_backend_dirs.add(rel_dir)
            if any(token in parts for token in ROUTE_PATH_TOKENS):
                candidate_route_dirs.add(rel_dir)
            if any(token in parts for token in CONTROLLER_PATH_TOKENS):
                candidate_controller_dirs.add(rel_dir)
            folders.append(
                {
                    "path": rel_dir,
                    "depth": len(Path(rel_dir).parts),
                    "file_count": len(filenames),
                    "subdir_count": len(dirnames),
                    "signals": signals,
                }
            )

        for filename in filenames:
            total_files_seen += 1
            path = dirpath / filename
            rel_path = path.relative_to(root).as_posix()
            ext = path.suffix.lower()
            if ext and ext not in TEXT_SOURCE_EXTS:
                files_skipped += 1
                continue
            files_included += 1
            extension_counts[ext or "<none>"] += 1

            lower_name = filename.lower()
            role: str | None = None
            file_signals: list[str] = []
            if lower_name in MANIFEST_FILES:
                role = "manifest"
                manifests.append(rel_path)
                file_signals.append(f"manifest:{lower_name}")
            if _is_entrypoint(rel_path):
                entrypoint_candidates.add(rel_path)
                file_signals.append("entrypoint")

            if role is not None or file_signals:
                files.append(
                    {
                        "path": rel_path,
                        "ext": ext or "",
                        "role": role or "source",
                        "signals": sorted(set(file_signals)),
                    }
                )

    folders.sort(key=lambda item: item["path"])
    files.sort(key=lambda item: item["path"])

    return {
        "repo": repo.name,
        "root": str(root),
        "folders": folders,
        "files": files,
        "extension_counts": dict(sorted(extension_counts.items())),
        "manifests": sorted(set(manifests)),
        "candidate_backend_dirs": sorted(candidate_backend_dirs),
        "candidate_route_dirs": sorted(candidate_route_dirs),
        "candidate_controller_dirs": sorted(candidate_controller_dirs),
        "entrypoint_candidates": sorted(entrypoint_candidates),
        "ignored_dirs": sorted(IGNORED_DIRS),
        "summary": {
            "total_files_seen": total_files_seen,
            "total_dirs_seen": total_dirs_seen,
            "files_included": files_included,
            "files_skipped": files_skipped,
        },
    }


def build_repo_map_batch(repos: list[RepoRef]) -> dict:
    """Build repo maps for many repositories in one payload."""
    return {
        "version": "v1",
        "repos": [build_repo_map(repo) for repo in repos],
    }
