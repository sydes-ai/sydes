"""Integration test suggestion generation from traced flows.

This module produces structured suggestion objects only.
It does not emit runnable framework-specific test files yet.
"""

from sydes.core.models import (
    Flow,
    GraphNode,
    IntegrationTestSuggestion,
    TestExpectation,
    TestInputHint,
    TraceResult,
)


def _route_token(path: str) -> str:
    """Build a compact route token usable in deterministic test names."""
    normalized = path.strip().strip("/")
    if not normalized:
        return "root"
    chunks = [chunk for chunk in normalized.replace("-", "_").split("/") if chunk]
    cleaned = []
    for chunk in chunks:
        token = "".join(char.lower() if char.isalnum() else "_" for char in chunk).strip("_")
        cleaned.append(token or "segment")
    return "_".join(cleaned)


def _entity_label_from_route(path: str) -> str:
    """Infer a lightweight singular-ish entity label from route path."""
    normalized = path.strip().strip("/")
    if not normalized:
        return "record"
    parts = [part for part in normalized.split("/") if part and not part.startswith("{") and not part.startswith(":")]
    if not parts:
        return "record"
    entity = parts[-1].replace("-", "_")
    if entity.endswith("ies") and len(entity) > 3:
        entity = f"{entity[:-3]}y"
    elif entity.endswith("s") and not entity.endswith("ss") and len(entity) > 3:
        entity = entity[:-1]
    entity = entity.replace("_", " ").strip()
    return entity or "record"


def _flow_step_names(trace_result: TraceResult) -> list[str]:
    """Return display names for nodes participating in the selected flow."""
    if not trace_result.flows:
        return []
    flow: Flow | None = None
    if trace_result.summary.key_flow_id:
        flow = next((item for item in trace_result.flows if item.id == trace_result.summary.key_flow_id), None)
    if flow is None:
        flow = trace_result.flows[0]
    node_by_id: dict[str, GraphNode] = {node.id: node for node in trace_result.nodes}
    names: list[str] = []
    for step in flow.steps:
        node = node_by_id.get(step.node_id)
        if node is None or node.type in {"api_endpoint", "database", "external_api", "queue", "file_sink", "sink"}:
            continue
        if node.name:
            names.append(node.name.lower().strip())
    return names


def _contains_return_step(step_names: list[str]) -> bool:
    """Detect return-like flow operations from inferred step names."""
    return any(name.startswith("return ") or " return " in name for name in step_names)


def _unique_suggestions(items: list[IntegrationTestSuggestion]) -> list[IntegrationTestSuggestion]:
    """Keep deterministic ordering while removing duplicate suggestion names."""
    seen: set[str] = set()
    result: list[IntegrationTestSuggestion] = []
    for item in items:
        key = item.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def generate_test_suggestions(trace_result: TraceResult) -> list[IntegrationTestSuggestion]:
    """Generate deterministic integration-test suggestions from trace output."""
    route = trace_result.target.path
    method = (trace_result.target.method or "ANY").upper()
    flow_id = trace_result.summary.key_flow_id
    route_token = _route_token(route)
    method_token = method.lower()
    entity_label = _entity_label_from_route(route)

    sink_nodes = [node for node in trace_result.nodes if node.type in {"database", "external_api", "queue", "file_sink"}]
    sink_types = {node.type for node in sink_nodes}
    has_db_write = any(node.type == "database" and (node.metadata or {}).get("action") == "write" for node in sink_nodes)
    has_db_read = any(node.type == "database" and (node.metadata or {}).get("action") == "read" for node in sink_nodes)
    has_queue = "queue" in sink_types
    has_external_api = "external_api" in sink_types
    step_names = _flow_step_names(trace_result)
    has_return_step = _contains_return_step(step_names)
    sink_only_evidence = bool(sink_nodes) and not step_names

    core_expectations: list[TestExpectation] = [
        TestExpectation(
            kind="http_response",
            description="request succeeds with expected response",
            target=f"{method} {route}",
        )
    ]

    if method == "POST" and has_db_write:
        primary_name = f"{method_token}_{route_token}_creates_record"
        primary_summary = f"verifies {method} {route} persists a new {entity_label} record"
        core_expectations.append(
            TestExpectation(kind="side_effect", description=f"persists a new {entity_label} record", target="database")
        )
    elif method == "GET" and has_db_read:
        primary_name = f"{method_token}_{route_token}_returns_retrieved_data"
        primary_summary = f"verifies {method} {route} returns retrieved {entity_label} data"
        core_expectations.append(
            TestExpectation(kind="behavior", description="response includes retrieved entity or list", target="database")
        )
    elif has_db_write:
        primary_name = f"{method_token}_{route_token}_writes_to_database"
        primary_summary = "validate primary route behavior from inferred flow and sink evidence"
        core_expectations.append(TestExpectation(kind="side_effect", description="database write occurs", target="database"))
    elif has_db_read:
        primary_name = f"{method_token}_{route_token}_reads_from_database"
        primary_summary = "validate primary route behavior from inferred flow and sink evidence"
        core_expectations.append(TestExpectation(kind="behavior", description="response reflects retrieved data", target="database"))
    else:
        primary_name = f"{method_token}_{route_token}_returns_success"
        primary_summary = "validate primary route behavior from inferred flow and sink evidence"

    if has_return_step:
        core_expectations.append(
            TestExpectation(kind="behavior", description="response payload reflects returned domain data", target="response")
        )
    if has_queue:
        core_expectations.append(
            TestExpectation(kind="side_effect", description="event/message emission occurs", target="queue")
        )
    if has_external_api:
        core_expectations.append(
            TestExpectation(kind="side_effect", description="outbound dependency interaction occurs", target="external_api")
        )

    basic = IntegrationTestSuggestion(
        name=primary_name,
        route=route,
        method=method,
        summary=primary_summary,
        inputs=[
            TestInputHint(kind="request_path", value_hint=route, required=True),
            TestInputHint(kind="http_method", value_hint=method, required=True),
        ],
        expectations=core_expectations,
        derived_from_flow_id=flow_id,
        confidence=trace_result.summary.confidence,
        notes=["expectations inferred from sink evidence only"] if sink_only_evidence else [],
    )

    suggestions: list[IntegrationTestSuggestion] = [basic]
    if has_return_step:
        if method == "POST":
            payload_name = f"{method_token}_{route_token}_returns_created_entity"
            payload_summary = f"verifies the response returns created {entity_label} data"
            payload_expectation = "response returns created entity data"
        else:
            payload_name = f"{method_token}_{route_token}_returns_response_payload"
            payload_summary = "validate response body shape from return-like flow steps"
            payload_expectation = "response payload includes expected created or fetched data"
        suggestions.append(
            IntegrationTestSuggestion(
                name=payload_name,
                route=route,
                method=method,
                summary=payload_summary,
                expectations=[
                    TestExpectation(kind="behavior", description=payload_expectation)
                ],
                derived_from_flow_id=flow_id,
                confidence=trace_result.summary.confidence,
            )
        )
    if (has_db_write or has_queue or has_external_api) and len(suggestions) < 3:
        suffix = "writes_to_database" if has_db_write else "verifies_side_effects"
        suggestions.append(
            IntegrationTestSuggestion(
                name=f"{method_token}_{route_token}_{suffix}",
                route=route,
                method=method,
                summary="validate major side effects inferred from sink semantics",
                expectations=[
                    TestExpectation(kind="side_effect", description="database write occurs", target="database")
                    if has_db_write
                    else TestExpectation(
                        kind="side_effect", description="observable side effects occur in dependencies", target="integration"
                    )
                ],
                derived_from_flow_id=flow_id,
                confidence=trace_result.summary.confidence,
                notes=["derived from V1 sink taxonomy; framework assertions are intentionally generic"]
                if sink_only_evidence
                else [],
            )
        )
        if has_queue:
            suggestions[-1].expectations.append(
                TestExpectation(kind="side_effect", description="event/message emission occurs", target="queue")
            )
        if has_external_api:
            suggestions[-1].expectations.append(
                TestExpectation(kind="side_effect", description="outbound dependency interaction occurs", target="external_api")
            )

    return _unique_suggestions(suggestions)[:3]
