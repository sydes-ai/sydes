"""Trace command tests for discovered-endpoint target grounding."""

import json
from pathlib import Path

from typer.testing import CliRunner

import sydes.cli.trace as trace_module
from sydes.cli.main import app
from sydes.core.models import (
    CrossRepoCallCandidate,
    CrossRepoLinkResult,
    EndpointCandidate,
    FlowExpansionResult,
    FlowExpansionContext,
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

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
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

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
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
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
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
    assert "Test Matrix:" in result.stdout
    assert "Happy Path:" in result.stdout
    assert "post_checkout_creates_resource" in result.stdout
    assert "verifies POST /checkout creates a new checkout and returns success" in result.stdout
    assert result.stdout.index("Sinks:") < result.stdout.index("Test Matrix:")
    flow_section = result.stdout.split("Flow:", 1)[1].split("Sinks:", 1)[0]
    assert "sink:" not in flow_section
    assert "trace_result" in saved_names
    assert "flow_expansion" in saved_names
    assert "trace_graph" in saved_names


def test_trace_terminal_lightly_normalizes_step_labels(tmp_path: Path, monkeypatch) -> None:
    """Terminal rendering should lightly normalize underscore-heavy step labels."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
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
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
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


def test_trace_command_renders_cross_repo_links_when_confident_match_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """Trace should add linked endpoint node/edge and render compact cross-repo link summary."""
    api_root = tmp_path / "api"
    payments_root = tmp_path / "payments"
    api_root.mkdir()
    payments_root.mkdir()

    source_endpoint = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="create_checkout",
        file="src/routes.py",
        repo="api",
        service="orders",
        confidence=0.9,
    )
    target_endpoint = EndpointCandidate(
        method="POST",
        path="/charge",
        handler="charge",
        file="src/routes.py",
        repo="payments",
        service="payments",
        confidence=0.85,
    )

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[source_endpoint, target_endpoint],
        )

    call_candidate = CrossRepoCallCandidate(
        source_repo="api",
        source_file="src/routes.py",
        source_symbol="create_checkout",
        target_path="/charge",
        target_method="POST",
        raw_call_text="payments_client.post('/charge')",
        confidence=0.82,
    )

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
            steps=[
                TraceStep(
                    kind="handler",
                    name="create_checkout",
                    repo="api",
                    file="src/routes.py",
                    symbol="create_checkout",
                )
            ],
            confidence=0.8,
        ),
    )
    monkeypatch.setattr(
        trace_module,
        "prepare_flow_expansion_context",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionContext(
            anchor_repo="api",
            anchor_file="src/routes.py",
            files=[],
            notes=[],
        ),
    )
    monkeypatch.setattr(
        trace_module,
        "detect_cross_repo_call_candidates",
        lambda context, source_symbol_hint=None: [call_candidate],
    )
    monkeypatch.setattr(
        trace_module,
        "link_cross_repo_call_candidates",
        lambda calls, endpoints: [
            CrossRepoLinkResult(
                source_endpoint_id="api:src/routes.py:create_checkout",
                matched_target_endpoint_id="payments:src/routes.py:POST:/charge",
                link_type="exact_method_path",
                confidence=0.82,
                notes=[],
            )
        ],
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    terminal_result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"api={api_root}",
            "--repo",
            f"payments={payments_root}",
        ],
    )
    assert terminal_result.exit_code == 0
    assert "Cross-Repo Links:" in terminal_result.stdout
    assert "api -> payments::POST /charge" in terminal_result.stdout

    json_result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"api={api_root}",
            "--repo",
            f"payments={payments_root}",
            "--format",
            "json",
        ],
    )
    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert any(edge["type"] == "CALLS_API" for edge in payload["edges"])
    assert any(node["repo"] == "payments" and node.get("path") == "/charge" for node in payload["nodes"])


def test_trace_command_graceful_when_flow_expansion_fails_after_match(
    tmp_path: Path, monkeypatch
) -> None:
    """Trace should remain successful when expansion returns fallback-unavailable notes."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
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
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
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


def test_trace_confidence_is_capped_for_partial_inferred_flow(tmp_path: Path, monkeypatch) -> None:
    """Partially inferred flows should not surface 1.00 summary confidence."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/users",
                    handler="create_user",
                    file="src/routes.py",
                    repo="api",
                    confidence=0.95,
                )
            ],
        )

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
            steps=[TraceStep(kind="internal_step", name="create User object", status="inferred")],
            sinks=[SinkCandidate(kind="database", name="database", action="write", repo="api")],
            notes=["Dropped suspicious abstract step #2: call payment client."],
            confidence=1.0,
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
        ["trace", "/users", "--method", "POST", "--repo", f"api={repo_root}", "--format", "json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["confidence"] <= 0.85


def test_trace_single_repo_output_has_no_cross_repo_section_when_no_links(
    tmp_path: Path, monkeypatch
) -> None:
    """Single-repo traces should not render cross-repo link section unless links exist."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/users",
                    handler="create_user",
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
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
            steps=[TraceStep(kind="handler", name="create_user", repo="api", file="src/routes.py")],
            sinks=[SinkCandidate(kind="database", name="database", action="write", repo="api", file="src/repo.py")],
            confidence=0.8,
        ),
    )
    monkeypatch.setattr(
        trace_module,
        "prepare_flow_expansion_context",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionContext(
            anchor_repo="api",
            anchor_file="src/routes.py",
            files=[],
            notes=[],
        ),
    )
    monkeypatch.setattr(trace_module, "detect_cross_repo_call_candidates", lambda context, source_symbol_hint=None: [])
    monkeypatch.setattr(trace_module, "link_cross_repo_call_candidates", lambda calls, endpoints: [])
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
    assert "Flow:" in result.stdout
    assert "Sinks:" in result.stdout
    assert "Cross-Repo Links:" not in result.stdout


def test_trace_renders_unmatched_cross_repo_candidate_note(tmp_path: Path, monkeypatch) -> None:
    """Unmatched cross-repo candidates should be surfaced in terminal notes section."""
    service1_root = tmp_path / "service1"
    service2_root = tmp_path / "service2"
    service1_root.mkdir()
    service2_root.mkdir()

    source_endpoint = EndpointCandidate(
        method="GET",
        path="/goodreads/books",
        handler="get_books",
        file="src/routes.py",
        repo="service2",
        confidence=0.9,
    )

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[source_endpoint],
        )

    call_candidate = CrossRepoCallCandidate(
        source_repo="service2",
        source_file="src/client.py",
        source_symbol="fetch_db_books",
        target_method="GET",
        target_path="/db/books",
        normalized_target_method="GET",
        normalized_target_path="/db/books",
        raw_call_text='return client.get().uri("/db/books").retrieve()',
        confidence=0.7,
    )

    monkeypatch.setattr(trace_module, "discover_endpoints", _fake_discovery)
    monkeypatch.setattr(
        trace_module,
        "run_flow_expansion",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
            steps=[TraceStep(kind="handler", name="get_books", repo="service2", file="src/routes.py")],
            confidence=0.8,
        ),
    )
    monkeypatch.setattr(
        trace_module,
        "prepare_flow_expansion_context",
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionContext(
            anchor_repo="service2",
            anchor_file="src/routes.py",
            files=[],
            notes=[],
        ),
    )
    monkeypatch.setattr(
        trace_module,
        "detect_cross_repo_call_candidates",
        lambda context, source_symbol_hint=None: [call_candidate],
    )
    monkeypatch.setattr(
        trace_module,
        "link_cross_repo_call_candidates",
        lambda calls, endpoints: [
            CrossRepoLinkResult(
                source_endpoint_id="service2:src/client.py:fetch_db_books",
                matched_target_endpoint_id=None,
                link_type=None,
                normalized_target_method="GET",
                normalized_target_path="/db/books",
                confidence=0.5,
                notes=[
                    "Cross-repo candidate normalized: method=GET path=/db/books.",
                    "No endpoint candidates matched by normalized method+path or path-only.",
                    "Raw call text: return client.get().uri(\"/db/books\").retrieve()",
                ],
            )
        ],
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
        [
            "trace",
            "/goodreads/books",
            "--method",
            "GET",
            "--repo",
            f"service1={service1_root}",
            "--repo",
            f"service2={service2_root}",
        ],
    )

    assert result.exit_code == 0
    assert "Cross-Repo Links:" in result.stdout
    assert "Unmatched cross-repo candidate: GET /db/books" in result.stdout


def test_trace_verbose_flag_controls_debug_notes_visibility(tmp_path: Path, monkeypatch) -> None:
    """Trace terminal output should hide debug notes unless --verbose is set."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()

    def _fake_discovery(repos: list[RepoRef], *, model_spec: str | None = None) -> RoutesResult:
        return RoutesResult(
            repos=repos,
            routes=[
                EndpointCandidate(
                    method="POST",
                    path="/users",
                    handler="create_user",
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
        lambda matched_endpoint, repos, **_kwargs: FlowExpansionResult(
            notes=[
                "Flow expansion context files selected: 1 (examined=1).",
                "Flow expansion prompt chars: 1234.",
                "LLM discovery unavailable: mock timeout.",
            ]
        ),
    )
    monkeypatch.setattr(trace_module, "compute_workspace_id", lambda repos: "ws-test")
    monkeypatch.setattr(trace_module, "create_run_id", lambda: "run-test")
    monkeypatch.setattr(
        trace_module,
        "save_run_artifact",
        lambda **kwargs: Path(f"/tmp/{kwargs['artifact_name']}.json"),
    )

    default_result = runner.invoke(
        app,
        ["trace", "/users", "--method", "POST", "--repo", f"api={repo_root}"],
    )
    assert default_result.exit_code == 0
    assert "Flow expansion context files selected:" not in default_result.stdout
    assert "Flow expansion prompt chars:" not in default_result.stdout
    assert "LLM discovery unavailable: mock timeout." in default_result.stdout

    verbose_result = runner.invoke(
        app,
        ["trace", "/users", "--method", "POST", "--repo", f"api={repo_root}", "--verbose"],
    )
    assert verbose_result.exit_code == 0
    assert "Flow expansion context files selected:" in verbose_result.stdout
    assert "Flow expansion prompt chars:" in verbose_result.stdout
    assert "LLM discovery unavailable: mock timeout." in verbose_result.stdout
