"""Tests for deterministic integration test suggestion scaffolding."""

from sydes.core.models import Flow, FlowStep, GraphNode, TraceResult, TraceSummary, TargetSpec
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
    assert first.name == "post_users_returns_success"
    assert any(item.kind == "request_path" for item in first.inputs)
    assert any(exp.description == "request succeeds with expected response" for exp in first.expectations)


def test_generate_test_suggestions_adds_sink_semantic_expectations_for_post_and_get() -> None:
    """Generator should derive specific POST/GET expectations from sink semantics."""
    trace = TraceResult(
        target=TargetSpec(path="/checkout", method="POST"),
        nodes=[
            GraphNode(id="n1", type="database", name="database", metadata={"action": "write"}),
            GraphNode(id="n3", type="external_api", name="payments", metadata={"action": "read"}),
            GraphNode(id="n4", type="queue", name="events", metadata={"action": "publish"}),
        ],
        flows=[Flow(id="flow:checkout", name="checkout", entry_node="n0")],
        summary=TraceSummary(key_flow_id="flow:checkout", confidence=0.75),
    )

    suggestions = generate_test_suggestions(trace)

    assert 1 <= len(suggestions) <= 3
    assert suggestions[0].name == "post_checkout_creates_record"
    assert suggestions[0].summary == "verifies POST /checkout persists a new checkout record"
    first_expectations = {item.description for item in suggestions[0].expectations}
    assert "persists a new checkout record" in first_expectations
    assert "outbound dependency interaction occurs" in first_expectations
    assert "event/message emission occurs" in first_expectations

    get_trace = TraceResult(
        target=TargetSpec(path="/orders", method="GET"),
        nodes=[GraphNode(id="n1", type="database", name="database", metadata={"action": "read"})],
        summary=TraceSummary(confidence=0.5),
    )
    get_suggestions = generate_test_suggestions(get_trace)
    assert get_suggestions[0].name == "get_orders_returns_retrieved_data"
    assert any(
        item.description == "response includes retrieved entity or list"
        for item in get_suggestions[0].expectations
    )


def test_generate_test_suggestions_adds_payload_expectation_for_return_flow_step() -> None:
    """Return-like flow steps should drive response payload expectations."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[
            GraphNode(id="endpoint", type="api_endpoint", name="/users"),
            GraphNode(id="n1", type="internal_step", name="create User object"),
            GraphNode(id="n2", type="internal_step", name="return user"),
        ],
        flows=[
            Flow(
                id="flow:users",
                name="users",
                entry_node="endpoint",
                steps=[
                    FlowStep(node_id="endpoint", kind="endpoint"),
                    FlowStep(node_id="n1", kind="step"),
                    FlowStep(node_id="n2", kind="step"),
                ],
            )
        ],
        summary=TraceSummary(key_flow_id="flow:users", confidence=0.8),
    )

    suggestions = generate_test_suggestions(trace)

    payload_suggestion = next(item for item in suggestions if item.name == "post_users_returns_created_entity")
    assert payload_suggestion.summary == "verifies the response returns created user data"
    first_expectations = {item.description for item in suggestions[0].expectations}
    assert "response payload reflects returned domain data" in first_expectations
    assert payload_suggestion.expectations[0].description == "response returns created entity data"


def test_generate_test_suggestions_names_are_stable_and_non_empty() -> None:
    """Suggestion names should be deterministic and always non-empty."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[GraphNode(id="n1", type="database", name="database", metadata={"action": "write"})],
        summary=TraceSummary(confidence=0.7),
    )

    first = generate_test_suggestions(trace)
    second = generate_test_suggestions(trace)

    first_names = [item.name for item in first]
    second_names = [item.name for item in second]

    assert first_names == second_names
    assert first_names
    assert all(name.strip() for name in first_names)


def test_generate_test_suggestions_post_includes_request_body_when_inferred() -> None:
    """Write-route suggestions should carry request_body inputs when flow evidence exposes fields."""
    trace = TraceResult(
        target=TargetSpec(path="/items", method="POST"),
        nodes=[
            GraphNode(id="endpoint", type="api_endpoint", name="/items"),
            GraphNode(
                id="n1",
                type="internal_step",
                name="read JSON request body",
                metadata={"step_kind": "input"},
                evidence=[
                    {
                        "file": "app/routes.py",
                        "symbol": "add_item",
                        "snippet": 'data = request.get_json(); item = {"name": data["name"], "price": data.get("price")}',
                    }
                ],
            ),
        ],
        flows=[
            Flow(
                id="flow:items",
                name="items",
                entry_node="endpoint",
                steps=[
                    FlowStep(node_id="endpoint", kind="endpoint"),
                    FlowStep(node_id="n1", kind="input"),
                ],
            )
        ],
        summary=TraceSummary(key_flow_id="flow:items", confidence=0.8),
    )
    suggestions = generate_test_suggestions(trace)
    body_hint = next(
        input_hint.value_hint
        for suggestion in suggestions
        for input_hint in suggestion.inputs
        if input_hint.kind == "request_body"
    )
    assert body_hint["name"] == "string"
