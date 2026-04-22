"""Confidence helpers for trace-level confidence stabilization."""

from __future__ import annotations

from sydes.core.models import FlowExpansionResult, TraceStep

PARTIAL_TRACE_CONFIDENCE_CAP = 0.85


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
