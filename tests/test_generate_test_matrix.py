"""Tests for deterministic API test matrix generation."""

from sydes.core.models import (
    GraphEdge,
    GraphNode,
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
    assert "validation" in categories
    assert "side_effects" in categories
    assert "state_consistency" in categories
    names = _flatten_names(matrix)
    assert "post_users_creates_resource" in names
    assert "post_users_rejects_missing_required_field" in names
    assert "post_users_rejects_invalid_payload" in names
    assert "post_users_writes_to_database" in names
    assert "post_users_create_then_fetch_consistent" in names
    assert 1 <= len(names) <= 7


def test_generate_test_matrix_for_get_with_id_path() -> None:
    """GET matrix should include happy path, not-found edge, and id validation."""
    trace = TraceResult(
        target=TargetSpec(path="/users/{id}", method="GET"),
        summary=TraceSummary(confidence=0.6),
    )

    matrix = generate_test_matrix(trace)

    categories = [group.category for group in matrix.groups]
    assert categories == ["happy_path", "validation", "edge_cases"]
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
