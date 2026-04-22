"""Tests for building coarse graph artifacts from inferred flow expansion."""

from sydes.core.graph import add_cross_repo_api_link, build_graph_from_inferred_flow
from sydes.core.models import (
    CrossRepoCallCandidate,
    EndpointCandidate,
    EvidenceRef,
    FlowExpansionResult,
    SinkCandidate,
    TraceStep,
)


def test_build_graph_from_inferred_flow_maps_endpoint_steps_and_sinks() -> None:
    """Graph builder should map endpoint, steps, and sinks into nodes/edges/flow."""
    endpoint = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="checkout_handler",
        file="src/routes.py",
        repo="api",
        service="orders",
        evidence=[EvidenceRef(file="src/routes.py", symbol="checkout_handler", label="route")],
        confidence=0.7,
    )
    expansion = FlowExpansionResult(
        entry_endpoint_id="flow:checkout",
        steps=[
            TraceStep(
                kind="handler",
                name="checkout_handler",
                repo="api",
                service="orders",
                file="src/routes.py",
                symbol="checkout_handler",
                confidence=0.8,
                status="inferred",
                evidence=[EvidenceRef(file="src/routes.py", symbol="checkout_handler", label="handler")],
            ),
            TraceStep(
                kind="service_call",
                name="create_order",
                repo="api",
                service="orders",
                file="src/service.py",
                symbol="create_order",
                confidence=0.75,
                status="inferred",
            ),
        ],
        sinks=[
            SinkCandidate(
                kind="database",
                name="orders",
                repo="api",
                file="src/repo.py",
                action="write",
                confidence=0.8,
                status="inferred",
            ),
            SinkCandidate(
                kind="external_api",
                name="payments",
                repo="api",
                file="src/clients/payments.py",
                action="read",
                confidence=0.65,
                status="inferred",
            ),
            SinkCandidate(
                kind="queue",
                name="orders-events",
                repo="api",
                file="src/queue.py",
                action="publish",
                confidence=0.7,
                status="inferred",
            ),
            SinkCandidate(
                kind="file_sink",
                name="invoices-bucket",
                repo="api",
                file="src/storage.py",
                action="write",
                confidence=0.6,
                status="inferred",
            ),
        ],
        confidence=0.77,
    )

    nodes, edges, flows = build_graph_from_inferred_flow(endpoint, expansion)

    assert len(nodes) == 7
    assert nodes[0].type == "api_endpoint"
    assert [node.type for node in nodes[1:3]] == ["internal_step", "internal_step"]
    assert [node.type for node in nodes[3:]] == ["database", "external_api", "queue", "file_sink"]

    edge_types = [edge.type for edge in edges]
    assert edge_types[:2] == ["CALLS_INTERNAL", "CALLS_INTERNAL"]
    assert "WRITES_DB" in edge_types
    assert "CALLS_EXTERNAL" in edge_types
    assert "PUBLISHES_QUEUE" in edge_types
    assert "WRITES_FILE" in edge_types

    assert len(flows) == 1
    flow = flows[0]
    assert flow.id == "flow:checkout"
    assert flow.entry_node == nodes[0].id
    assert flow.confidence == 0.77
    assert len(flow.steps) == 7


def test_build_graph_from_inferred_flow_with_no_expansion_keeps_endpoint_only() -> None:
    """When expansion is missing, graph should remain valid with endpoint-only flow."""
    endpoint = EndpointCandidate(
        method="GET",
        path="/status",
        file="app.py",
        repo="api",
    )

    nodes, edges, flows = build_graph_from_inferred_flow(endpoint, None)

    assert len(nodes) == 1
    assert nodes[0].type == "api_endpoint"
    assert edges == []
    assert len(flows) == 1
    assert len(flows[0].steps) == 1


def test_add_cross_repo_api_link_adds_target_endpoint_node_and_calls_api_edge() -> None:
    """Cross-repo link helper should append linked endpoint node and CALLS_API edge."""
    source_endpoint = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="create_checkout",
        file="src/routes.py",
        repo="api",
    )
    expansion = FlowExpansionResult(
        steps=[
            TraceStep(
                kind="handler",
                name="create_checkout",
                repo="api",
                file="src/routes.py",
                symbol="create_checkout",
            )
        ]
    )
    nodes, edges, flows = build_graph_from_inferred_flow(source_endpoint, expansion)
    target_endpoint = EndpointCandidate(
        method="POST",
        path="/charge",
        handler="charge",
        file="src/routes.py",
        repo="payments",
        service="payments",
    )
    call = CrossRepoCallCandidate(
        source_repo="api",
        source_file="src/routes.py",
        source_symbol="create_checkout",
        target_path="/charge",
        target_method="POST",
        evidence=[EvidenceRef(file="src/routes.py", symbol="create_checkout", label="http_client_call")],
        confidence=0.82,
    )

    label = add_cross_repo_api_link(
        nodes=nodes,
        edges=edges,
        call=call,
        target_endpoint=target_endpoint,
        link_type="exact_method_path",
        confidence=0.82,
        evidence=call.evidence,
    )

    assert label == "api -> payments::POST /charge"
    assert any(node.repo == "payments" and node.path == "/charge" and node.type == "api_endpoint" for node in nodes)
    assert any(edge.type == "CALLS_API" for edge in edges)
