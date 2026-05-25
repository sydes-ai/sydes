"""Routes command tests for discovery outcome handling."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import sydes.cli.routes as routes_module
from sydes.cli.main import app
from sydes.core.models import EndpointCandidate, RepoRef, RoutesResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_llm_preflight_success(monkeypatch):
    """Routes command tests should not depend on live preflight checks."""
    from sydes.llm.client import LLMValidationResult

    ok = LLMValidationResult(ok=True, provider="ollama", model="llama3.1:latest", base_url="http://localhost:11434")
    monkeypatch.setattr("sydes.cli.routes.validate_llm_available", lambda model_spec=None: ok)


def test_routes_command_handles_no_endpoints(
    tmp_path: Path, monkeypatch
) -> None:
    """Routes CLI should succeed and report zero routes when none are discovered."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=6,
            files_examined=3,
            notes=["no matches from stub"],
        )

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}"])

    assert result.exit_code == 0
    assert "Routes discovered: 0" in result.stdout
    assert "No routes discovered yet" in result.stdout
    assert "no matches from stub" in result.stdout


def test_routes_command_handles_one_endpoint(tmp_path: Path, monkeypatch) -> None:
    """Routes CLI should render one discovered endpoint when present."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/checkout",
                    handler="checkout_handler",
                    file="src/routes.py",
                    repo="api",
                    service="backend",
                    confidence=0.8,
                    status="inferred",
                )
            ],
            candidate_files=4,
            files_examined=2,
        )

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}"])

    assert result.exit_code == 0
    assert "Routes discovered: 1" in result.stdout
    assert "Discovered routes by repo/service:" in result.stdout
    assert "api / backend:" in result.stdout
    assert "POST /checkout" in result.stdout
    assert "handler=checkout_handler" in result.stdout
    assert "file=src/routes.py" in result.stdout
    assert "      handler=checkout_handler" in result.stdout
    assert "      file=src/routes.py" in result.stdout
    assert "confidence=0.80" in result.stdout
    assert "status=inferred" in result.stdout


def test_routes_command_handles_ambiguous_endpoints_json(
    tmp_path: Path, monkeypatch
) -> None:
    """Routes CLI JSON output should include multiple endpoint candidates."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="GET",
                    path="/status",
                    handler="status_get",
                    file="src/routes.py",
                    repo="api",
                ),
                EndpointCandidate(
                    method="POST",
                    path="/status",
                    handler="status_post",
                    file="src/routes.py",
                    repo="api",
                ),
            ],
            candidate_files=5,
            files_examined=3,
            notes=["ambiguous candidate set"],
        )

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    result = runner.invoke(
        app,
        ["routes", "--repo", f"api={repo_root}", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["candidate_files"] == 5
    assert payload["files_examined"] == 3
    assert len(payload["routes"]) == 2
    assert payload["notes"][0] == "ambiguous candidate set"


def test_routes_command_saves_repo_map_route_index_graph_coverage_and_plan_artifacts(tmp_path: Path, monkeypatch) -> None:
    """Routes CLI should save repo_map, route_index, route_graph_facts, coverage, plan, and execution artifacts."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    saved_names: list[str] = []

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False, **_kwargs) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=[],
        )

    def _fake_save_run_artifact(**kwargs):
        saved_names.append(kwargs["artifact_name"])
        return Path(f"/tmp/{kwargs['artifact_name']}.json")

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(routes_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(routes_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(routes_module, "save_run_artifact", _fake_save_run_artifact)

    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}"])

    assert result.exit_code == 0
    assert "routes_discovery" in saved_names
    assert "repo_map" in saved_names
    assert "route_index" in saved_names
    assert "route_graph_facts" in saved_names
    assert "discovery_coverage" in saved_names
    assert "routing_pattern_plan" in saved_names
    assert "routing_pattern_execution" in saved_names
    assert "Saved repo map artifact" in result.stdout
    assert "Saved route index artifact" in result.stdout
    assert "Saved route graph facts artifact" in result.stdout
    assert "Saved discovery coverage artifact" in result.stdout
    assert "Saved routing pattern plan artifact" in result.stdout
    assert "Saved routing pattern execution artifact" in result.stdout


def test_routes_command_planner_skips_under_auto_when_coverage_strong(tmp_path: Path, monkeypatch) -> None:
    """Planner should skip in auto mode when coverage is strong."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False, **_kwargs) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=[
                "api: deterministic_routes_found=100, deterministic_frameworks=flask_fastapi",
                "api: deterministic_scan_truncated_files=0",
            ],
        )

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(routes_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(routes_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        routes_module,
        "build_route_index_batch",
        lambda repos, repo_map_batch=None: {
            "version": "v1",
            "repos": [{"repo": "api", "summary": {"files_indexed": 10, "files_with_route_calls": 0, "route_call_count": 0, "mount_call_count": 0, "router_symbol_count": 0}, "files": []}],
        },
    )
    monkeypatch.setattr(
        routes_module,
        "build_route_graph_facts_batch",
        lambda repos, route_index_batch=None: {"version": "v1", "repos": [{"repo": "api", "summary": {"containers": 0, "declarations": 0, "mount_edges": 0, "composed_routes": 0, "unresolved_mounts": 0}}]},
    )
    monkeypatch.setattr(
        routes_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}", "--llm-policy", "auto"])
    assert result.exit_code == 0
    assert "routing_pattern_planner=skipped reason=coverage_strong" in result.stdout


def test_routes_command_planner_runs_under_auto_when_coverage_weak(tmp_path: Path, monkeypatch) -> None:
    """Planner should run in auto mode when coverage is weak."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False, **_kwargs) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=[
                "api: deterministic_routes_found=1, deterministic_frameworks=none",
                "api: deterministic_scan_truncated_files=0",
            ],
        )

    class _PlannerClient:
        def generate(self, request):
            payload = {
                "version": "v1",
                "repo": "api",
                "framework_family": "express",
                "routing_convention": "modular_router_mount_graph",
                "confidence": 0.8,
                "route_container_patterns": [],
                "route_declaration_patterns": [],
                "mount_patterns": [],
                "entrypoint_hints": [],
                "route_dir_hints": [],
                "ignore_hints": [],
                "risks": [],
                "recommended_next_action": "apply_mount_graph_extraction",
            }
            return type("R", (), {"text": json.dumps(payload)})()

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(routes_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(routes_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        routes_module,
        "build_route_index_batch",
        lambda repos, repo_map_batch=None: {
            "version": "v1",
            "repos": [{"repo": "api", "summary": {"files_indexed": 10, "files_with_route_calls": 8, "route_call_count": 25, "mount_call_count": 6, "router_symbol_count": 7}, "files": []}],
        },
    )
    monkeypatch.setattr(
        routes_module,
        "build_route_graph_facts_batch",
        lambda repos, route_index_batch=None: {"version": "v1", "repos": [{"repo": "api", "summary": {"containers": 7, "declarations": 25, "mount_edges": 6, "composed_routes": 1, "unresolved_mounts": 6}}]},
    )
    monkeypatch.setattr(routes_module, "create_default_llm_client", lambda **kwargs: _PlannerClient())
    monkeypatch.setattr(
        routes_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}", "--llm-policy", "auto"])
    assert result.exit_code == 0
    assert "routing_pattern_planner=ran confidence=0.8 convention=modular_router_mount_graph" in result.stdout


def test_routes_command_planner_does_not_run_under_never(tmp_path: Path, monkeypatch) -> None:
    """Planner should not run when llm-policy is never."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None, strict_llm: bool = False, **_kwargs) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[],
            candidate_files=0,
            files_examined=0,
            notes=["api: deterministic_routes_found=0, deterministic_frameworks=none", "api: deterministic_scan_truncated_files=0"],
        )

    monkeypatch.setattr(routes_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(routes_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(routes_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        routes_module,
        "build_route_index_batch",
        lambda repos, repo_map_batch=None: {"version": "v1", "repos": [{"repo": "api", "summary": {}, "files": []}]},
    )
    monkeypatch.setattr(
        routes_module,
        "build_route_graph_facts_batch",
        lambda repos, route_index_batch=None: {"version": "v1", "repos": [{"repo": "api", "summary": {}}]},
    )
    monkeypatch.setattr(
        routes_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(app, ["routes", "--repo", f"api={repo_root}", "--llm-policy", "never"])
    assert result.exit_code == 0
    assert "routing_pattern_planner=skipped reason=policy_never" in result.stdout
