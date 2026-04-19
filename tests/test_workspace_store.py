"""Tests for local Sydes workspace store helpers."""

import json
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.store.workspace import (
    compute_workspace_id,
    create_run_id,
    ensure_workspace,
    resolve_store_root,
    save_run_artifact,
)


def test_resolve_store_root_defaults_to_user_sydes_dir() -> None:
    """Default root should resolve to ~/.sydes."""
    root = resolve_store_root()

    assert root.name == ".sydes"
    assert root.is_absolute()


def test_compute_workspace_id_is_stable_for_repo_set() -> None:
    """Workspace id should be stable regardless of repo order."""
    repos_a = [RepoRef(name="api", root="./api"), RepoRef(name="gateway", root="./gw")]
    repos_b = [RepoRef(name="gateway", root="./gw"), RepoRef(name="api", root="./api")]

    assert compute_workspace_id(repos_a) == compute_workspace_id(repos_b)


def test_ensure_workspace_creates_layout_and_index(tmp_path: Path) -> None:
    """Workspace directories and index file should be created when missing."""
    paths = ensure_workspace(workspace_id="abc123", root=tmp_path)

    assert paths.runs_dir.exists()
    assert paths.artifacts_dir.exists()
    assert paths.index_file.exists()

    index = json.loads(paths.index_file.read_text(encoding="utf-8"))
    assert index["workspace_id"] == "abc123"


def test_save_run_artifact_writes_json(tmp_path: Path) -> None:
    """Artifacts should be persisted as pretty JSON under run-scoped folders."""
    artifact_file = save_run_artifact(
        workspace_id="abc123",
        run_id=create_run_id(),
        artifact_name="trace_result",
        payload={"version": "v1", "nodes": []},
        root=tmp_path,
    )

    assert artifact_file.exists()
    payload = json.loads(artifact_file.read_text(encoding="utf-8"))
    assert payload["version"] == "v1"

