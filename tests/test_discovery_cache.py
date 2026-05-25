"""Tests for incremental discovery cache manifest and invalidation."""

from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.discovery_cache import (
    DiscoveryCacheBundle,
    DiscoveryCacheStatus,
    collect_repo_file_snapshot,
    load_cache_bundle,
    save_cache_bundle,
)


def _write_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "node_modules").mkdir()
    (root / "dist").mkdir()
    (root / "src" / "app.ts").write_text("app.get('/x', h)\n", encoding="utf-8")
    (root / "src" / "routes.ts").write_text("router.get('/a', h)\n", encoding="utf-8")
    (root / "node_modules" / "x.js").write_text("ignored\n", encoding="utf-8")
    (root / "dist" / "bundle.js").write_text("ignored\n", encoding="utf-8")


def test_cache_manifest_creation_and_hit(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo(repo_root)
    repos = [RepoRef(name="api", root=str(repo_root))]

    artifacts = {
        "routes_discovery": {"result": {"version": "v1", "repos": [{"name": "api", "root": str(repo_root)}], "routes": []}},
        "repo_map": {"map": {}},
        "route_index": {"index": {}},
        "route_graph_facts": {"graph_facts": {}},
        "discovery_coverage": {"coverage": {}},
        "routing_pattern_plan": {"plans": {}},
        "routing_pattern_execution": {"execution": {}},
    }
    save_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint=None, artifacts=artifacts, root=tmp_path)

    status, bundle = load_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint=None, root=tmp_path)
    assert status.hit is True
    assert bundle is not None
    assert "routes_discovery" in bundle.artifacts


def test_cache_miss_on_file_content_change(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo(repo_root)
    repos = [RepoRef(name="api", root=str(repo_root))]
    artifacts = {
        "routes_discovery": {"result": {"version": "v1", "repos": [{"name": "api", "root": str(repo_root)}], "routes": []}},
        "repo_map": {"map": {}},
        "route_index": {"index": {}},
        "route_graph_facts": {"graph_facts": {}},
        "discovery_coverage": {"coverage": {}},
        "routing_pattern_plan": {"plans": {}},
    }
    save_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint=None, artifacts=artifacts, root=tmp_path)

    (repo_root / "src" / "routes.ts").write_text("router.get('/b', h)\n", encoding="utf-8")

    status, bundle = load_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint=None, root=tmp_path)
    assert status.hit is False
    assert status.reason == "changed_files"
    assert status.changed_files >= 1
    assert bundle is None


def test_cache_miss_on_new_relevant_file(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo(repo_root)
    repos = [RepoRef(name="api", root=str(repo_root))]
    artifacts = {
        "routes_discovery": {"result": {"version": "v1", "repos": [{"name": "api", "root": str(repo_root)}], "routes": []}},
        "repo_map": {"map": {}},
        "route_index": {"index": {}},
        "route_graph_facts": {"graph_facts": {}},
        "discovery_coverage": {"coverage": {}},
        "routing_pattern_plan": {"plans": {}},
    }
    save_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint=None, artifacts=artifacts, root=tmp_path)

    (repo_root / "src" / "new-routes.ts").write_text("router.post('/c', h)\n", encoding="utf-8")
    status, _ = load_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint=None, root=tmp_path)
    assert status.hit is False
    assert status.reason == "changed_files"


def test_cache_ignores_noise_dirs_in_snapshot(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo(repo_root)
    repos = [RepoRef(name="api", root=str(repo_root))]
    snap = collect_repo_file_snapshot(repos)
    keys = set(snap.keys())
    assert all("node_modules" not in key for key in keys)
    assert all("dist" not in key for key in keys)


def test_cache_version_or_policy_mismatch_invalidates(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _write_repo(repo_root)
    repos = [RepoRef(name="api", root=str(repo_root))]
    artifacts = {
        "routes_discovery": {"result": {"version": "v1", "repos": [{"name": "api", "root": str(repo_root)}], "routes": []}},
        "repo_map": {"map": {}},
        "route_index": {"index": {}},
        "route_graph_facts": {"graph_facts": {}},
        "discovery_coverage": {"coverage": {}},
        "routing_pattern_plan": {"plans": {}},
    }
    save_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint="openai:gpt-4.1-mini", artifacts=artifacts, root=tmp_path)

    status, _ = load_cache_bundle("ws1", repos, llm_policy="always", model_fingerprint="openai:gpt-4.1-mini", root=tmp_path)
    assert status.hit is False
    assert status.reason == "policy_mismatch"

    status, _ = load_cache_bundle("ws1", repos, llm_policy="auto", model_fingerprint="anthropic:claude", root=tmp_path)
    assert status.hit is False
    assert status.reason == "model_mismatch"
