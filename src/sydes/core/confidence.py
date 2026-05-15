"""Confidence helpers for trace grounding and test-matrix coverage scoring."""

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
    *,
    has_strong_grounding: bool = False,
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
    if weak_inferred_steps and not has_strong_grounding:
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

    # Heuristic: extra graph edges beyond minimal chain often indicate richer grounded links.
    cross_repo_edges = max(0, edges_count - 1)
    if cross_repo_edges:
        score += min(0.08, cross_repo_edges * 0.04)

    score = _clamp_confidence(score)
    if score > MAX_TRACE_CONFIDENCE:
        score = MAX_TRACE_CONFIDENCE
    return round(score, 2)


def _has_path_param(path: str) -> bool:
    """Detect id-like path parameters for risk-surface detection."""
    import re

    return bool(re.search(r"\{[^}]+\}|:[A-Za-z_][A-Za-z0-9_]*|<[^>]+>", path))


def compute_test_matrix_coverage(trace_result: TraceResult, test_matrix: TestMatrix | None) -> float | None:
    """Compute coverage of detected risk surfaces by generated matrix cases."""
    if test_matrix is None or not test_matrix.groups:
        return None

    route = trace_result.target.path
    method = (trace_result.target.method or "ANY").upper()
    has_id_param = _has_path_param(route)
    node_types = {node.type for node in trace_result.nodes}
    has_external_api = "external_api" in node_types
    has_database = "database" in node_types
    has_queue = "queue" in node_types
    has_cross_repo = any(edge.type == "CALLS_API" for edge in trace_result.edges)
    tests = [test for group in test_matrix.groups for test in group.tests]
    names = {test.name.lower() for test in tests}
    summaries = {(test.summary or "").lower() for test in tests}
    text_blob = " ".join(sorted(names | summaries))

    detected_surfaces: set[str] = {"happy_path"}
    if method == "GET" and not has_id_param:
        detected_surfaces.add("collection_shape")
    if has_id_param:
        detected_surfaces.add("resource_lookup")
    if has_external_api:
        detected_surfaces.update({"external_api_failure", "downstream_empty", "data_shape"})
        detected_surfaces.add("proxy_downstream")
    if has_database:
        detected_surfaces.add("database_path")
    if has_queue:
        detected_surfaces.add("queue_publish")
    if has_cross_repo:
        detected_surfaces.add("cross_repo_contract")

    covered_surfaces: set[str] = set()
    if tests:
        covered_surfaces.add("happy_path")
    if "shape" in text_blob or "schema" in text_blob:
        covered_surfaces.add("collection_shape")
        covered_surfaces.add("data_shape")
    if "not_found" in text_blob or "not-found" in text_blob or "missing_resource" in text_blob:
        covered_surfaces.add("resource_lookup")
    if any(token in text_blob for token in ("downstream_unavailable", "downstream_timeout", "external_api")):
        covered_surfaces.add("external_api_failure")
    if "downstream_empty" in text_blob or "empty_payload" in text_blob:
        covered_surfaces.add("downstream_empty")
    if any(token in text_blob for token in ("proxy", "proxies_downstream", "downstream_service")):
        covered_surfaces.add("proxy_downstream")
    if any(token in text_blob for token in ("database_write", "writes_to_database", "database")):
        covered_surfaces.add("database_path")
    if "queue" in text_blob or "publish" in text_blob:
        covered_surfaces.add("queue_publish")
    if "cross_service_contract" in text_blob or "contract_compatible" in text_blob:
        covered_surfaces.add("cross_repo_contract")

    total = max(1, len(detected_surfaces))
    raw_coverage = len(detected_surfaces & covered_surfaces) / total
    coverage = raw_coverage

    # Generic fallback matrices should not appear fully comprehensive.
    rich_surfaces = {"external_api_failure", "database_path", "queue_publish", "cross_repo_contract", "proxy_downstream"}
    has_rich_signal = bool(detected_surfaces & rich_surfaces)
    if not has_rich_signal:
        coverage = min(0.70, max(0.50, raw_coverage * 0.7))

    coverage = _clamp_confidence(coverage)
    if has_cross_repo and has_external_api:
        # Rich proxy/link traces may legitimately hit full surface coverage.
        pass
    elif has_external_api or has_database or has_queue:
        # Sink-aware matrices should remain high but generally below perfect.
        coverage = min(coverage, 0.95)
    return round(coverage, 2)


def compute_test_matrix_confidence(trace_result: TraceResult, test_matrix: TestMatrix | None) -> float | None:
    """Backward-compatible alias for test-matrix coverage scoring."""
    return compute_test_matrix_coverage(trace_result, test_matrix)
