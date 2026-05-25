"""Tests for deterministic repository map builder."""

from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.repo_map import build_repo_map


def _write_mini_repo(root: Path) -> None:
    (root / "src" / "routes" / "apis").mkdir(parents=True)
    (root / "src" / "controllers").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "dist").mkdir()
    (root / "package.json").write_text('{"name":"demo"}', encoding="utf-8")
    (root / "src" / "app.ts").write_text("export const app = {};\n", encoding="utf-8")
    (root / "src" / "routes" / "apis" / "tasks-api-router.ts").write_text("router.get('/tasks', h)\n", encoding="utf-8")
    (root / "src" / "controllers" / "tasks-controller.ts").write_text("export function c(){}\n", encoding="utf-8")
    (root / "node_modules" / "huge.js").write_text("ignored\n", encoding="utf-8")
    (root / "dist" / "bundle.js").write_text("ignored\n", encoding="utf-8")


def test_repo_map_detects_signals_and_ignores_noise_dirs(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_mini_repo(repo_root)

    payload = build_repo_map(RepoRef(name="demo", root=str(repo_root)))

    assert "node_modules" in payload["ignored_dirs"]
    assert "dist" in payload["ignored_dirs"]
    assert "package.json" in payload["manifests"]
    assert "src/app.ts" in payload["entrypoint_candidates"]
    assert "src/routes/apis" in payload["candidate_route_dirs"]
    assert "src/controllers" in payload["candidate_controller_dirs"]
    assert "src" in payload["candidate_backend_dirs"]
    assert ".ts" in payload["extension_counts"]
    file_paths = {item["path"] for item in payload["files"]}
    assert "node_modules/huge.js" not in file_paths
    assert "dist/bundle.js" not in file_paths


def test_repo_map_handles_empty_repo(tmp_path: Path) -> None:
    repo_root = tmp_path / "empty"
    repo_root.mkdir()
    payload = build_repo_map(RepoRef(name="empty", root=str(repo_root)))
    assert payload["repo"] == "empty"
    assert payload["candidate_route_dirs"] == []
    assert payload["summary"]["total_files_seen"] == 0


def test_repo_map_paths_are_relative_and_stable(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_mini_repo(repo_root)
    payload = build_repo_map(RepoRef(name="demo", root=str(repo_root)))
    assert all(not path.startswith(str(repo_root)) for path in payload["candidate_route_dirs"])
    assert all("/" in path or path == "package.json" for path in payload["entrypoint_candidates"] + payload["manifests"])
