"""Helpers for building a first coarse graph from matched endpoint + expansion output."""

from __future__ import annotations

from urllib.parse import urlparse
import re

from sydes.core.models import (
    CrossRepoCallCandidate,
    EndpointCandidate,
    EvidenceRef,
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
EDGE_TYPE_CALLS_API = "CALLS_API"
OUTBOUND_EVIDENCE_PREFIXES = (
    "chain_extraction:",
    "multiline_chain:",
    "partial_extraction:",
    "multiline_chain_partial:",
    "http_client_call",
    "outbound_client_call",
)
OUTBOUND_RAW_CALL_RE = re.compile(
    r"\b(?:client|webclient|requests|httpx|axios)\b|fetch\s*\(|\.retrieve\s*\(|\.exchange\s*\(|\.uri\s*\(",
    re.IGNORECASE,
)
DETERMINISTIC_EVIDENCE_PREFIXES = (
    "deterministic:db_read:",
    "deterministic:db_write:",
    "deterministic:external_call:",
    "deterministic:dependency:",
)
DB_QUERY_ENTITY_RE = re.compile(r"db\.query\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", re.IGNORECASE)
RAW_HTTP_METHOD_RE = re.compile(r"\.(get|post|put|delete|patch)\s*\(", re.IGNORECASE)
RAW_URI_PATH_RE = re.compile(r"""\.uri\s*\(\s*["']([^"']+)["']\s*\)""", re.IGNORECASE)
RAW_URL_RE = re.compile(r"""["'](https?://[^"']+)["']""", re.IGNORECASE)


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


def _extract_expression_from_evidence(evidence: list[EvidenceRef]) -> str | None:
    """Extract deterministic one-line expression from evidence labels when available."""
    for ref in evidence:
        label = ref.label or ""
        for prefix in DETERMINISTIC_EVIDENCE_PREFIXES:
            if label.startswith(prefix):
                expression = label[len(prefix) :].strip()
                if expression:
                    return expression
    return None


def _guess_expression_from_step(step: TraceStep) -> str | None:
    """Best-effort expression extraction for step metadata."""
    expression = _extract_expression_from_evidence(step.evidence)
    if expression:
        return expression
    name = step.name.strip()
    if "." in name or "(" in name or ")" in name:
        return name
    if name.startswith("Depends("):
        return name
    return None


def _extract_target_entity_from_expression(expression: str | None) -> str | None:
    """Extract coarse target entity from a concrete DB expression."""
    if not expression:
        return None
    match = DB_QUERY_ENTITY_RE.search(expression)
    if match:
        return match.group(1)
    return None


def _guess_sink_operation(sink: SinkCandidate) -> str | None:
    """Best-effort operation extraction for sink metadata."""
    expression = _extract_expression_from_evidence(sink.evidence)
    if expression:
        return expression
    name = (sink.name or "").strip()
    if "." in name or "(" in name or ")" in name:
        return name
    return None


def _compact_snippet(text: str | None, *, max_chars: int = 220) -> str | None:
    """Normalize a raw call snippet into one readable, bounded line."""
    if not isinstance(text, str):
        return None
    compact = " ".join(text.strip().split())
    if not compact:
        return None
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _extract_http_details_from_call(call: CrossRepoCallCandidate) -> tuple[str | None, str | None, str | None]:
    """Extract HTTP method/path/url hints from candidate fields or raw call text."""
    method = call.normalized_target_method or call.target_method
    path = call.normalized_target_path or call.target_path
    target_url = None
    raw = call.raw_call_text or ""
    if method is None:
        method_match = RAW_HTTP_METHOD_RE.search(raw)
        if method_match:
            method = method_match.group(1).upper()
    if path is None:
        path_match = RAW_URI_PATH_RE.search(raw)
        if path_match:
            path = path_match.group(1)
    url_match = RAW_URL_RE.search(raw)
    if url_match:
        target_url = url_match.group(1)
        if path is None:
            parsed = urlparse(target_url)
            path = parsed.path or None
    if isinstance(path, str) and path and not path.startswith("/"):
        path = f"/{path}"
    return method, path, target_url


def _attach_call_evidence(
    existing: list[EvidenceRef],
    *,
    call: CrossRepoCallCandidate,
    label: str,
) -> list[EvidenceRef]:
    """Append caller-side snippet evidence when available, deduping by tuple key."""
    merged = list(existing)
    seen = {(item.file, item.symbol, item.label, item.snippet) for item in merged}
    snippet = _compact_snippet(call.raw_call_text)
    if snippet:
        ref = EvidenceRef(
            file=call.source_file or "?",
            symbol=call.source_symbol,
            label=label,
            snippet=snippet,
        )
        key = (ref.file, ref.symbol, ref.label, ref.snippet)
        if key not in seen:
            merged.append(ref)
            seen.add(key)
    for ref in call.evidence:
        key = (ref.file, ref.symbol, ref.label, ref.snippet)
        if key in seen:
            continue
        merged.append(ref)
        seen.add(key)
    return merged


def _find_source_node_id_for_cross_repo_call(
    nodes: list[GraphNode],
    call: CrossRepoCallCandidate,
) -> str | None:
    """Find best source graph node for a cross-repo API call."""
    if call.source_repo:
        if call.source_file and call.source_symbol:
            for node in nodes:
                if node.type == "internal_step" and node.repo == call.source_repo and node.file == call.source_file and node.symbol == call.source_symbol:
                    return node.id
        if call.source_file:
            for node in nodes:
                if node.type == "internal_step" and node.repo == call.source_repo and node.file == call.source_file:
                    return node.id
        for node in nodes:
            if node.type == "api_endpoint" and node.repo == call.source_repo and (not call.source_file or node.file == call.source_file):
                return node.id
        for node in nodes:
            if node.type == "api_endpoint" and node.repo == call.source_repo:
                return node.id
    for node in nodes:
        if node.type == "api_endpoint":
            return node.id
    return None


def _call_has_outbound_evidence(
    call: CrossRepoCallCandidate,
    evidence: list[EvidenceRef] | None,
) -> bool:
    """Return True when call evidence indicates outbound client behavior."""
    labels = [
        item.label or ""
        for item in [*(call.evidence or []), *(evidence or [])]
    ]
    if any("route_declaration" in label for label in labels):
        return False
    if any(label.startswith(OUTBOUND_EVIDENCE_PREFIXES) for label in labels):
        return True
    raw_call_text = call.raw_call_text or ""
    return bool(OUTBOUND_RAW_CALL_RE.search(raw_call_text))


def add_cross_repo_api_link(
    *,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    call: CrossRepoCallCandidate,
    target_endpoint: EndpointCandidate,
    link_type: str | None = None,
    confidence: float | None = None,
    evidence: list[EvidenceRef] | None = None,
) -> str | None:
    """Add a shallow cross-repo API endpoint node+edge into an existing trace graph."""
    source_node_id = _find_source_node_id_for_cross_repo_call(nodes, call)
    if source_node_id is None:
        return None
    source_repo = call.source_repo or ""
    target_repo = target_endpoint.repo or ""
    if source_repo and target_repo and source_repo == target_repo:
        if not _call_has_outbound_evidence(call, evidence):
            return None

    target_node_id = _endpoint_node_id(target_endpoint)
    existing_target = next((node for node in nodes if node.id == target_node_id), None)
    if existing_target is None:
        target_node = GraphNode(
            id=target_node_id,
            type="api_endpoint",
            name=target_endpoint.path or target_endpoint.file,
            service=target_endpoint.service,
            repo=target_endpoint.repo,
            file=target_endpoint.file,
            symbol=target_endpoint.handler,
            method=target_endpoint.method,
            path=target_endpoint.path,
            metadata={"cross_repo_linked": True, "link_type": link_type},
            evidence=target_endpoint.evidence,
            confidence=target_endpoint.confidence,
            status=target_endpoint.status,
        )
        nodes.append(target_node)

    edge_id = f"edge:cross_repo:{_sanitize_id(source_node_id)}:{_sanitize_id(target_node_id)}"
    if any(edge.id == edge_id for edge in edges):
        return None
    call_edge_evidence = _attach_call_evidence(
        list(evidence or []),
        call=call,
        label="webclient_call",
    )

    edges.append(
        GraphEdge(
            id=edge_id,
            source=source_node_id,
            target=target_node_id,
            type=EDGE_TYPE_CALLS_API,
            direction="outbound",
            service=target_endpoint.service,
            repo=call.source_repo,
            evidence=call_edge_evidence,
            confidence=confidence,
            status="inferred",
        )
    )

    source_repo = call.source_repo
    target_repo = target_endpoint.repo
    target_method = target_endpoint.method or "?"
    target_path = target_endpoint.path or "?"
    return f"{source_repo} -> {target_repo}::{target_method} {target_path}"


def enrich_external_api_graph_evidence(
    *,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    calls: list[CrossRepoCallCandidate],
) -> None:
    """Attach structured caller-side HTTP evidence to external API nodes/edges/steps."""
    node_by_id = {node.id: node for node in nodes}
    for call in calls:
        source_node_id = _find_source_node_id_for_cross_repo_call(nodes, call)
        method, target_path, target_url = _extract_http_details_from_call(call)
        operation = None
        if method and target_path:
            operation = f"WebClient {method} {target_path}"
        elif method:
            operation = f"WebClient {method}"
        elif target_path:
            operation = f"WebClient call {target_path}"

        source_node = node_by_id.get(source_node_id) if source_node_id else None
        if source_node is not None and source_node.type == "internal_step":
            metadata = dict(source_node.metadata or {})
            if metadata.get("step_kind") in {None, "", "unknown"}:
                metadata["step_kind"] = "external_api_call"
            if operation and not metadata.get("operation"):
                metadata["operation"] = operation
            if method and not metadata.get("http_method"):
                metadata["http_method"] = method
            if target_path and not metadata.get("target_path"):
                metadata["target_path"] = target_path
            if target_url and not metadata.get("target_url"):
                metadata["target_url"] = target_url
            source_node.metadata = metadata
            source_node.evidence = _attach_call_evidence(
                source_node.evidence,
                call=call,
                label="webclient_call",
            )

        related_sink_ids: list[str] = []
        if source_node_id:
            for edge in edges:
                if edge.source == source_node_id and edge.type == EDGE_TYPE_CALLS_EXTERNAL:
                    related_sink_ids.append(edge.target)
                    edge.evidence = _attach_call_evidence(
                        edge.evidence,
                        call=call,
                        label="webclient_call",
                    )

        for sink_id in related_sink_ids:
            sink_node = node_by_id.get(sink_id)
            if sink_node is None or sink_node.type != "external_api":
                continue
            metadata = dict(sink_node.metadata or {})
            metadata["sink_kind"] = metadata.get("sink_kind") or "external_api"
            metadata["action"] = metadata.get("action") or "read"
            if method and not metadata.get("http_method"):
                metadata["http_method"] = method
            if target_path and not metadata.get("target_path"):
                metadata["target_path"] = target_path
            if target_url and not metadata.get("target_url"):
                metadata["target_url"] = target_url
            if operation and not metadata.get("operation"):
                metadata["operation"] = operation
            sink_node.metadata = metadata
            if target_path:
                sink_node.name = target_path
            sink_node.evidence = _attach_call_evidence(
                sink_node.evidence,
                call=call,
                label="webclient_call",
            )


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
        step_expression = _guess_expression_from_step(step)
        step_metadata: dict[str, str] = {"step_kind": step.kind}
        if step_expression:
            step_metadata["expression"] = step_expression
        target_entity = _extract_target_entity_from_expression(step_expression)
        if target_entity:
            step_metadata["target_entity"] = target_entity
        node = GraphNode(
            id=_step_node_id(step, index),
            type="internal_step",
            name=step.name,
            service=step.service,
            repo=step.repo,
            file=step.file,
            symbol=step.symbol,
            metadata=step_metadata,
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
        sink_operation = _guess_sink_operation(sink)
        sink_metadata: dict[str, str] = {"sink_kind": sink.kind, "action": sink.action or ""}
        if sink_operation:
            sink_metadata["operation"] = sink_operation
        sink_target = _extract_target_entity_from_expression(sink_operation)
        if sink_target:
            sink_metadata["target_entity"] = sink_target
        node = GraphNode(
            id=_sink_node_id(sink, index),
            type=_node_type_for_sink(sink),
            name=sink.name,
            service=sink.service,
            repo=sink.repo,
            file=sink.file,
            symbol=sink.symbol,
            metadata=sink_metadata,
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
