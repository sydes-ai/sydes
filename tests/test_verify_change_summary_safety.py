"""Verdict-safety invariants over `accepted_impacts` that the obligation
ladder in `_compute_summary` cannot see on its own, since it only ever looks
at `affected_flows`/`ci_suite`.

`_compute_summary` is exercised directly against hand-built
`ChangeVerificationResult` objects — precise and fast, and consistent with
how `test_verify_change_final_report.py` already tests the renderer against
the same canonical model, independent of any live CBM/LLM/test run.

Three independent invariants, each provable in isolation:
1. an accepted impact that never became a full verification flow
   (`verification_model_status != "modeled"`) blocks VERIFIED even when
   every obligation on the modeled subset passed.
2. an accepted impact still `status="inferred"` blocks VERIFIED even when
   it *is* modeled and its obligations all passed — LLM confidence is not
   verification proof.
3. an unresolved changed symbol (`unresolved_changed_symbols > 0`) blocks
   VERIFIED even when every modeled obligation passed.
"""

from __future__ import annotations

from sydes.verify.analyzer import _compute_summary
from sydes.verify.models import (
    VERDICT_INCOMPLETE,
    VERDICT_VERIFIED,
    VERIFICATION_PASSED,
    AcceptedImpact,
    AffectedFlow,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    ChangeVerificationResult,
    VerificationObligation,
)


def _passing_obligation(flow_id: str, obligation_id: str = "ob:1") -> VerificationObligation:
    return VerificationObligation(
        id=obligation_id, flow_id=flow_id, kind="route_contract", statement="responds 200",
        origin="api_contract", required=True, status=VERIFICATION_PASSED,
    )


def _modeled_proven_flow(flow_id: str = "flow:GET:/x") -> AffectedFlow:
    return AffectedFlow(
        id=flow_id, entry_label="GET /x", method="GET", path="/x",
        obligations=[_passing_obligation(flow_id)], impact_status="proven",
    )


def _result(**overrides) -> ChangeVerificationResult:
    change = ChangeSet(
        base="main", head="abc123",
        files=[ChangedFile(repo="app", path="x.py")],
        symbols=[ChangedSymbol(id="x", repo="app", file="x.py", name="x")],
    )
    return ChangeVerificationResult(change=change, **overrides)


def test_baseline_reaches_verified_when_every_impact_is_modeled_and_proven() -> None:
    """Sanity check: none of the three new gates should trip when there is
    nothing for them to catch — otherwise the fix would be over-broad."""
    flow = _modeled_proven_flow()
    impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    result = _result(affected_flows=[flow], accepted_impacts=[impact])
    summary = _compute_summary(result)
    assert summary.verdict == VERDICT_VERIFIED


def test_unmodeled_accepted_impact_blocks_verified_even_if_modeled_obligations_pass() -> None:
    """Task item 1: an accepted impact with no verification flow at all
    must keep the verdict out of VERIFIED, regardless of the modeled
    subset's own obligations."""
    flow = _modeled_proven_flow()
    modeled_impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    unmodeled_impact = AcceptedImpact(
        id="impact:app:helper", label="helper", status="proven",
        verification_model_status="unsupported_or_partial",
    )
    result = _result(affected_flows=[flow], accepted_impacts=[modeled_impact, unmodeled_impact])
    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    assert summary.counts.impacts_not_modeled == 1
    assert any("not yet modeled for verification" in reason for reason in summary.risk_reasons)


def test_inferred_modeled_impact_blocks_verified_even_if_its_obligations_pass() -> None:
    """Task item 2: a modeled INFERRED flow whose required obligations all
    pass must still keep the verdict out of VERIFIED — LLM confidence,
    corroborated or not, is never verification proof."""
    flow = AffectedFlow(
        id="flow:GET:/cases", entry_label="GET /cases", method="GET", path="/cases",
        obligations=[_passing_obligation("flow:GET:/cases")], impact_status="inferred",
    )
    inferred_impact = AcceptedImpact(
        id=flow.id, label="GET /cases", status="inferred", route_method="GET", route_path="/cases",
        llm_confidence=0.9, llm_reason="shares a query helper", corroborated=True,
        verification_model_status="modeled",
    )
    result = _result(affected_flows=[flow], accepted_impacts=[inferred_impact])
    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    assert summary.counts.impacts_inferred == 1
    assert any("AI-inferred rather than structurally proven" in reason for reason in summary.risk_reasons)


def test_unresolved_changed_symbol_blocks_verified_even_if_modeled_obligations_pass() -> None:
    """Task item 3: at least one changed symbol with no established impact
    path must block VERIFIED, even when every modeled obligation passed."""
    flow = _modeled_proven_flow()
    impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    result = _result(
        affected_flows=[flow], accepted_impacts=[impact], unresolved_changed_symbols=1,
    )
    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    assert summary.counts.unresolved_changed_symbols == 1
    assert any("unresolved" in reason and "impact path" in reason for reason in summary.risk_reasons)


def test_all_three_gates_can_fire_together_without_masking_each_others_reason() -> None:
    flow = _modeled_proven_flow()
    modeled_impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    unmodeled_inferred = AcceptedImpact(
        id="impact:app:other", label="other", status="inferred", llm_confidence=0.5,
        verification_model_status="unsupported_or_partial",
    )
    result = _result(
        affected_flows=[flow], accepted_impacts=[modeled_impact, unmodeled_inferred],
        unresolved_changed_symbols=2,
    )
    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    reasons_text = " | ".join(summary.risk_reasons)
    assert "not yet modeled for verification" in reasons_text
    assert "AI-inferred rather than structurally proven" in reasons_text
    assert "unresolved" in reasons_text
