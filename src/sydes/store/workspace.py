"""Small JSON-backed local workspace store rooted at ``~/.sydes``.

The store is intentionally simple and local-first:
- no database
- append-friendly JSON index files
- enough metadata for continuity, export, and future UI surfaces
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from sydes.core.models import RepoRef


DEFAULT_STORE_ROOT = Path("~/.sydes")


@dataclass(frozen=True)
class WorkspacePaths:
    """Resolved paths for a single Sydes workspace."""

    root: Path
    workspace_id: str
    workspace_dir: Path
    runs_dir: Path
    artifacts_dir: Path
    index_file: Path


def resolve_store_root(root: Path | None = None) -> Path:
    """Resolve the workspace store root (defaults to ``~/.sydes``)."""
    base = root if root is not None else DEFAULT_STORE_ROOT
    return base.expanduser()


def compute_workspace_id(repos: list[RepoRef]) -> str:
    """Compute a stable workspace id from repository inputs."""
    canonical = [f"{repo.name}={Path(repo.root).as_posix()}" for repo in repos]
    payload = "\n".join(sorted(canonical)).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return digest[:16]


def create_run_id(now: datetime | None = None) -> str:
    """Create a timestamp-based run id suitable for folder/file naming."""
    ts = now if now is not None else datetime.now(tz=UTC)
    stamp = ts.strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:8]}"


def ensure_workspace(workspace_id: str, root: Path | None = None) -> WorkspacePaths:
    """Ensure workspace directories and index file exist for the workspace id."""
    store_root = resolve_store_root(root)
    workspace_dir = store_root / "workspaces" / workspace_id
    runs_dir = workspace_dir / "runs"
    artifacts_dir = workspace_dir / "artifacts"
    index_file = workspace_dir / "index.json"

    runs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    if not index_file.exists():
        index = {
            "workspace_id": workspace_id,
            "store_version": "v1",
            "created_at": datetime.now(tz=UTC).isoformat(),
            "updated_at": datetime.now(tz=UTC).isoformat(),
            "runs": {},
        }
        index_file.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    return WorkspacePaths(
        root=store_root,
        workspace_id=workspace_id,
        workspace_dir=workspace_dir,
        runs_dir=runs_dir,
        artifacts_dir=artifacts_dir,
        index_file=index_file,
    )


def _load_index(index_file: Path, workspace_id: str) -> dict[str, Any]:
    """Load index JSON while tolerating older/minimal index layouts."""
    try:
        raw = json.loads(index_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("workspace_id") != workspace_id:
        raw["workspace_id"] = workspace_id
    raw.setdefault("store_version", "v1")
    now = datetime.now(tz=UTC).isoformat()
    raw.setdefault("created_at", now)
    raw.setdefault("updated_at", now)
    runs = raw.get("runs")
    if not isinstance(runs, dict):
        raw["runs"] = {}
    return raw


def _write_index(index_file: Path, index_payload: dict[str, Any]) -> None:
    """Persist workspace index payload to disk as pretty JSON."""
    index_file.write_text(json.dumps(index_payload, indent=2) + "\n", encoding="utf-8")


def _extract_repo_inputs(payload: Any) -> list[dict[str, Any]]:
    """Extract repo inputs from known artifact payload shapes."""
    if not isinstance(payload, dict):
        return []
    repo_inputs = payload.get("repo_inputs")
    if isinstance(repo_inputs, list):
        return [item for item in repo_inputs if isinstance(item, dict)]
    if "result" in payload and isinstance(payload["result"], dict):
        result_repos = payload["result"].get("repos")
        if isinstance(result_repos, list):
            return [item for item in result_repos if isinstance(item, dict)]
    return []


def _extract_target_route(payload: Any) -> dict[str, Any] | None:
    """Extract target route metadata from known artifact payload shapes."""
    if not isinstance(payload, dict):
        return None
    target = payload.get("target")
    if isinstance(target, dict):
        return {
            "kind": target.get("kind"),
            "method": target.get("method"),
            "path": target.get("path"),
        }
    if "result" in payload and isinstance(payload["result"], dict):
        result_target = payload["result"].get("target")
        if isinstance(result_target, dict):
            return {
                "kind": result_target.get("kind"),
                "method": result_target.get("method"),
                "path": result_target.get("path"),
            }
    return None


def _update_workspace_index(
    paths: WorkspacePaths,
    *,
    run_id: str,
    artifact_name: str,
    artifact_kind: str,
    artifact_file: Path,
    timestamp: str,
    repo_inputs: list[dict[str, Any]],
    target_route: dict[str, Any] | None,
) -> None:
    """Append artifact metadata under workspace index by run id."""
    index = _load_index(paths.index_file, paths.workspace_id)
    runs: dict[str, Any] = index.setdefault("runs", {})
    run_entry = runs.get(run_id)
    if not isinstance(run_entry, dict):
        run_entry = {
            "run_id": run_id,
            "workspace_id": paths.workspace_id,
            "timestamp": timestamp,
            "repo_inputs": repo_inputs,
            "target_route": target_route,
            "artifacts": [],
        }
        runs[run_id] = run_entry
    else:
        run_entry.setdefault("run_id", run_id)
        run_entry.setdefault("workspace_id", paths.workspace_id)
        run_entry.setdefault("timestamp", timestamp)
        if repo_inputs:
            run_entry["repo_inputs"] = repo_inputs
        else:
            run_entry.setdefault("repo_inputs", [])
        if target_route is not None:
            run_entry["target_route"] = target_route
        else:
            run_entry.setdefault("target_route", None)
        run_entry.setdefault("artifacts", [])

    artifacts = run_entry.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        run_entry["artifacts"] = artifacts

    relative_path = artifact_file.relative_to(paths.workspace_dir).as_posix()
    artifact_entry = {
        "artifact_name": artifact_name,
        "artifact_kind": artifact_kind,
        "filename": artifact_file.name,
        "relative_path": relative_path,
        "timestamp": timestamp,
    }
    existing_index = next(
        (
            idx
            for idx, item in enumerate(artifacts)
            if isinstance(item, dict)
            and item.get("artifact_name") == artifact_name
            and item.get("relative_path") == relative_path
        ),
        None,
    )
    if existing_index is None:
        artifacts.append(artifact_entry)
    else:
        artifacts[existing_index] = artifact_entry

    index["updated_at"] = datetime.now(tz=UTC).isoformat()
    _write_index(paths.index_file, index)


def _artifact_timestamp(payload: Any) -> str:
    """Resolve artifact timestamp from payload when available."""
    if isinstance(payload, dict):
        candidate = payload.get("timestamp")
        if isinstance(candidate, str) and candidate:
            return candidate
        metadata = payload.get("artifact_metadata")
        if isinstance(metadata, dict):
            candidate = metadata.get("timestamp")
            if isinstance(candidate, str) and candidate:
                return candidate
    return datetime.now(tz=UTC).isoformat()


def save_run_artifact(
    workspace_id: str,
    run_id: str,
    artifact_name: str,
    payload: Any,
    root: Path | None = None,
) -> Path:
    """Save a JSON artifact and update append-friendly workspace run index."""
    paths = ensure_workspace(workspace_id=workspace_id, root=root)
    run_dir = paths.artifacts_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_file = run_dir / f"{artifact_name}.json"
    timestamp = _artifact_timestamp(payload)
    repo_inputs = _extract_repo_inputs(payload)
    target_route = _extract_target_route(payload)
    artifact_kind = artifact_name
    artifact_metadata = {
        "timestamp": timestamp,
        "artifact_kind": artifact_kind,
        "workspace_id": workspace_id,
        "run_id": run_id,
        "repo_inputs": repo_inputs,
        "target_route": target_route,
    }

    if isinstance(payload, dict):
        to_write = dict(payload)
        existing_meta = to_write.get("artifact_metadata")
        if isinstance(existing_meta, dict):
            merged_meta = dict(existing_meta)
            merged_meta.update(
                {
                    "timestamp": artifact_metadata["timestamp"],
                    "artifact_kind": artifact_metadata["artifact_kind"],
                    "workspace_id": artifact_metadata["workspace_id"],
                    "run_id": artifact_metadata["run_id"],
                    "repo_inputs": artifact_metadata["repo_inputs"],
                    "target_route": artifact_metadata["target_route"],
                }
            )
            to_write["artifact_metadata"] = merged_meta
        else:
            to_write["artifact_metadata"] = artifact_metadata
    else:
        to_write = {
            "payload": payload,
            "artifact_metadata": artifact_metadata,
        }

    artifact_file.write_text(json.dumps(to_write, indent=2) + "\n", encoding="utf-8")
    _update_workspace_index(
        paths,
        run_id=run_id,
        artifact_name=artifact_name,
        artifact_kind=artifact_kind,
        artifact_file=artifact_file,
        timestamp=artifact_metadata["timestamp"],
        repo_inputs=repo_inputs,
        target_route=target_route,
    )
    return artifact_file


def list_workspace_runs(workspace_id: str, root: Path | None = None) -> list[dict[str, Any]]:
    """List run metadata for a workspace from local index JSON."""
    paths = ensure_workspace(workspace_id=workspace_id, root=root)
    index = _load_index(paths.index_file, workspace_id)
    runs = index.get("runs", {})
    if not isinstance(runs, dict):
        return []
    ordered = sorted(
        (item for item in runs.values() if isinstance(item, dict)),
        key=lambda item: item.get("timestamp") or "",
        reverse=True,
    )
    return ordered


def list_workspace_artifacts(
    workspace_id: str,
    run_id: str,
    root: Path | None = None,
) -> list[dict[str, Any]]:
    """List saved artifact metadata for one workspace run id."""
    paths = ensure_workspace(workspace_id=workspace_id, root=root)
    index = _load_index(paths.index_file, workspace_id)
    runs = index.get("runs", {})
    if not isinstance(runs, dict):
        return []
    run_entry = runs.get(run_id)
    if not isinstance(run_entry, dict):
        return []
    artifacts = run_entry.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, dict)]
