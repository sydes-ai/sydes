"""Regression tests for CLI --output path handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sydes.cli.main import app
from sydes.core.models import (
    ConfidenceSummary,
    EndpointCandidate,
    Flow,
    FlowExpansionResult,
    FlowStep,
    GraphEdge,
    GraphNode,
    RepoRef,
    RoutesResult,
    TargetSpec,
    TraceResult,
    TraceSummary,
    TestMatrix as SydesTestMatrix,
)
from sydes.llm.client import LLMValidationResult

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_llm_preflight_success(monkeypatch):
    """CLI output-path tests should not depend on live LLM availability."""
    ok = LLMValidationResult(
        ok=True,
        provider="ollama",
        model="llama3.1:latest",
        base_url="http://localhost:11434",
    )
    monkeypatch.setattr("sydes.cli.routes.validate_llm_available", lambda model_spec=None: ok)
    monkeypatch.setattr("sydes.cli.trace.validate_llm_available", lambda model_spec=None: ok)


@pytest.fixture(autouse=True)
def _mock_routes_planner_fast(monkeypatch):
    """Avoid planner runtime/LLM calls in output-path tests."""

    monkeypatch.setattr(
        "sydes.cli.routes.run_routing_pattern_planner",
        lambda **_kwargs: {
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
        },
    )


def _fake_routes_result(repo_name: str, repo_root: Path) -> RoutesResult:
    return RoutesResult(
        repos=[RepoRef(name=repo_name, root=str(repo_root))],
        routes=[],
        candidate_files=1,
        files_examined=1,
        confidence_summary=ConfidenceSummary(average=0.5, minimum=0.5, maximum=0.5),
    )


def _fake_trace_result(repo_name: str, repo_root: Path) -> TraceResult:
    return TraceResult(
        target=TargetSpec(path="/users", method="GET"),
        repos=[RepoRef(name=repo_name, root=str(repo_root))],
        nodes=[
            GraphNode(
                id="node:endpoint",
                type="api_endpoint",
                name="/users",
                repo=repo_name,
                file="main.py",
                symbol="get_users",
                method="GET",
                path="/users",
            )
        ],
        edges=[
            GraphEdge(
                id="edge:1",
                source="node:endpoint",
                target="node:endpoint",
                type="INFERRED_STEP",
            )
        ],
        flows=[
            Flow(
                id="flow:users",
                name="GET /users",
                entry_node="node:endpoint",
                steps=[FlowStep(node_id="node:endpoint", kind="endpoint")],
            )
        ],
        summary=TraceSummary(
            key_flow_id="flow:users",
            confidence=0.8,
            trace_confidence=0.8,
        ),
        test_matrix=SydesTestMatrix(groups=[]),
    )


def test_routes_output_existing_directory_writes_routes_json(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    output_dir = tmp_path / "artifact-dir"
    output_dir.mkdir()

    monkeypatch.setattr(
        "sydes.cli.routes.discover_endpoints",
        lambda repos, **_kwargs: _fake_routes_result("api", repo_root),
    )
    monkeypatch.setattr(
        "sydes.cli.routes.save_run_artifact",
        lambda **_kwargs: tmp_path / "routes_discovery.json",
    )

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    output_file = output_dir / "routes.json"
    assert output_file.exists()
    assert (output_dir / "api_contract.json").exists()
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["version"] == "v1"


def test_routes_output_missing_directory_like_path_creates_dir(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    output_dir = tmp_path / "new-artifact-dir"

    monkeypatch.setattr(
        "sydes.cli.routes.discover_endpoints",
        lambda repos, **_kwargs: _fake_routes_result("api", repo_root),
    )
    monkeypatch.setattr(
        "sydes.cli.routes.save_run_artifact",
        lambda **_kwargs: tmp_path / "routes_discovery.json",
    )

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert (output_dir / "routes.json").exists()
    assert (output_dir / "api_contract.json").exists()


def test_routes_output_explicit_json_file_writes_that_file(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    output_file = tmp_path / "routes.json"

    monkeypatch.setattr(
        "sydes.cli.routes.discover_endpoints",
        lambda repos, **_kwargs: _fake_routes_result("api", repo_root),
    )
    monkeypatch.setattr(
        "sydes.cli.routes.save_run_artifact",
        lambda **_kwargs: tmp_path / "routes_discovery.json",
    )

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_file),
        ],
    )

    assert result.exit_code == 0
    assert output_file.exists()
    assert not (tmp_path / "api_contract.json").exists()


def test_routes_output_parent_file_fails_gracefully(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    parent_file = tmp_path / "not-a-dir"
    parent_file.write_text("x", encoding="utf-8")
    output_file = parent_file / "routes.json"

    monkeypatch.setattr(
        "sydes.cli.routes.discover_endpoints",
        lambda repos, **_kwargs: _fake_routes_result("api", repo_root),
    )
    monkeypatch.setattr(
        "sydes.cli.routes.save_run_artifact",
        lambda **_kwargs: tmp_path / "routes_discovery.json",
    )

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_file),
        ],
    )

    assert result.exit_code != 0
    assert f"Output parent exists but is not a directory: {parent_file}" in result.stdout
    assert "Traceback" not in result.stdout
    assert "IsADirectoryError" not in result.stdout


def test_trace_output_existing_directory_writes_trace_artifacts(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    output_dir = tmp_path / "trace-output"
    output_dir.mkdir()

    fake_result = _fake_trace_result("api", repo_root)
    fake_expansion = FlowExpansionResult(steps=[], sinks=[], notes=[], confidence=0.6)

    monkeypatch.setattr(
        "sydes.cli.trace._build_trace_result",
        lambda **_kwargs: (fake_result, fake_expansion),
    )
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **_kwargs: tmp_path / "artifact.json",
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "GET",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert (output_dir / "trace_result.json").exists()
    assert (output_dir / "trace_graph.json").exists()
    assert (output_dir / "test_matrix.json").exists()
    assert (output_dir / "flow_expansion.json").exists()
    assert (output_dir / "contract_view.json").exists()
    trace_payload = json.loads((output_dir / "trace_result.json").read_text(encoding="utf-8"))
    matrix_payload = json.loads((output_dir / "test_matrix.json").read_text(encoding="utf-8"))
    assert trace_payload["test_matrix"]["groups"] == matrix_payload["groups"]
    assert len(trace_payload["test_matrix"]["groups"]) == len(matrix_payload["groups"])


def test_trace_output_missing_directory_like_path_creates_dir(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    output_dir = tmp_path / "new-trace-output"

    fake_result = _fake_trace_result("api", repo_root)
    fake_expansion = FlowExpansionResult(steps=[], sinks=[], notes=[], confidence=0.6)

    monkeypatch.setattr(
        "sydes.cli.trace._build_trace_result",
        lambda **_kwargs: (fake_result, fake_expansion),
    )
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **_kwargs: tmp_path / "artifact.json",
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "GET",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    assert (output_dir / "trace_result.json").exists()
    assert (output_dir / "trace_graph.json").exists()


def test_trace_output_directory_writes_enriched_api_contract_for_express(
    monkeypatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "worklenz"
    repo_root.mkdir()
    route_file = repo_root / "worklenz-backend" / "src" / "routes" / "apis" / "home-page-api-router.ts"
    route_file.parent.mkdir(parents=True)
    route_file.write_text("homePageApiRouter.post('/personal-task', handler)\n", encoding="utf-8")
    controller_file = repo_root / "worklenz-backend" / "src" / "controllers" / "home-page-controller.ts"
    controller_file.parent.mkdir(parents=True)
    controller_file.write_text("export class HomePageController {}\n", encoding="utf-8")
    output_dir = tmp_path / "trace-output-contract"
    output_dir.mkdir()

    fake_result = _fake_trace_result("worklenz", repo_root)
    fake_result.target.path = "/api/v1/home/personal-task"
    fake_result.target.method = "POST"
    fake_result.repos = [RepoRef(name="worklenz", root=str(repo_root))]
    matched_endpoint = EndpointCandidate(
        method="POST",
        path="/api/v1/home/personal-task",
        handler="HomePageController.createPersonalTask",
        file="worklenz-backend/src/routes/apis/home-page-api-router.ts",
        repo="worklenz",
        confidence=0.9,
    )

    monkeypatch.setattr(
        "sydes.cli.trace._build_trace_result",
        lambda **_kwargs: (fake_result, FlowExpansionResult(steps=[], sinks=[], notes=[], confidence=0.6), matched_endpoint),
    )
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **_kwargs: tmp_path / "artifact.json",
    )
    monkeypatch.setattr(
        "sydes.cli.trace.build_handler_symbol_index_batch",
        lambda repos: {"repos": [{"repo": "worklenz", "files": []}], "summary": {}},
    )
    monkeypatch.setattr(
        "sydes.cli.trace.resolve_handler_reference",
        lambda endpoint, repo_index: {
            "primary_handler": {
                "normalized_handler": "HomePageController.createPersonalTask",
                "symbol": {
                    "file": "worklenz-backend/src/controllers/home-page-controller.ts",
                    "line": 10,
                    "start_line": 10,
                    "end_line": 20,
                    "language": "typescript",
                },
            },
            "prehandlers": [],
            "unresolved_handlers": [],
        },
    )
    monkeypatch.setattr(
        "sydes.cli.trace.slice_resolved_handler_body",
        lambda **_kwargs: {
            "handler": "HomePageController.createPersonalTask",
            "file": "worklenz-backend/src/controllers/home-page-controller.ts",
            "statements": [
                {"index": 1, "text": "const result = await db.query(q, [req.body.name, req.body.color_code, req.user?.id]);"},
                {"index": 2, "text": "const q = `INSERT INTO personal_todo_list (name, color_code, user_id, index) VALUES ($1, $2, $3, 1) RETURNING id, name`;"},
                {"index": 3, "text": "return res.status(200).send(new ServerResponse(true, data));"},
            ],
            "summary": {"statement_count": 3, "signals": ["request_body_read", "possible_db_call", "response_return"]},
        },
    )
    monkeypatch.setattr(
        "sydes.cli.trace.build_layered_trace_expansion",
        lambda **_kwargs: {"layers": [], "summary": {"functions_followed": 0, "steps_added": 0}, "skipped_calls": []},
    )
    monkeypatch.setattr(
        "sydes.cli.trace.run_trace_llm_summarizer",
        lambda **_kwargs: {"skipped": True, "warnings": [], "result": {}},
    )
    monkeypatch.setattr(
        "sydes.cli.trace.build_layered_trace_contract",
        lambda **_kwargs: {
            "target": {"method": "POST", "path": "/api/v1/home/personal-task"},
            "flow": {
                "steps": [
                    {"kind": "request_input", "detail": "req.body.name"},
                    {"kind": "request_input", "detail": "req.body.color_code"},
                    {"kind": "database_write", "detail": "INSERT INTO personal_todo_list"},
                    {"kind": "response", "detail": "return res.status(200).send(new ServerResponse(true, data));"},
                ]
            },
            "layers": [],
            "sinks": [
                {
                    "kind": "database",
                    "name": "INSERT personal_todo_list",
                    "evidence": [{"snippet": "INSERT INTO personal_todo_list ... RETURNING id, name"}],
                }
            ],
            "resolved_handlers": [],
            "budgets": {"max_depth": 2},
            "diagnostics": [],
            "summary": "Handles request input, performs database operations, and returns response.",
            "artifacts": {},
        },
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/api/v1/home/personal-task",
            "--method",
            "POST",
            "--repo",
            f"worklenz={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    contract_payload = json.loads((output_dir / "api_contract.json").read_text(encoding="utf-8"))
    contract_view_payload = json.loads((output_dir / "contract_view.json").read_text(encoding="utf-8"))
    route = contract_payload["routes"][0]
    assert route["path"] == "/api/v1/home/personal-task"
    assert "name" in route["request"]["body"]["properties"]
    assert "color_code" in route["request"]["body"]["properties"]
    assert "200" in route["responses"]
    assert "201" not in route["responses"]
    assert "ServerResponse wrapper" in route["responses"]["200"]["body"]["description"]
    assert any("req.user?.id" in note for note in route["notes"])
    assert any("personal_todo_list" in note for note in route["notes"])
    assert contract_view_payload["route"]["path"] == "/api/v1/home/personal-task"
    assert {field["name"] for field in contract_view_payload["request"]["body_fields"]} >= {"name", "color_code"}


def test_trace_output_explicit_json_file_preserves_single_file_output(monkeypatch, tmp_path: Path) -> None:
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    output_file = tmp_path / "trace.json"

    fake_result = _fake_trace_result("api", repo_root)
    fake_expansion = FlowExpansionResult(steps=[], sinks=[], notes=[], confidence=0.6)

    monkeypatch.setattr(
        "sydes.cli.trace._build_trace_result",
        lambda **_kwargs: (fake_result, fake_expansion),
    )
    monkeypatch.setattr(
        "sydes.cli.trace.save_run_artifact",
        lambda **_kwargs: tmp_path / "artifact.json",
    )

    result = runner.invoke(
        app,
        [
            "trace",
            "/users",
            "--method",
            "GET",
            "--repo",
            f"api={repo_root}",
            "--format",
            "json",
            "--output",
            str(output_file),
        ],
    )

    assert result.exit_code == 0
    assert output_file.exists()
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["version"] == "v1"
