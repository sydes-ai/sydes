"""Confidence helpers for trace/test-matrix confidence stabilization."""

from __future__ import annotations

from sydes.core.models import EndpointCandidate, FlowExpansionResult, TestMatrix, TraceResult, TraceStep

PARTIAL_TRACE_CONFIDENCE_CAP = 0.85
MAX_TRACE_CONFIDENCE = 0.95
MAX_TEST_MATRIX_CONFIDENCE = 0.95


def _clamp_confidence(value: float | None) -> float:
    """Clamp confidence into [0, 1] while preserving a deterministic default."""
    if value is None:
        return 0.0
    if value < 0:
        return 0.0
    if value > 1:
        return 1.0
    return value


def _has_strong_step_evidence(step: TraceStep) -> bool:
    """Return true when a step has concrete symbol/label evidence beyond fallback."""
    if step.symbol:
        return True
    for ref in step.evidence:
        if ref.symbol:
            return True
        label = (ref.label or "").strip().lower()
        if label and label != "inferred-from-context":
            return True
    return False


def cap_trace_summary_confidence(
    base_confidence: float | None,
    flow_expansion: FlowExpansionResult | None,
) -> tuple[float, bool, list[str]]:
    """Cap optimistic confidence for partially inferred traces."""
    confidence = _clamp_confidence(base_confidence)
    if flow_expansion is None:
        return confidence, False, []

    reasons: list[str] = []

    weak_inferred_steps = any(
        (step.status or "inferred").lower() == "inferred" and not _has_strong_step_evidence(step)
        for step in flow_expansion.steps
    )
    if weak_inferred_steps:
        reasons.append("inferred steps have weak grounding")

    dropped_suspicious_steps = any("Dropped suspicious abstract step" in note for note in flow_expansion.notes)
    if dropped_suspicious_steps:
        reasons.append("suspicious abstract steps were dropped")

    sink_only_flow = bool(flow_expansion.sinks) and not flow_expansion.steps
    if sink_only_flow:
        reasons.append("flow includes sinks without intermediate steps")

    if reasons and confidence > PARTIAL_TRACE_CONFIDENCE_CAP:
        return PARTIAL_TRACE_CONFIDENCE_CAP, True, reasons
    return confidence, False, reasons


def compute_trace_confidence(
    *,
    selected_endpoint: EndpointCandidate | None,
    flow_expansion: FlowExpansionResult | None,
    nodes_count: int,
    edges_count: int,
) -> float:
    """Compute conservative trace confidence from grounded evidence signals."""
    score = 0.25
    if selected_endpoint is not None:
        score += 0.15
        if selected_endpoint.method and selected_endpoint.path:
            score += 0.12
        if selected_endpoint.handler:
            score += 0.08
        if selected_endpoint.file:
            score += 0.08
        if selected_endpoint.repo:
            score += 0.05
        if selected_endpoint.confidence is not None:
            score += min(0.12, max(0.0, selected_endpoint.confidence) * 0.12)

    if flow_expansion is not None:
        grounded_steps = 0
        inferred_only_steps = 0
        for step in flow_expansion.steps:
            has_grounding = bool(step.file or step.repo or step.symbol or _has_strong_step_evidence(step))
            if has_grounding:
                grounded_steps += 1
            elif (step.status or "inferred").lower() == "inferred":
                inferred_only_steps += 1
        score += min(0.14, grounded_steps * 0.035)
        score -= min(0.12, inferred_only_steps * 0.03)

        sink_count = len(flow_expansion.sinks)
        if sink_count:
            score += min(0.10, sink_count * 0.04)
        if flow_expansion.sinks and flow_expansion.steps:
            score += 0.04

        if any("Dropped suspicious abstract step" in note for note in flow_expansion.notes):
            score -= 0.08
        if flow_expansion.sinks and not flow_expansion.steps:
            score -= 0.12

    cross_repo_edges = max(0, edges_count - max(0, nodes_count - 1))
    if cross_repo_edges:
        score += min(0.08, cross_repo_edges * 0.04)

    score = _clamp_confidence(score)
    if score > MAX_TRACE_CONFIDENCE:
        score = MAX_TRACE_CONFIDENCE
    return round(score, 2)


def compute_test_matrix_confidence(trace_result: TraceResult, test_matrix: TestMatrix | None) -> float | None:
    """Compute conservative confidence for generated test matrix usefulness."""
    if test_matrix is None or not test_matrix.groups:
        return None

    trace_conf = (
        trace_result.summary.trace_confidence
        if trace_result.summary.trace_confidence is not None
        else trace_result.summary.confidence
    ) or 0.0
    score = 0.35 + min(0.35, trace_conf * 0.45)

    tests = [test for group in test_matrix.groups for test in group.tests]
    specific_tests = 0
    generic_tests = 0
    for test in tests:
        name = test.name.lower()
        summary = (test.summary or "").lower()
        if any(token in name or token in summary for token in ("downstream", "cross_service", "database", "queue", "contract", "timeout")):
            specific_tests += 1
        if any(token in name for token in ("returns_entity_or_list", "returns_expected_response")):
            generic_tests += 1
    score += min(0.18, specific_tests * 0.03)
    score -= min(0.14, generic_tests * 0.04)

    node_types = {node.type for node in trace_result.nodes}
    if "external_api" in node_types:
        score += 0.05
    if "database" in node_types:
        score += 0.04
    if "queue" in node_types:
        score += 0.03
    if any(edge.type == "CALLS_API" for edge in trace_result.edges):
        score += 0.04

    # Penalize if matrix appears mostly generic without clear grounded side-effect coverage.
    if specific_tests == 0:
        score -= 0.1

    score = _clamp_confidence(score)
    if score > MAX_TEST_MATRIX_CONFIDENCE:
        score = MAX_TEST_MATRIX_CONFIDENCE
    return round(score, 2)
