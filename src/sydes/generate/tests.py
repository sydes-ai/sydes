"""Integration test suggestion generation from traced flows.

This module produces structured suggestion objects only.
It does not emit runnable framework-specific test files yet.
"""

from sydes.core.models import (
    IntegrationTestSuggestion,
    TestExpectation,
    TestInputHint,
    TraceResult,
)


def generate_test_suggestions(trace_result: TraceResult) -> list[IntegrationTestSuggestion]:
    """Generate deterministic integration-test suggestions from trace output."""
    route = trace_result.target.path
    method = trace_result.target.method
    flow_id = trace_result.summary.key_flow_id

    sink_nodes = [node for node in trace_result.nodes if node.type in {"database", "external_api", "queue", "file_sink"}]
    sink_types = {node.type for node in sink_nodes}
    has_db_write = any(node.type == "database" and (node.metadata or {}).get("action") == "write" for node in sink_nodes)
    has_db_read = any(node.type == "database" and (node.metadata or {}).get("action") == "read" for node in sink_nodes)

    expectations: list[TestExpectation] = [
        TestExpectation(
            kind="http_response",
            description="request succeeds with expected HTTP response",
            target=f"{method or 'ANY'} {route}",
        )
    ]
    if has_db_write:
        expectations.append(TestExpectation(kind="side_effect", description="database write occurs", target="database"))
    if "external_api" in sink_types:
        expectations.append(
            TestExpectation(kind="side_effect", description="outbound dependency call occurs", target="external_api")
        )
    if "queue" in sink_types:
        expectations.append(TestExpectation(kind="side_effect", description="event/message is published", target="queue"))
    if "file_sink" in sink_types:
        expectations.append(TestExpectation(kind="side_effect", description="file write occurs", target="file_sink"))
    if has_db_read:
        expectations.append(
            TestExpectation(kind="behavior", description="response reflects retrieved data", target="database")
        )

    basic = IntegrationTestSuggestion(
        name=f"exercise {method or 'route'} {route}",
        route=route,
        method=method,
        summary="basic route-level integration test suggestion",
        inputs=[
            TestInputHint(kind="request_path", value_hint=route, required=True),
            TestInputHint(kind="http_method", value_hint=method or "ANY", required=True),
        ],
        expectations=expectations,
        derived_from_flow_id=flow_id,
        confidence=trace_result.summary.confidence,
    )

    suggestions = [basic]
    if len(trace_result.flows) > 0:
        suggestions.append(
            IntegrationTestSuggestion(
                name=f"happy path for {route}",
                route=route,
                method=method,
                summary="exercise likely happy path flow inferred by Sydes",
                expectations=[
                    TestExpectation(kind="flow", description="key inferred flow steps execute in order where observable")
                ],
                derived_from_flow_id=trace_result.flows[0].id,
                confidence=trace_result.summary.confidence,
            )
        )
    if sink_types and len(suggestions) < 3:
        targets = ", ".join(sorted(sink_types))
        suggestions.append(
            IntegrationTestSuggestion(
                name=f"side effects for {route}",
                route=route,
                method=method,
                summary="validate side effects inferred from sink analysis",
                expectations=[TestExpectation(kind="side_effect", description="inferred side effects occur", target=targets)],
                derived_from_flow_id=flow_id,
                confidence=trace_result.summary.confidence,
                notes=["derived from V1 sink taxonomy; framework assertions are intentionally generic"],
            )
        )

    return suggestions[:3]
