"""Tests for deterministic API test matrix generation."""

from sydes.core.models import (
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
