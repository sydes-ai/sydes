"""Lightweight file-role classification for discovery candidates.

This phase uses path/name heuristics only. It does not perform framework-
specific parsing and does not exclude files by itself.
"""

from __future__ import annotations

from pathlib import PurePosixPath

FILE_ROLE_SOURCE_ROUTE_CANDIDATE = "source_route_candidate"
FILE_ROLE_TEST_USAGE_CANDIDATE = "test_usage_candidate"
FILE_ROLE_DOCS_CANDIDATE = "docs_candidate"
FILE_ROLE_UNKNOWN = "unknown"

SOURCE_ROUTE_SUFFIXES = {
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
}
DOC_SUFFIXES = {".md", ".rst", ".adoc"}
TEST_SUFFIXES = {
    ".test.ts",
    ".spec.ts",
    ".test.js",
    ".spec.js",
    ".test.jsx",
    ".spec.jsx",
    ".test.tsx",
    ".spec.tsx",
}
TEST_DIR_MARKERS = {"tests", "test", "__tests__", "spec"}


def classify_candidate_file_role(path: str) -> str:
    """Classify a candidate file path into source/test/docs/unknown role."""
    normalized = path.replace("\\", "/").strip()
    p = PurePosixPath(normalized)
    filename = p.name.lower()
    parts = [part.lower() for part in p.parts]
    lower_path = normalized.lower()

    if filename == "readme.md":
        return FILE_ROLE_DOCS_CANDIDATE
    if "docs/" in lower_path or any(part == "docs" for part in parts):
        return FILE_ROLE_DOCS_CANDIDATE
    if any(filename.endswith(suffix) for suffix in DOC_SUFFIXES):
        return FILE_ROLE_DOCS_CANDIDATE

    if any(part in TEST_DIR_MARKERS for part in parts):
        return FILE_ROLE_TEST_USAGE_CANDIDATE
    if filename.startswith("test_") and filename.endswith(".py"):
        return FILE_ROLE_TEST_USAGE_CANDIDATE
    if filename.endswith("_test.py"):
        return FILE_ROLE_TEST_USAGE_CANDIDATE
    if any(filename.endswith(suffix) for suffix in TEST_SUFFIXES):
        return FILE_ROLE_TEST_USAGE_CANDIDATE

    if p.suffix.lower() in SOURCE_ROUTE_SUFFIXES:
        return FILE_ROLE_SOURCE_ROUTE_CANDIDATE
    return FILE_ROLE_UNKNOWN
