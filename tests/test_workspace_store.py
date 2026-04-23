"""Tests for local Sydes workspace store helpers."""

import json
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.store.workspace import (
    compute_workspace_id,
    create_run_id,
    ensure_workspace,
    list_workspace_artifacts,
    list_workspace_runs,
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
    assert index["store_version"] == "v1"
    assert isinstance(index.get("runs"), dict)


def test_save_run_artifact_writes_json(tmp_path: Path) -> None:
    """Artifacts should be persisted as pretty JSON under run-scoped folders."""
    run_id = create_run_id()
    artifact_file = save_run_artifact(
        workspace_id="abc123",
        run_id=run_id,
        artifact_name="trace_result",
        payload={
            "timestamp": "2026-04-23T12:00:00Z",
            "repo_inputs": [{"name": "api", "root": "/tmp/api"}],
            "target": {"kind": "api_route", "method": "POST", "path": "/users"},
            "result": {
                "version": "v1",
                "target": {"kind": "api_route", "method": "POST", "path": "/users"},
                "repos": [{"name": "api", "root": "/tmp/api"}],
                "nodes": [],
                "edges": [],
                "flows": [],
                "tests": [],
                "unknowns": [],
                "notes": [],
                "summary": {"key_flow_id": None, "confidence": 0.4},
            },
        },
        root=tmp_path,
    )

    assert artifact_file.exists()
    payload = json.loads(artifact_file.read_text(encoding="utf-8"))
    assert payload["result"]["version"] == "v1"
    assert payload["artifact_metadata"]["workspace_id"] == "abc123"
    assert payload["artifact_metadata"]["run_id"] == run_id
    assert payload["artifact_metadata"]["artifact_kind"] == "trace_result"
    assert payload["artifact_metadata"]["target_route"]["path"] == "/users"

    runs = list_workspace_runs(workspace_id="abc123", root=tmp_path)
    assert runs
    assert runs[0]["run_id"] == run_id
    assert runs[0]["target_route"]["path"] == "/users"

    artifacts = list_workspace_artifacts(workspace_id="abc123", run_id=run_id, root=tmp_path)
    assert artifacts
    assert artifacts[0]["artifact_name"] == "trace_result"
    assert artifacts[0]["artifact_kind"] == "trace_result"
    assert artifacts[0]["filename"] == "trace_result.json"
