"""Trace command tests for discovered-endpoint target grounding."""

import json
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.core.models import (
    EndpointCandidate,
    FlowExpansionResult,
    RepoRef,
    RoutesResult,
    SinkCandidate,
    TraceStep,
)

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
    assert payload["flows"]
    assert payload["flows"][0]["entry_node"] == payload["nodes"][0]["id"]
    assert payload["tests"]
    assert payload["tests"][0]["route"] == "/checkout"
    assert any(item["kind"] == "ambiguous_target_candidate" for item in payload["unknowns"])


def test_trace_command_renders_flow_steps_sinks_and_graph_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    """Trace terminal output should include ordered flow steps and sinks."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    saved_names: list[str] = []

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
                )
            ],
        )

    def _fake_save_run_artifact(**kwargs):
        saved_names.append(kwargs["artifact_name"])
        return Path(f"/tmp/{kwargs['artifact_name']}.json")

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos: FlowExpansionResult(
            steps=[
                TraceStep(
                    kind="handler",
                    name="checkout_primary",
                    repo="api",
                    file="src/routes.py",
                    symbol="checkout_primary",
                )
            ],
            sinks=[
                SinkCandidate(
                    kind="database",
                    name="orders_db",
                    action="write",
                    repo="api",
                    file="src/repo.py",
                )
            ],
            notes=["mock expansion"],
            confidence=0.8,
        ),
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(trace_module, "save_run_artifact", _fake_save_run_artifact)

    result = runner.invoke(
        app,
        ["trace", "/checkout", "--method", "POST", "--repo", f"api={repo_root}"],
    )

    assert result.exit_code == 0
    assert "Flow:" in result.stdout
    assert "step: checkout primary" in result.stdout
    assert "Sinks:" in result.stdout
    assert "database: write orders_db" in result.stdout
    assert "Suggested Tests:" in result.stdout
    assert "post_checkout_creates_record" in result.stdout
    assert "expects: request succeeds with expected response" in result.stdout
    assert result.stdout.index("Sinks:") < result.stdout.index("Suggested Tests:")
    assert "trace_result" in saved_names
    assert "flow_expansion" in saved_names
    assert "trace_graph" in saved_names


def test_trace_terminal_lightly_normalizes_step_labels(tmp_path: Path, monkeypatch) -> None:
    """Terminal rendering should lightly normalize underscore-heavy step labels."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef]) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/users",
                    handler="create_user_handler",
                    file="src/routes.py",
                    repo="api",
                    confidence=0.9,
                )
            ],
        )

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos: FlowExpansionResult(
            steps=[
                TraceStep(kind="internal_step", name="create_user_handler", repo="api", file="src/routes.py"),
                TraceStep(kind="internal_step", name="create_User_object", repo="api", file="src/routes.py"),
                TraceStep(kind="internal_step", name="db.commit", repo="api", file="src/routes.py"),
            ],
            sinks=[],
        ),
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        ["trace", "/users", "--method", "POST", "--repo", f"api={repo_root}"],
    )

    assert result.exit_code == 0
    assert "step: create user handler" in result.stdout
    assert "step: create User object" in result.stdout
    assert "step: db.commit" in result.stdout


def test_trace_command_graceful_when_flow_expansion_fails_after_match(
    tmp_path: Path, monkeypatch
) -> None:
    """Trace should remain successful when expansion returns fallback-unavailable notes."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef]) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="GET",
                    path="/status",
                    handler="status_handler",
                    file="src/routes.py",
                    repo="api",
                    service="orders",
                    confidence=0.9,
                )
            ],
        )

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos: FlowExpansionResult(
            notes=["Flow expansion unavailable: mock timeout."],
        ),
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    result = runner.invoke(
        app,
        ["trace", "/status", "--method", "GET", "--repo", f"api={repo_root}"],
    )

    assert result.exit_code == 0
    assert "Matched endpoint:" in result.stdout
    assert "GET /status" in result.stdout
    assert "Flow expansion unavailable: mock timeout." in result.stdout
