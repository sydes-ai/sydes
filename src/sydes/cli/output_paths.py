"""Shared helpers for resolving CLI output paths.

These helpers support both explicit file targets and directory-style artifact
targets so command callers can pass either shape safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


def _ensure_parent_directory(parent: Path) -> None:
    """Ensure a parent directory exists and is a directory."""
    if parent.exists() and not parent.is_dir():
        raise ValueError(f"Output parent exists but is not a directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)


def _looks_like_json_file(path: Path) -> bool:
    """Return true when a path clearly looks like an explicit JSON file."""
    return path.suffix.lower() == ".json"


def resolve_output_file_path(output: Path, *, default_filename: str) -> Path:
    """Resolve output to a concrete file path.

    Rules:
    - Existing directory -> write to <dir>/<default_filename>
    - Existing file -> write to that file
    - Missing .json path -> treat as explicit file
    - Missing non-.json path -> treat as directory and write default filename
    """
    if output.exists():
        if output.is_dir():
            return output / default_filename
        if output.is_file():
            return output
        raise ValueError(f"Output path exists but is not a file or directory: {output}")

    if _looks_like_json_file(output):
        _ensure_parent_directory(output.parent)
        return output

    _ensure_parent_directory(output.parent)
    output.mkdir(parents=True, exist_ok=True)
    return output / default_filename


@dataclass(frozen=True)
class TraceOutputTarget:
    """Resolved trace output target."""

    kind: Literal["file", "directory"]
    path: Path


def resolve_trace_output_target(output: Path) -> TraceOutputTarget:
    """Resolve trace output as either explicit JSON file or artifact directory."""
    if output.exists():
        if output.is_dir():
            return TraceOutputTarget(kind="directory", path=output)
        if output.is_file():
            return TraceOutputTarget(kind="file", path=output)
        raise ValueError(f"Output path exists but is not a file or directory: {output}")

    if _looks_like_json_file(output):
        _ensure_parent_directory(output.parent)
        return TraceOutputTarget(kind="file", path=output)

    _ensure_parent_directory(output.parent)
    output.mkdir(parents=True, exist_ok=True)
    return TraceOutputTarget(kind="directory", path=output)


def write_output_text(path: Path, content: str) -> None:
    """Write rendered output to a file path with parent directory creation."""
    _ensure_parent_directory(path.parent)
    path.write_text(content + "\n", encoding="utf-8")
