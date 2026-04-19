"""Trace command tests for discovered-endpoint target grounding."""

import json
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.core.models import EndpointCandidate, RepoRef, RoutesResult

runner = CliRunner()


def test_trace_command_renders_match_and_alternatives(tmp_path: Path, monkeypatch) -> None:
    """Trace CLI should show matched endpoint and ambiguous alternatives."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef]) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/checkout",
                    handler="checkout_primary",
                    file="src/routes.py",
                    repo="api",
                    service="orders",
                    confidence=0.9,
                ),
                EndpointCandidate(
                    method="POST",
                    path="/checkout",
                    handler="checkout_alt",
                    file="src/router.py",
                    repo="gateway",
                    service="edge",
                    confidence=0.4,
                ),
            ],
            candidate_files=8,
            files_examined=5,
        )

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path("/tmp/trace_result.json"),
    )

    result = runner.invoke(
        app,
        ["trace", "/checkout", "--method", "POST", "--repo", f"api={repo_root}"],
    )

    assert result.exit_code == 0
    assert "Matched endpoint:" in result.stdout
    assert "repo=api" in result.stdout
    assert "service=orders" in result.stdout
    assert "Alternatives:" in result.stdout
    assert "repo=gateway" in result.stdout
    assert "service=edge" in result.stdout

    json_result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
        ],
    )
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["nodes"][0]["type"] == "api_endpoint"
    assert any(item["kind"] == "ambiguous_target_candidate" for item in payload["unknowns"])
