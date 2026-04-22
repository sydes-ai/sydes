"""Tests for deterministic integration test suggestion scaffolding."""

from sydes.core.models import Flow, GraphNode, TraceResult, TraceSummary, TargetSpec
from sydes.generate.tests import generate_test_suggestions


def test_generate_test_suggestions_builds_basic_route_suggestion() -> None:
    """Generator should always return one basic route suggestion."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        summary=TraceSummary(confidence=0.6),
    )

    suggestions = generate_test_suggestions(trace)

    assert len(suggestions) >= 1
    first = suggestions[0]
    assert first.route == "/users"
    assert first.method == "POST"
    assert any(item.kind == "request_path" for item in first.inputs)
    assert any(exp.description == "request succeeds with expected HTTP response" for exp in first.expectations)


def test_generate_test_suggestions_adds_sink_driven_expectations() -> None:
    """Generator should add deterministic expectations derived from sink kinds/actions."""
    trace = TraceResult(
        target=TargetSpec(path="/checkout", method="POST"),
        nodes=[
            GraphNode(id="n1", type="database", name="database", metadata={"action": "write"}),
            GraphNode(id="n2", type="database", name="database", metadata={"action": "read"}),
            GraphNode(id="n3", type="external_api", name="payments", metadata={"action": "read"}),
            GraphNode(id="n4", type="queue", name="events", metadata={"action": "publish"}),
        ],
        flows=[Flow(id="flow:checkout", name="checkout", entry_node="n0")],
        summary=TraceSummary(key_flow_id="flow:checkout", confidence=0.75),
    )

    suggestions = generate_test_suggestions(trace)

    assert 1 <= len(suggestions) <= 3
    first_expectations = {item.description for item in suggestions[0].expectations}
    assert "database write occurs" in first_expectations
    assert "response reflects retrieved data" in first_expectations
    assert "outbound dependency call occurs" in first_expectations
    assert "event/message is published" in first_expectations
