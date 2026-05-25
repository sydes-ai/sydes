"""Incremental cache for route discovery artifacts.

Cache is local-only and repository-state keyed. It allows routes discovery to
reuse repo_map/route_index/route_graph/coverage/planner artifacts when source
files are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any

from sydes.core.models import RepoRef
from sydes.discover.repo_map import IGNORED_DIRS
from sydes.store.workspace import ensure_workspace

DISCOVERY_CACHE_VERSION = "phase32h-v1"
CACHE_SUBDIR = "cache/discovery"
MANIFEST_FILE = "manifest.json"
RELEVANT_EXTS = {
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
    ".yml",
    ".yaml",
    ".xml",
}

ARTIFACT_NAMES = [
    "repo_map",
    "route_index",
    "route_graph_facts",
    "discovery_coverage",
    "routing_pattern_plan",
    "routing_pattern_execution",
    "routes_discovery",
]


@dataclass(frozen=True)
class DiscoveryCacheStatus:
    hit: bool
    reason: str
    changed_files: int = 0


@dataclass(frozen=True)
class DiscoveryCacheBundle:
    manifest: dict[str, Any]
    artifacts: dict[str, Any]


def _cache_key(repos: list[RepoRef]) -> str:
    canonical = [f"{repo.name}={Path(repo.root).expanduser().resolve().as_posix()}" for repo in repos]
    digest = hashlib.sha256("\n".join(sorted(canonical)).encode("utf-8")).hexdigest()
    return digest[:16]


def _cache_dir(workspace_id: str, repos: list[RepoRef], *, root: Path | None = None) -> Path:
    workspace = ensure_workspace(workspace_id=workspace_id, root=root)
    return workspace.workspace_dir / CACHE_SUBDIR / _cache_key(repos)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _should_include(path: Path) -> bool:
    return path.suffix.lower() in RELEVANT_EXTS


def collect_repo_file_snapshot(repos: list[RepoRef]) -> dict[str, dict[str, Any]]:
    """Collect deterministic file metadata snapshot for relevant discovery files."""
    snapshot: dict[str, dict[str, Any]] = {}
    for repo in repos:
        root = Path(repo.root).expanduser().resolve()
        for dirpath, dirnames, filenames in root.walk():
            dirnames[:] = [name for name in dirnames if name.lower() not in IGNORED_DIRS]
            for filename in filenames:
                path = dirpath / filename
                if not _should_include(path):
                    continue
                rel = path.relative_to(root).as_posix()
                key = f"{repo.name}:{rel}"
                try:
                    stat = path.stat()
                except OSError:
                    continue
                snapshot[key] = {
                    "repo": repo.name,
                    "relative_path": rel,
                    "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
                    "size": stat.st_size,
                }
    return dict(sorted(snapshot.items()))


def _manifest_path(cache_dir: Path) -> Path:
    return cache_dir / MANIFEST_FILE


def load_cache_bundle(
    workspace_id: str,
    repos: list[RepoRef],
    *,
    llm_policy: str,
    model_fingerprint: str | None,
    root: Path | None = None,
) -> tuple[DiscoveryCacheStatus, DiscoveryCacheBundle | None]:
    """Load cache when manifest matches repository fingerprint."""
    cache_dir = _cache_dir(workspace_id, repos, root=root)
    manifest_path = _manifest_path(cache_dir)
    if not manifest_path.exists():
        return DiscoveryCacheStatus(hit=False, reason="cache_missing"), None

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DiscoveryCacheStatus(hit=False, reason="manifest_invalid"), None

    if not isinstance(manifest, dict):
        return DiscoveryCacheStatus(hit=False, reason="manifest_invalid"), None

    if manifest.get("discovery_version") != DISCOVERY_CACHE_VERSION:
        return DiscoveryCacheStatus(hit=False, reason="version_mismatch"), None
    if manifest.get("llm_policy") != llm_policy:
        return DiscoveryCacheStatus(hit=False, reason="policy_mismatch"), None
    if manifest.get("model_fingerprint") != model_fingerprint:
        return DiscoveryCacheStatus(hit=False, reason="model_mismatch"), None

    current = collect_repo_file_snapshot(repos)
    cached = manifest.get("files", {})
    if not isinstance(cached, dict):
        return DiscoveryCacheStatus(hit=False, reason="manifest_invalid"), None

    changed = 0
    for key, meta in current.items():
        old = cached.get(key)
        if not isinstance(old, dict):
            changed += 1
            continue
        if old.get("mtime_ns") != meta.get("mtime_ns") or old.get("size") != meta.get("size"):
            changed += 1
            # Optional hash reliability check when metadata differs.
            repo_name = meta["repo"]
            repo_root = next((Path(r.root).expanduser().resolve() for r in repos if r.name == repo_name), None)
            if repo_root is not None:
                file_path = repo_root / meta["relative_path"]
                if file_path.exists():
                    current_hash = _sha256_file(file_path)
                    old_hash = old.get("sha256")
                    if isinstance(old_hash, str) and old_hash == current_hash:
                        changed -= 1

    removed = set(cached.keys()) - set(current.keys())
    changed += len(removed)

    if changed > 0:
        return DiscoveryCacheStatus(hit=False, reason="changed_files", changed_files=changed), None

    artifacts: dict[str, Any] = {}
    for name in ARTIFACT_NAMES:
        path = cache_dir / f"{name}.json"
        if not path.exists():
            if name == "routing_pattern_execution":
                continue
            return DiscoveryCacheStatus(hit=False, reason=f"missing_artifact:{name}"), None
        try:
            artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return DiscoveryCacheStatus(hit=False, reason=f"invalid_artifact:{name}"), None

    return DiscoveryCacheStatus(hit=True, reason="cache_hit"), DiscoveryCacheBundle(manifest=manifest, artifacts=artifacts)


def save_cache_bundle(
    workspace_id: str,
    repos: list[RepoRef],
    *,
    llm_policy: str,
    model_fingerprint: str | None,
    artifacts: dict[str, Any],
    root: Path | None = None,
) -> Path:
    """Persist cache manifest and artifact payloads for later reuse."""
    cache_dir = _cache_dir(workspace_id, repos, root=root)
    cache_dir.mkdir(parents=True, exist_ok=True)

    snapshot = collect_repo_file_snapshot(repos)
    for key, meta in snapshot.items():
        repo_name = meta["repo"]
        repo_root = next((Path(r.root).expanduser().resolve() for r in repos if r.name == repo_name), None)
        if repo_root is None:
            continue
        file_path = repo_root / meta["relative_path"]
        if file_path.exists():
            meta["sha256"] = _sha256_file(file_path)

    now = datetime.now(tz=UTC).isoformat()
    manifest = {
        "version": "v1",
        "created_at": now,
        "updated_at": now,
        "discovery_version": DISCOVERY_CACHE_VERSION,
        "llm_policy": llm_policy,
        "model_fingerprint": model_fingerprint,
        "repos": [repo.model_dump() for repo in repos],
        "files": snapshot,
        "artifact_versions": {name: "v1" for name in ARTIFACT_NAMES if name in artifacts},
    }

    for name, payload in artifacts.items():
        (cache_dir / f"{name}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    _manifest_path(cache_dir).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return cache_dir
