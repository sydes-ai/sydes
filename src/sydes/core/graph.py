"""Helpers for building a first coarse graph from matched endpoint + expansion output."""

from __future__ import annotations

from sydes.core.models import (
    EndpointCandidate,
    Flow,
    FlowExpansionResult,
    FlowStep,
    GraphEdge,
    GraphNode,
    SINK_ACTION_CONSUME,
    SINK_ACTION_PUBLISH,
    SINK_ACTION_READ,
    SINK_ACTION_WRITE,
    SINK_KIND_DATABASE,
    SINK_KIND_EXTERNAL_API,
    SINK_KIND_FILE_SINK,
    SINK_KIND_QUEUE,
    SinkCandidate,
    TraceStep,
)

EDGE_TYPE_CALLS_INTERNAL = "CALLS_INTERNAL"
EDGE_TYPE_CALLS_EXTERNAL = "CALLS_EXTERNAL"
EDGE_TYPE_READS_DB = "READS_DB"
EDGE_TYPE_WRITES_DB = "WRITES_DB"
EDGE_TYPE_PUBLISHES_QUEUE = "PUBLISHES_QUEUE"
EDGE_TYPE_CONSUMES_QUEUE = "CONSUMES_QUEUE"
EDGE_TYPE_WRITES_FILE = "WRITES_FILE"
EDGE_TYPE_ACCESSES_DB = "ACCESSES_DB"
EDGE_TYPE_INTERACTS_QUEUE = "INTERACTS_QUEUE"
EDGE_TYPE_INTERACTS_FILE = "INTERACTS_FILE"
EDGE_TYPE_INTERACTS_SINK = "INTERACTS_SINK"


def _sanitize_id(value: str) -> str:
    """Convert text to a deterministic node/edge id-safe token."""
    token = value.strip().lower().replace(" ", "_").replace("/", "_")
    token = token.replace("\\", "_").replace(":", "_")
    return token or "unknown"


def _endpoint_node_id(endpoint: EndpointCandidate) -> str:
    """Build a stable endpoint node id."""
    return (
        "endpoint:"
        f"{_sanitize_id(endpoint.repo)}:"
        f"{_sanitize_id(endpoint.file)}:"
        f"{_sanitize_id(endpoint.path or '?')}:"
        f"{_sanitize_id(endpoint.method or '?')}"
    )


def _step_node_id(step: TraceStep, index: int) -> str:
    """Build a stable internal step node id."""
    return (
        "step:"
        f"{index}:"
        f"{_sanitize_id(step.repo or '?')}:"
        f"{_sanitize_id(step.file or '?')}:"
        f"{_sanitize_id(step.symbol or step.name)}"
    )


def _sink_node_id(sink: SinkCandidate, index: int) -> str:
    """Build a stable sink node id."""
    return (
        "sink:"
        f"{index}:"
        f"{_sanitize_id(sink.kind)}:"
        f"{_sanitize_id(sink.repo or '?')}:"
        f"{_sanitize_id(sink.file or sink.name)}"
    )


def _node_type_for_sink(sink: SinkCandidate) -> str:
    """Map sink taxonomy kind to graph node type."""
    if sink.kind in {
        SINK_KIND_DATABASE,
        SINK_KIND_EXTERNAL_API,
        SINK_KIND_QUEUE,
        SINK_KIND_FILE_SINK,
    }:
        return sink.kind
    return "sink"


def _edge_type_for_sink(sink: SinkCandidate) -> str:
    """Map sink kind/action to a coarse V1 edge type."""
    if sink.kind == SINK_KIND_DATABASE:
        if sink.action == SINK_ACTION_READ:
            return EDGE_TYPE_READS_DB
        if sink.action == SINK_ACTION_WRITE:
            return EDGE_TYPE_WRITES_DB
        return EDGE_TYPE_ACCESSES_DB
    if sink.kind == SINK_KIND_EXTERNAL_API:
        return EDGE_TYPE_CALLS_EXTERNAL
    if sink.kind == SINK_KIND_QUEUE:
        if sink.action == SINK_ACTION_PUBLISH:
            return EDGE_TYPE_PUBLISHES_QUEUE
        if sink.action == SINK_ACTION_CONSUME:
            return EDGE_TYPE_CONSUMES_QUEUE
        return EDGE_TYPE_INTERACTS_QUEUE
    if sink.kind == SINK_KIND_FILE_SINK:
        if sink.action == SINK_ACTION_WRITE:
            return EDGE_TYPE_WRITES_FILE
        return EDGE_TYPE_INTERACTS_FILE
    return EDGE_TYPE_INTERACTS_SINK


def build_graph_from_inferred_flow(
    endpoint: EndpointCandidate,
    expansion: FlowExpansionResult | None,
) -> tuple[list[GraphNode], list[GraphEdge], list[Flow]]:
    """Build first-pass graph nodes, edges, and one flow from inferred output."""
    endpoint_node = GraphNode(
        id=_endpoint_node_id(endpoint),
        type="api_endpoint",
        name=endpoint.path or endpoint.file,
        service=endpoint.service,
        repo=endpoint.repo,
        file=endpoint.file,
        symbol=endpoint.handler,
        method=endpoint.method,
        path=endpoint.path,
        evidence=endpoint.evidence,
        confidence=endpoint.confidence,
        status=endpoint.status,
    )

    nodes: list[GraphNode] = [endpoint_node]
    edges: list[GraphEdge] = []
    flow_steps: list[FlowStep] = [FlowStep(node_id=endpoint_node.id, kind="endpoint")]

    steps = expansion.steps if expansion is not None else []
    sinks = expansion.sinks if expansion is not None else []

    previous_node_id = endpoint_node.id
    for index, step in enumerate(steps, start=1):
        node = GraphNode(
            id=_step_node_id(step, index),
            type="internal_step",
            name=step.name,
            service=step.service,
            repo=step.repo,
            file=step.file,
            symbol=step.symbol,
            metadata={"step_kind": step.kind},
            evidence=step.evidence,
            confidence=step.confidence,
            status=step.status,
        )
        nodes.append(node)
        edges.append(
            GraphEdge(
                id=f"edge:step:{index}:{_sanitize_id(previous_node_id)}:{_sanitize_id(node.id)}",
                source=previous_node_id,
                target=node.id,
                type=EDGE_TYPE_CALLS_INTERNAL,
                repo=step.repo,
                service=step.service,
                evidence=step.evidence,
                confidence=step.confidence,
                status=step.status,
            )
        )
        flow_steps.append(FlowStep(node_id=node.id, kind=step.kind or "internal_step"))
        previous_node_id = node.id

    sink_source_id = previous_node_id
    for index, sink in enumerate(sinks, start=1):
        node = GraphNode(
            id=_sink_node_id(sink, index),
            type=_node_type_for_sink(sink),
            name=sink.name,
            service=sink.service,
            repo=sink.repo,
            file=sink.file,
            symbol=sink.symbol,
            metadata={"sink_kind": sink.kind, "action": sink.action},
            evidence=sink.evidence,
            confidence=sink.confidence,
            status=sink.status,
        )
        nodes.append(node)
        edges.append(
            GraphEdge(
                id=f"edge:sink:{index}:{_sanitize_id(sink_source_id)}:{_sanitize_id(node.id)}",
                source=sink_source_id,
                target=node.id,
                type=_edge_type_for_sink(sink),
                repo=sink.repo,
                service=sink.service,
                evidence=sink.evidence,
                confidence=sink.confidence,
                status=sink.status,
            )
        )
        flow_steps.append(FlowStep(node_id=node.id, kind=f"sink:{sink.kind}"))

    flow_confidence = expansion.confidence if expansion is not None else endpoint.confidence
    flow_summary = None
    if expansion is not None and expansion.notes:
        flow_summary = expansion.notes[0]

    flow_id = expansion.entry_endpoint_id or f"flow:{endpoint_node.id}" if expansion else f"flow:{endpoint_node.id}"
    flow = Flow(
        id=flow_id,
        name=f"{endpoint.method or 'ANY'} {endpoint.path or endpoint.file}",
        entry_node=endpoint_node.id,
        steps=flow_steps,
        summary=flow_summary,
        confidence=flow_confidence,
    )
    return nodes, edges, [flow]
