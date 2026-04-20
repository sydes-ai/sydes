"""Routes command tests for discovery outcome handling."""

import json
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.routes as routes_module
from sydes.cli.main import app
from sydes.core.models import EndpointCandidate, RepoRef, RoutesResult

runner = CliRunner()


def test_routes_command_handles_no_endpoints(
    tmp_path: Path, monkeypatch
) -> None:
    """Routes CLI should succeed and report zero routes when none are discovered."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef]) -> RoutesResult:
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

    def _fake_discovery(repos: list[RepoRef]) -> RoutesResult:
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

    def _fake_discovery(repos: list[RepoRef]) -> RoutesResult:
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
