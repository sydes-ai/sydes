"""Small JSON-backed local workspace store rooted at ``~/.sydes``."""

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
            "created_at": datetime.now(tz=UTC).isoformat(),
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


def save_run_artifact(
    workspace_id: str,
    run_id: str,
    artifact_name: str,
    payload: Any,
    root: Path | None = None,
) -> Path:
    """Save a JSON artifact for a run and return the output file path."""
    paths = ensure_workspace(workspace_id=workspace_id, root=root)
    run_dir = paths.artifacts_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_file = run_dir / f"{artifact_name}.json"
    artifact_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return artifact_file

