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

from sydes.verify.analyzer import _canonicalize_obligations_across_flows, _compute_summary
from sydes.verify.models import (
    RISK_HIGH,
    RISK_MEDIUM,
    VERDICT_INCOMPLETE,
    VERDICT_VERIFIED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    AcceptedImpact,
    AffectedFlow,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    ChangeVerificationResult,
    MappedTest,
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


# ---------------------------------------------------------------------------
# Issue 5: "tests not executed" alone (e.g. --no-run-tests) must not be
# indistinguishable from a genuine "no test exists" coverage gap for risk
# purposes. VERDICT_INCOMPLETE is correct either way (Sydes really could not
# confirm pass/fail) — only the RISK label was conflating the two.
# ---------------------------------------------------------------------------


def _mapped_test(name: str = "test_x") -> MappedTest:
    return MappedTest(id=f"test:{name}", name=name, file="tests/test_x.py")


def test_tests_not_executed_with_full_mapping_stays_medium_not_high() -> None:
    """Every obligation has a real mapped test; the ONLY reason its status
    is UNKNOWN is that the suite was never run (no `ci_suite` at all, the
    --no-run-tests shape). This is a fact about how the run was invoked,
    not evidence of a risky change, and must not alone reach RISK_HIGH even
    though the obligation was introduced by this change."""
    obligation = VerificationObligation(
        id="ob:1", flow_id="flow:GET:/x", kind="route_contract", statement="responds 200",
        origin="api_contract", required=True, introduced_by_change=True,
        mapped_tests=[_mapped_test()], status=VERIFICATION_UNKNOWN,
        reason="The repository test suite was not executed",
    )
    flow = AffectedFlow(
        id="flow:GET:/x", entry_label="GET /x", method="GET", path="/x",
        obligations=[obligation], impact_status="proven",
    )
    impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    result = _result(affected_flows=[flow], accepted_impacts=[impact])
    assert result.ci_suite is None  # the --no-run-tests shape this test targets

    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    assert summary.risk == RISK_MEDIUM


def test_genuine_missing_test_coverage_still_reaches_high_risk() -> None:
    """Contrast case: at least one obligation has NO mapped test at all
    (a real coverage gap, VERIFICATION_UNVERIFIED) -- this must still reach
    RISK_HIGH for an obligation introduced by the change, exactly as
    before. The fix must only soften the "simply never executed" case,
    never mask an actual missing-test gap."""
    covered = VerificationObligation(
        id="ob:1", flow_id="flow:GET:/x", kind="route_contract", statement="responds 200",
        origin="api_contract", required=True, introduced_by_change=True,
        mapped_tests=[_mapped_test()], status=VERIFICATION_UNKNOWN,
        reason="The repository test suite was not executed",
    )
    uncovered = VerificationObligation(
        id="ob:2", flow_id="flow:GET:/x", kind="validation", statement="rejects bad input",
        origin="api_contract", required=True, introduced_by_change=True,
        mapped_tests=[], status=VERIFICATION_UNVERIFIED,
        reason="No existing test asserts this behavior",
    )
    flow = AffectedFlow(
        id="flow:GET:/x", entry_label="GET /x", method="GET", path="/x",
        obligations=[covered, uncovered], impact_status="proven",
    )
    impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    result = _result(affected_flows=[flow], accepted_impacts=[impact])

    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    assert summary.risk == RISK_HIGH


def test_suite_ran_but_produced_no_signal_still_reaches_high_risk() -> None:
    """Contrast case: the suite WAS executed (ci_suite is not None) but
    could not produce a usable signal (e.g. it crashed) -- unlike the
    never-executed case, this is a real anomaly and must keep its existing
    risk weighting, not be softened."""
    from sydes.verify.models import CiSuiteRun

    obligation = VerificationObligation(
        id="ob:1", flow_id="flow:GET:/x", kind="route_contract", statement="responds 200",
        origin="api_contract", required=True, introduced_by_change=True,
        mapped_tests=[_mapped_test()], status=VERIFICATION_UNKNOWN,
        reason="The test suite failed without attributable test results",
    )
    flow = AffectedFlow(
        id="flow:GET:/x", entry_label="GET /x", method="GET", path="/x",
        obligations=[obligation], impact_status="proven",
    )
    impact = AcceptedImpact(
        id=flow.id, label="GET /x", status="proven", route_method="GET", route_path="/x",
        verification_model_status="modeled",
    )
    ci_suite = CiSuiteRun(status=VERIFICATION_UNKNOWN, reason="crashed before producing results")
    result = _result(affected_flows=[flow], accepted_impacts=[impact], ci_suite=ci_suite)

    summary = _compute_summary(result)

    assert summary.verdict == VERDICT_INCOMPLETE
    assert summary.risk == RISK_HIGH


def test_route_aliases_of_one_handler_do_not_multiply_the_same_obligation() -> None:
    """Healthchecks-shaped regression: one real changed behavior
    (`ping_by_slug` rejecting uppercase slugs) reachable through 5 HTTP
    route aliases. Before `_canonicalize_obligations_across_flows`, each
    of the 5 `AffectedFlow`s got its own independently-derived obligation
    for the SAME underlying claim, and `_compute_summary` flattened all 5
    into its counts -- `counts.obligations`/`mapped_tests` would read 5x
    the real, unique fact. This proves the fix: canonicalizing collapses
    them to one unique claim for counting purposes, while every route
    alias individually still carries the evidence (blast-radius
    visibility is preserved, not merged away)."""
    routes = [
        ("flow:ping-by-slug:GET", "GET /ping/{slug}"),
        ("flow:ping-by-slug:POST", "POST /ping/{slug}"),
        ("flow:ping-by-slug:HEAD", "HEAD /ping/{slug}"),
        ("flow:ping-by-slug:GET-fail", "GET /ping/{slug}/fail"),
        ("flow:ping-by-slug:POST-start", "POST /ping/{slug}/start"),
    ]
    flows = []
    for flow_id, label in routes:
        obligation = VerificationObligation(
            id=f"{flow_id}::obligation-0", flow_id=flow_id, kind="route_contract",
            statement="ping_by_slug rejects a slug containing uppercase characters",
            origin="api_contract", required=True, status=VERIFICATION_UNVERIFIED,
        )
        flows.append(AffectedFlow(
            id=flow_id, entry_label=label, handler="ping_by_slug",
            obligations=[obligation], impact_status="proven",
        ))

    # Only ONE flow's own test-mapping pass actually found the relevant
    # regression test -- the realistic shape before canonicalization (each
    # flow's `map_tests_to_obligation` runs independently and may not all
    # find the same evidence the same way).
    relevant_test = _mapped_test()
    flows[0].obligations[0].mapped_tests = [relevant_test]
    flows[0].obligations[0].status = VERIFICATION_PASSED

    _canonicalize_obligations_across_flows(flows)

    # Evidence preserved AND mirrored: every route alias's own obligation
    # copy sees the same test, not just the one flow that originally found it.
    for flow in flows:
        assert len(flow.obligations[0].mapped_tests) == 1
        assert flow.obligations[0].mapped_tests[0].name == relevant_test.name

    impacts = [
        AcceptedImpact(
            id=flow.id, label=flow.entry_label, status="proven",
            verification_model_status="modeled",
        )
        for flow in flows
    ]
    result = _result(affected_flows=flows, accepted_impacts=impacts)
    summary = _compute_summary(result)

    # The unique behavior is counted once, not 5 times.
    assert summary.counts.obligations == 1
    assert summary.counts.mapped_tests == 1

    # Blast radius (how many routes this reaches) is still fully visible --
    # every flow object survives untouched, just not double-counted.
    assert len(result.affected_flows) == 5

    # Risk/verdict must not read WORSE than an equivalent single-route case
    # with identical evidence -- the fix must only stop multiplying the
    # fact, never fabricate additional risk from having multiple aliases.
    single_flow = _modeled_proven_flow()
    single_flow.obligations[0].mapped_tests = [relevant_test]
    single_impact = AcceptedImpact(
        id=single_flow.id, label="GET /x", status="proven",
        route_method="GET", route_path="/x", verification_model_status="modeled",
    )
    single_result = _result(affected_flows=[single_flow], accepted_impacts=[single_impact])
    single_summary = _compute_summary(single_result)
    assert summary.risk == single_summary.risk
    assert summary.verdict == single_summary.verdict
