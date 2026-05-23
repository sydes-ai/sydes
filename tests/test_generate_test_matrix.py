"""Tests for deterministic API test matrix generation."""

from pathlib import Path

from sydes.core.models import (
    EvidenceRef,
    Flow,
    FlowStep,
    GraphEdge,
    GraphNode,
    RepoRef,
    TraceResult,
    TraceSummary,
    TargetSpec,
)
from sydes.generate.tests import generate_test_matrix


def _flatten_names(matrix) -> list[str]:
    """Return deterministic flattened suggestion names from matrix groups."""
    names: list[str] = []
    for group in matrix.groups:
        names.extend(item.name for item in group.tests)
    return names


def test_generate_test_matrix_for_post_includes_expected_groups() -> None:
    """POST matrix should include happy/validation/side-effects/consistency rules."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[
            GraphNode(id="e1", type="api_endpoint", name="/users", method="POST", path="/users", repo="api"),
            GraphNode(id="e2", type="api_endpoint", name="/users/{id}", method="GET", path="/users/{id}", repo="api"),
            GraphNode(id="n1", type="database", name="users_db", metadata={"action": "write"}, repo="api"),
        ],
        summary=TraceSummary(confidence=0.8),
    )

    matrix = generate_test_matrix(trace)

    categories = [group.category for group in matrix.groups]
    assert "happy_path" in categories
    assert "data_shape" in categories
    assert "failure_modes" in categories
    assert "validation" in categories
    assert "persistence" in categories
    names = _flatten_names(matrix)
    assert "post_users_creates_resource" in names
    assert "post_users_returns_created_entity_shape" in names
    assert "post_users_rejects_missing_required_field" in names
    assert "post_users_rejects_invalid_payload" in names
    assert "post_users_writes_to_database" in names or "post_users_write_sequence_persists_entity" in names
    assert 1 <= len(names) <= 7


def test_generate_test_matrix_for_get_with_id_path() -> None:
    """GET matrix should include happy path, not-found edge, and id validation."""
    trace = TraceResult(
        target=TargetSpec(path="/users/{id}", method="GET"),
        nodes=[
            GraphNode(
                id="step1",
                type="internal_step",
                name="db.query(User).filter(User.id == user_id).first()",
                metadata={"step_kind": "db_read", "expression": "db.query(User).filter(User.id == user_id).first()"},
                repo="api",
            ),
            GraphNode(id="sink", type="database", name="User", metadata={"action": "read"}, repo="api"),
        ],
        flows=[Flow(id="f1", name="GET /users/{id}", entry_node="step1", steps=[FlowStep(node_id="step1", kind="db_read")])],
        summary=TraceSummary(confidence=0.6),
    )

    matrix = generate_test_matrix(trace)

    categories = [group.category for group in matrix.groups]
    assert "happy_path" in categories
    assert "validation" in categories
    assert "edge_cases" in categories
    names = _flatten_names(matrix)
    assert "get_users_id_returns_entity_or_list" in names
    assert "get_users_id_returns_not_found_for_missing_resource" in names
    assert "get_users_id_rejects_invalid_path_param" in names


def test_generate_test_matrix_for_get_without_id_path_keeps_happy_and_edge_groups() -> None:
    """GET collection routes should avoid not-found fallback and include shape checks."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="GET"),
        summary=TraceSummary(confidence=0.6),
    )

    matrix = generate_test_matrix(trace)

    categories = [group.category for group in matrix.groups]
    assert categories == ["happy_path", "data_shape", "edge_cases"]
    names = _flatten_names(matrix)
    assert "get_users_returns_entity_or_list" in names
    assert "get_users_returns_not_found_for_missing_resource" not in names
    assert "get_users_handles_empty_result_set" in names
    assert "get_users_returns_expected_response_shape" in names
    assert all("invalid_path_param" not in name for name in names)


def test_generate_test_matrix_for_get_with_colon_id_path_includes_not_found() -> None:
    """Colon-style id path routes should include not-found fallback checks."""
    trace = TraceResult(
        target=TargetSpec(path="/books/:id", method="GET"),
        summary=TraceSummary(confidence=0.6),
    )
    names = _flatten_names(generate_test_matrix(trace))
    assert "get_books_id_returns_not_found_for_missing_resource" in names


def test_generate_test_matrix_for_get_with_angle_id_path_includes_not_found() -> None:
    """Angle-bracket id path routes should include not-found fallback checks."""
    trace = TraceResult(
        target=TargetSpec(path="/books/<id>", method="GET"),
        summary=TraceSummary(confidence=0.6),
    )
    names = _flatten_names(generate_test_matrix(trace))
    assert "get_books_id_returns_not_found_for_missing_resource" in names


def test_generate_test_matrix_for_put_and_delete_rules() -> None:
    """PUT/PATCH and DELETE should produce deterministic consistency-focused checks."""
    put_trace = TraceResult(
        target=TargetSpec(path="/users/{id}", method="PUT"),
        summary=TraceSummary(confidence=0.7),
    )
    put_matrix = generate_test_matrix(put_trace)
    put_names = _flatten_names(put_matrix)
    assert "put_users_id_updates_resource" in put_names
    assert "put_users_id_rejects_invalid_payload" in put_names
    assert "put_users_id_update_then_fetch_consistent" in put_names

    delete_trace = TraceResult(
        target=TargetSpec(path="/users/{id}", method="DELETE"),
        summary=TraceSummary(confidence=0.7),
    )
    delete_matrix = generate_test_matrix(delete_trace)
    delete_names = _flatten_names(delete_matrix)
    assert "delete_users_id_deletes_resource" in delete_names
    assert "delete_users_id_deleted_resource_not_returned" in delete_names


def test_generate_test_matrix_external_api_sink_adds_downstream_failure_cases() -> None:
    """External API sink should add downstream failure/data-shape scenarios."""
    trace = TraceResult(
        target=TargetSpec(path="/goodreads/books", method="GET"),
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/goodreads/books", method="GET", path="/goodreads/books", repo="service2"),
            GraphNode(id="sink", type="external_api", name="http://service1/db/books", metadata={"action": "read"}, repo="service2"),
        ],
        summary=TraceSummary(confidence=0.7),
    )

    matrix = generate_test_matrix(trace)
    names = _flatten_names(matrix)
    assert "get_goodreads_books_proxies_downstream_response" in names
    happy_group = next(group for group in matrix.groups if group.category == "happy_path")
    assert "downstream service call" in (happy_group.tests[0].summary or "")
    assert "get_goodreads_books_downstream_unavailable" in names
    assert "get_goodreads_books_downstream_timeout" in names
    assert "get_goodreads_books_downstream_empty_payload_handled" in names
    assert "get_goodreads_books_downstream_malformed_payload_handled" in names
    assert "get_goodreads_books_handles_empty_result_set" not in names


def test_generate_test_matrix_cross_repo_link_adds_contract_case() -> None:
    """Cross-repo CALLS_API links should add contract-compatibility checks."""
    trace = TraceResult(
        target=TargetSpec(path="/goodreads/books", method="GET"),
        nodes=[
            GraphNode(id="src_ep", type="api_endpoint", name="/goodreads/books", method="GET", path="/goodreads/books", repo="service2"),
            GraphNode(id="dst_ep", type="api_endpoint", name="/db/books", method="GET", path="/db/books", repo="service1"),
        ],
        edges=[
            GraphEdge(id="e1", source="src_ep", target="dst_ep", type="CALLS_API"),
        ],
        summary=TraceSummary(confidence=0.7),
    )

    matrix = generate_test_matrix(trace)
    names = _flatten_names(matrix)
    assert "get_goodreads_books_cross_service_contract_compatible" in names


def test_generate_test_matrix_simple_fastapi_get_users_prefers_empty_list_not_not_found() -> None:
    """SimpleFastPyAPI-like GET /users should use empty-list edge case, not not-found fallback."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="GET"),
        nodes=[
            GraphNode(
                id="ep",
                type="api_endpoint",
                name="/users",
                method="GET",
                path="/users",
                repo="api",
            ),
            GraphNode(
                id="step_dep",
                type="internal_step",
                name="Depends(get_db)",
                metadata={"step_kind": "dependency", "expression": "Depends(get_db)"},
                repo="api",
                file="main.py",
                symbol="get_all_users",
            ),
            GraphNode(
                id="step_db",
                type="internal_step",
                name="db.query(User).all()",
                metadata={"step_kind": "db_read", "expression": "db.query(User).all()", "target_entity": "User"},
                repo="api",
                file="main.py",
                symbol="get_all_users",
            ),
            GraphNode(
                id="sink_db",
                type="database",
                name="User",
                metadata={"action": "read", "operation": "db.query(User).all()", "target_entity": "User"},
                repo="api",
            ),
        ],
        flows=[
            Flow(
                id="f1",
                name="GET /users",
                entry_node="ep",
                steps=[
                    FlowStep(node_id="ep", kind="endpoint"),
                    FlowStep(node_id="step_dep", kind="dependency"),
                    FlowStep(node_id="step_db", kind="db_read"),
                    FlowStep(node_id="sink_db", kind="sink:database"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.8),
    )

    matrix = generate_test_matrix(trace)
    names = _flatten_names(matrix)
    categories = [group.category for group in matrix.groups]
    assert "happy_path" in categories
    assert "data_shape" in categories
    assert "edge_cases" in categories
    assert "failure_modes" in categories
    assert "get_users_returns_not_found_for_missing_resource" not in names
    assert "get_users_handles_empty_result_set" in names
    assert "get_users_database_read_failure_handled" in names


def test_generate_test_matrix_simple_fastapi_post_users_uses_write_evidence() -> None:
    """SimpleFastPyAPI-like POST /users should include create/validation/persistence/failure coverage."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/users", method="POST", path="/users", repo="api"),
            GraphNode(
                id="step_input",
                type="internal_step",
                name="input model: UserCreate",
                metadata={"step_kind": "input_model", "expression": "UserCreate"},
                repo="api",
                file="main.py",
                symbol="create_user",
            ),
            GraphNode(
                id="step_add",
                type="internal_step",
                name="db.add(db_user)",
                metadata={"step_kind": "db_write", "expression": "db.add(db_user)"},
                repo="api",
            ),
            GraphNode(
                id="step_commit",
                type="internal_step",
                name="db.commit()",
                metadata={"step_kind": "db_write", "expression": "db.commit()"},
                repo="api",
            ),
            GraphNode(
                id="sink_db",
                type="database",
                name="database",
                metadata={"action": "write", "operation": "db.commit()"},
                repo="api",
            ),
        ],
        flows=[
            Flow(
                id="f1",
                name="POST /users",
                entry_node="ep",
                steps=[
                    FlowStep(node_id="ep", kind="endpoint"),
                    FlowStep(node_id="step_input", kind="input_model"),
                    FlowStep(node_id="step_add", kind="db_write"),
                    FlowStep(node_id="step_commit", kind="db_write"),
                    FlowStep(node_id="sink_db", kind="sink:database"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.8),
    )

    matrix = generate_test_matrix(trace)
    names = _flatten_names(matrix)
    categories = [group.category for group in matrix.groups]
    assert "happy_path" in categories
    assert "validation" in categories
    assert "persistence" in categories
    assert "failure_modes" in categories
    assert "post_users_creates_resource" in names
    assert "post_users_rejects_missing_required_field" in names
    assert "post_users_rejects_invalid_payload" in names
    assert "post_users_database_commit_failure_handled" in names


def test_generate_test_matrix_flask_post_infers_request_body_from_handler_evidence() -> None:
    """Flask POST flow evidence should drive request_body test inputs and validation coverage."""
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/items", method="POST", path="/items", repo="flask"),
            GraphNode(
                id="step_input",
                type="internal_step",
                name="read JSON request body",
                metadata={"step_kind": "input"},
                repo="flask",
                file="app/routes.py",
                symbol="add_item",
                evidence=[
                    EvidenceRef(
                        file="app/routes.py",
                        symbol="add_item",
                        label="deterministic:input:get_json",
                        snippet='data = request.get_json(); item = {"name": data["name"], "price": data.get("price")}',
                    )
                ],
            ),
            GraphNode(id="sink", type="database", name="database", metadata={"action": "write"}, repo="flask"),
        ],
        flows=[
            Flow(
                id="f1",
                name="POST /items",
                entry_node="ep",
                steps=[
                    FlowStep(node_id="ep", kind="endpoint"),
                    FlowStep(node_id="step_input", kind="input"),
                    FlowStep(node_id="sink", kind="sink:database"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.8),
    )
    matrix = generate_test_matrix(trace)
    all_tests = [test for group in matrix.groups for test in group.tests]
    assert all_tests
    request_body_hint = next(
        item.value_hint
        for test in all_tests
        for item in test.inputs
        if item.kind == "request_body"
    )
    assert isinstance(request_body_hint, dict)
    assert request_body_hint["name"] == "string"
    assert request_body_hint["price"] in {"unknown", "string", "number"}
    assert "post_items_rejects_missing_required_field" in _flatten_names(matrix)
    assert "post_items_rejects_invalid_payload" in _flatten_names(matrix)


def test_generate_test_matrix_fastapi_post_infers_request_body_from_pydantic_model(tmp_path: Path) -> None:
    """Pydantic input model hints should populate request_body fields for write-route matrix tests."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    (repo_root / "models.py").write_text(
        "\n".join(
            [
                "from pydantic import BaseModel",
                "",
                "class UserCreate(BaseModel):",
                "    name: str",
                "    email: str",
                "    password: str",
            ]
        ),
        encoding="utf-8",
    )
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        repos=[RepoRef(name="api", root=str(repo_root))],
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/users", method="POST", path="/users", repo="api"),
            GraphNode(
                id="step_input",
                type="internal_step",
                name="input model: UserCreate",
                metadata={"step_kind": "input_model", "expression": "UserCreate"},
                repo="api",
                file="main.py",
                symbol="create_user",
            ),
        ],
        flows=[
            Flow(
                id="f1",
                name="POST /users",
                entry_node="ep",
                steps=[
                    FlowStep(node_id="ep", kind="endpoint"),
                    FlowStep(node_id="step_input", kind="input_model"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.8),
    )
    matrix = generate_test_matrix(trace)
    all_tests = [test for group in matrix.groups for test in group.tests]
    request_body_hint = next(
        item.value_hint
        for test in all_tests
        for item in test.inputs
        if item.kind == "request_body"
    )
    assert request_body_hint == {"name": "string", "email": "string", "password": "string"}


def test_generate_test_matrix_get_route_does_not_include_request_body_input() -> None:
    """GET matrix suggestions should not include request_body inputs."""
    trace = TraceResult(target=TargetSpec(path="/users", method="GET"), summary=TraceSummary(confidence=0.7))
    matrix = generate_test_matrix(trace)
    assert all(
        all(item.kind != "request_body" for item in suggestion.inputs)
        for group in matrix.groups
        for suggestion in group.tests
    )


def test_generate_test_matrix_body_signal_without_fields_uses_generic_request_body_input() -> None:
    """When body consumption is detected but fields are unknown, matrix should still include generic request_body."""
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/items", method="POST", path="/items", repo="api"),
            GraphNode(
                id="step_input",
                type="internal_step",
                name="read JSON request body",
                metadata={"step_kind": "input", "expression": "request.get_json()"},
                repo="api",
                evidence=[
                    EvidenceRef(
                        file="app/routes.py",
                        symbol="add_item",
                        label="deterministic:input:get_json",
                        snippet="data = request.get_json()",
                    )
                ],
            ),
        ],
        flows=[
            Flow(
                id="f1",
                name="POST /items",
                entry_node="ep",
                steps=[
                    FlowStep(node_id="ep", kind="endpoint"),
                    FlowStep(node_id="step_input", kind="input"),
                ],
            )
        ],
        summary=TraceSummary(confidence=0.8),
    )
    matrix = generate_test_matrix(trace)
    request_body_hints = [
        item.value_hint
        for group in matrix.groups
        for suggestion in group.tests
        for item in suggestion.inputs
        if item.kind == "request_body"
    ]
    assert request_body_hints
    assert {"type": "object", "description": "valid JSON payload"} in request_body_hints


def test_generate_test_matrix_db_write_adds_failure_and_idempotency_cases() -> None:
    """Database write sinks should add rollback/failure/idempotency-oriented checks."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[
            GraphNode(id="ep", type="api_endpoint", name="/users", method="POST", path="/users", repo="api"),
            GraphNode(id="db", type="database", name="users_db", metadata={"action": "write"}, repo="api"),
        ],
        summary=TraceSummary(confidence=0.8),
    )

    matrix = generate_test_matrix(trace)
    names = _flatten_names(matrix)
    assert "post_users_database_write_failure_handled" in names
    assert "post_users_write_path_is_idempotent" in names


def test_generate_test_matrix_basic_endpoint_without_sinks_keeps_happy_and_edge() -> None:
    """Baseline GET collection endpoint should include happy/data-shape/edge checks."""
    trace = TraceResult(
        target=TargetSpec(path="/status", method="GET"),
        summary=TraceSummary(confidence=0.5),
    )
    matrix = generate_test_matrix(trace)
    categories = [group.category for group in matrix.groups]
    names = _flatten_names(matrix)
    assert categories == ["happy_path", "data_shape", "edge_cases"]
    assert "get_status_returns_entity_or_list" in names
    assert "get_status_returns_not_found_for_missing_resource" not in names
    assert "get_status_handles_empty_result_set" in names
    assert "get_status_returns_expected_response_shape" in names


def test_generate_test_matrix_simple_get_without_sinks_keeps_generic_happy_path_name() -> None:
    """Simple GET endpoints should keep generic happy-path naming when no downstream sink exists."""
    trace = TraceResult(
        target=TargetSpec(path="/health", method="GET"),
        summary=TraceSummary(confidence=0.5),
    )
    names = _flatten_names(generate_test_matrix(trace))
    assert "get_health_returns_entity_or_list" in names
    assert "get_health_proxies_downstream_response" not in names


def test_generate_test_matrix_fallback_still_emits_grouped_output() -> None:
    """Unknown-method traces should still emit grouped baseline matrix output."""
    trace = TraceResult(
        target=TargetSpec(path="/mystery", method=None),
        summary=TraceSummary(confidence=0.4),
    )
    matrix = generate_test_matrix(trace)
    assert matrix.groups
    categories = [group.category for group in matrix.groups]
    assert "happy_path" in categories
    assert "edge_cases" in categories
