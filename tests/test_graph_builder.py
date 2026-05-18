"""Tests for building coarse graph artifacts from inferred flow expansion."""

from sydes.core.graph import (
    add_cross_repo_api_link,
    build_graph_from_inferred_flow,
    enrich_external_api_graph_evidence,
)
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
        raw_call_text="payments_client.post('/charge')",
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
    calls_api_edge = next(edge for edge in edges if edge.type == "CALLS_API")
    assert any(ref.label == "webclient_call" and ref.snippet for ref in calls_api_edge.evidence)


def test_add_cross_repo_api_link_skips_same_repo_route_declaration_evidence() -> None:
    """Same-repo route declaration evidence should never create CALLS_API edges."""
    source_endpoint = EndpointCandidate(
        method="POST",
        path="/users",
        handler="create_user",
        file="src/main.py",
        repo="api",
    )
    nodes, edges, _flows = build_graph_from_inferred_flow(source_endpoint, FlowExpansionResult())
    target_endpoint = EndpointCandidate(
        method="GET",
        path="/users",
        handler="list_users",
        file="src/main.py",
        repo="api",
    )
    call = CrossRepoCallCandidate(
        source_repo="api",
        source_file="src/main.py",
        source_symbol="create_user",
        target_path="/users",
        target_method="GET",
        raw_call_text="@app.get('/users/')",
        evidence=[EvidenceRef(file="src/main.py", symbol="create_user", label="route_declaration:GET:/users")],
        confidence=0.4,
    )

    label = add_cross_repo_api_link(
        nodes=nodes,
        edges=edges,
        call=call,
        target_endpoint=target_endpoint,
        link_type="path_only",
        confidence=0.4,
        evidence=call.evidence,
    )

    assert label is None
    assert all(edge.type != "CALLS_API" for edge in edges)


def test_enrich_external_api_graph_evidence_attaches_http_details_and_snippets() -> None:
    """External API sink graph nodes/edges should carry caller-side snippet + method/path metadata."""
    endpoint = EndpointCandidate(
        method="GET",
        path="/goodreads/books",
        handler="getBooks",
        file="src/main/java/com/jmhreif/service2/Service2Application.java",
        repo="service2",
    )
    expansion = FlowExpansionResult(
        steps=[
            TraceStep(
                kind="unknown",
                name="invoke downstream service",
                repo="service2",
                file="src/main/java/com/jmhreif/service2/Service2Application.java",
                symbol="getBooks",
            )
        ],
        sinks=[
            SinkCandidate(
                kind="external_api",
                name="http call",
                repo="service2",
                file="src/main/java/com/jmhreif/service2/Service2Application.java",
                symbol="getBooks",
                action="read",
            )
        ],
    )
    nodes, edges, _flows = build_graph_from_inferred_flow(endpoint, expansion)
    call = CrossRepoCallCandidate(
        source_repo="service2",
        source_file="src/main/java/com/jmhreif/service2/Service2Application.java",
        source_symbol="getBooks",
        target_method="GET",
        target_path="/db/books",
        raw_call_text='return client.get().uri("/db/books").retrieve().bodyToFlux(Book.class);',
    )

    enrich_external_api_graph_evidence(nodes=nodes, edges=edges, calls=[call])

    step_node = next(node for node in nodes if node.type == "internal_step")
    assert step_node.metadata.get("step_kind") == "external_api_call"
    assert step_node.metadata.get("http_method") == "GET"
    assert step_node.metadata.get("target_path") == "/db/books"
    assert any(ref.label == "webclient_call" and ref.snippet for ref in step_node.evidence)

    sink_node = next(node for node in nodes if node.type == "external_api")
    assert sink_node.metadata.get("sink_kind") == "external_api"
    assert sink_node.metadata.get("action") == "read"
    assert sink_node.metadata.get("http_method") == "GET"
    assert sink_node.metadata.get("target_path") == "/db/books"
    assert sink_node.metadata.get("operation") == "WebClient GET /db/books"
    assert any(ref.label == "webclient_call" and ref.snippet for ref in sink_node.evidence)

    calls_external_edge = next(edge for edge in edges if edge.type == "CALLS_EXTERNAL")
    assert any(ref.label == "webclient_call" and ref.snippet for ref in calls_external_edge.evidence)
