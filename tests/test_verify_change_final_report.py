"""The default human-readable report: concise, ~20-second-readable, built
entirely from the canonical `ChangeVerificationResult` — no internal field
name (`accepted_impacts`, `verification_model_status`, `mapped_tests`, ...)
ever appears in it. `--verbose` keeps the full detailed report unchanged.

`render_verify_change_terminal` is exercised directly against hand-built
`ChangeVerificationResult` objects, independent of any live CBM/LLM/test run.
"""

from __future__ import annotations

from sydes.core.models import EvidenceRef
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.models import (
    RISK_HIGH,
    RISK_MEDIUM,
    TIER_ASSERTED_EFFECT,
    VERDICT_INCOMPLETE,
    VERDICT_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    AcceptedImpact,
    AffectedFlow,
    ChangedSymbol,
    ChangeSet,
    ChangeSummary,
    ChangeVerificationResult,
    CiSuiteRun,
    MappedTest,
    RuntimeDependency,
    VerificationCounts,
    VerificationObligation,
)


def _result(**overrides) -> ChangeVerificationResult:
    change = overrides.pop("change", None) or ChangeSet(base="main", head="abc123", files=[], symbols=[])
    result = ChangeVerificationResult(change=change, **overrides)
    if "summary" not in overrides:
        flows = result.affected_flows
        required = [o for f in flows for o in f.obligations if o.required]
        result.summary = ChangeSummary(
            risk=RISK_MEDIUM, verdict=VERDICT_INCOMPLETE,
            counts=VerificationCounts(
                affected_flows=len(flows),
                obligations=len(required),
                obligations_passed=sum(1 for o in required if o.status == VERIFICATION_PASSED),
                impacts_not_modeled=sum(
                    1 for i in result.accepted_impacts if i.verification_model_status != "modeled"
                ),
                unresolved_changed_symbols=result.unresolved_changed_symbols,
            ),
        )
    return result


def _obligation(statement: str, *, flow_id: str = "flow:1", status: str = VERIFICATION_UNVERIFIED,
                 supporting: bool = False, supporting_count: int = 1, kind: str = "route_contract",
                 reason: str | None = None) -> VerificationObligation:
    obligation = VerificationObligation(
        id=f"ob:{statement[:10]}", flow_id=flow_id, kind=kind, statement=statement,
        origin="api_contract", required=True, status=status, reason=reason,
    )
    if status == VERIFICATION_PASSED:
        obligation.mapped_tests = [
            MappedTest(id="t", name="test_it", evidence_tier=TIER_ASSERTED_EFFECT, match_rule="asserts it directly")
        ]
    elif supporting:
        obligation.supporting_tests = [
            MappedTest(id=f"t{i}", name=f"test_it_{i}", evidence_tier=TIER_ASSERTED_EFFECT,
                       match_rule="exercises the flow")
            for i in range(supporting_count)
        ]
    return obligation


# --- 1. deterministic route flow with file/line propagation ----------------

def test_deterministic_route_flow_shows_file_line_and_propagation() -> None:
    change = ChangeSet(
        base="main", head="abc", files=[],
        symbols=[
            ChangedSymbol(id="1", repo="app", file="src/dispatch/signal/service.py",
                          name="get_signal_stats", start_line=973),
        ],
    )
    flow = AffectedFlow(
        id="flow:GET:/stats", entry_label="GET /stats", method="GET", path="/stats",
        handler="return_signal_stats",
        steps=[
            {"kind": "endpoint", "symbol": "return_signal_stats"},
            {"kind": "handler", "symbol": "return_signal_stats"},
            {"kind": "followed_call", "symbol": "get_signal_stats"},
        ],
        sinks=[{"kind": "database", "operation": "query"}],
    )
    impact = AcceptedImpact(
        id=flow.id, label="GET /stats", status="proven", route_method="GET", route_path="/stats",
        verification_model_status="modeled",
    )
    result = _result(change=change, affected_flows=[flow], accepted_impacts=[impact])
    report = render_verify_change_terminal(result)

    assert "Changed" in report
    # Compact single-line format: "<file>:<line> — <symbol>".
    assert "src/dispatch/signal/service.py:973 — get_signal_stats" in report
    assert "System impact" in report
    assert "GET /stats" in report
    assert "→ return_signal_stats" in report
    assert "→ get_signal_stats [changed]" in report
    assert "→ database query" in report


# --- Changed-section relevance filtering (Task items 1-3) -------------------

def _flow_with_changed(*, participant_symbol: str = "get_signal_stats") -> AffectedFlow:
    return AffectedFlow(
        id="flow:GET:/stats", entry_label="GET /stats", method="GET", path="/stats",
        steps=[
            {"kind": "endpoint", "symbol": "return_signal_stats"},
            {"kind": "followed_call", "symbol": participant_symbol},
        ],
    )


def test_changed_test_symbols_are_hidden_by_default() -> None:
    """Task item 1: changed test functions are never headlined by default —
    GitHub's own Files Changed view already shows them."""
    change = ChangeSet(
        base="main", head="abc", files=[],
        symbols=[
            ChangedSymbol(id="1", repo="app", file="src/dispatch/signal/service.py",
                          name="get_signal_stats", start_line=973),
            ChangedSymbol(id="2", repo="app", file="tests/signal/test_signal_data_service.py",
                          name="test_get_signal_stats_basic", start_line=4),
        ],
    )
    flow = _flow_with_changed()
    result = _result(change=change, affected_flows=[flow])
    report = render_verify_change_terminal(result)

    assert "get_signal_stats" in report
    assert "test_get_signal_stats_basic" not in report
    assert "test_signal_data_service.py" not in report


def test_relevant_changed_production_symbols_are_retained() -> None:
    """Task item 2: a changed production symbol that participates in a
    rendered flow (criterion 1) or that an accepted impact already
    attributes itself to (criterion 2) is kept — an unrelated changed
    production symbol that matches neither is not."""
    change = ChangeSet(
        base="main", head="abc", files=[],
        symbols=[
            ChangedSymbol(id="1", repo="app", file="src/dispatch/signal/service.py",
                          name="get_signal_stats", start_line=973),
            ChangedSymbol(id="2", repo="app", file="src/dispatch/signal/models.py",
                          name="SignalStats", start_line=382),
            ChangedSymbol(id="3", repo="app", file="src/unrelated/module.py",
                          name="unrelated_helper", start_line=10),
        ],
    )
    flow = _flow_with_changed()  # participates via flow.steps
    impact = AcceptedImpact(
        id=flow.id, label="GET /stats", status="proven", verification_model_status="modeled",
        changed_symbols=["get_signal_stats", "SignalStats"],  # SignalStats via criterion 2
    )
    result = _result(change=change, affected_flows=[flow], accepted_impacts=[impact])
    report = render_verify_change_terminal(result)

    assert "get_signal_stats" in report
    assert "SignalStats" in report
    assert "unrelated_helper" not in report


def test_more_than_five_relevant_symbols_are_summarized() -> None:
    """Task item: show the first relevant few, then a neutral +N line."""
    symbols = [
        ChangedSymbol(id=str(i), repo="app", file=f"src/app/module_{i}.py", name=f"changed_fn_{i}", start_line=i)
        for i in range(8)
    ]
    change = ChangeSet(base="main", head="abc", files=[], symbols=symbols)
    flow = AffectedFlow(
        id="flow:1", entry_label="GET /stats", method="GET", path="/stats",
        steps=[{"kind": "followed_call", "symbol": f"changed_fn_{i}"} for i in range(8)],
    )
    result = _result(change=change, affected_flows=[flow])
    report = render_verify_change_terminal(result)
    changed_section = report.split("System impact")[0]  # System impact legitimately lists all 8 hops

    for i in range(5):
        assert f"changed_fn_{i}" in changed_section
    for i in range(5, 8):
        assert f"changed_fn_{i}" not in changed_section
    assert "+3 more relevant changed symbols — use --verbose" in changed_section


def test_changed_symbol_location_is_not_described_as_a_diff_line() -> None:
    """`:973` is the symbol's resolved source location, not necessarily the
    exact changed diff line — the report must never claim otherwise."""
    change = ChangeSet(
        base="main", head="abc", files=[],
        symbols=[ChangedSymbol(id="1", repo="app", file="src/app/service.py", name="get_stats", start_line=973)],
    )
    flow = AffectedFlow(
        id="flow:1", entry_label="GET /stats", method="GET", path="/stats",
        steps=[{"kind": "followed_call", "symbol": "get_stats"}],
    )
    result = _result(change=change, affected_flows=[flow])
    report = render_verify_change_terminal(result)

    assert "src/app/service.py:973 — get_stats" in report
    assert "changed at line" not in report.lower()
    assert "changed on line" not in report.lower()


# --- 2. inferred-only impact -------------------------------------------------

def test_inferred_only_impact_shown_with_confidence_why_and_confirmation() -> None:
    impact = AcceptedImpact(
        id="impact:app:process_chat_response", label="process_chat_response", status="inferred",
        llm_confidence=0.84, llm_reason="the changed streaming handler participates in this response path",
        corroborated=False, verification_model_status="unsupported_or_partial",
    )
    result = _result(accepted_impacts=[impact])
    report = render_verify_change_terminal(result)

    assert "Inferred impact" in report
    assert "INFERRED · 0.84" in report
    assert "process_chat_response" in report
    assert "Why:" in report
    assert "the changed streaming handler participates in this response path" in report
    assert "Static confirmation: unavailable" in report
    # No structural flow exists, so nothing implies proof — and no obligation
    # section exists to fabricate either.
    assert "Verification evidence" not in report
    assert "Coverage" not in report


# --- 3. multiple inferred impacts: top few default, rest in --verbose ------

def test_multiple_inferred_impacts_show_top_few_by_default_rest_in_verbose() -> None:
    impacts = [
        AcceptedImpact(id=f"i{i}", label=f"behavior_{i}", status="inferred", llm_confidence=score,
                        llm_reason="reason", corroborated=False, verification_model_status="unsupported_or_partial")
        for i, score in enumerate([0.91, 0.74, 0.55, 0.40, 0.20])
    ]
    result = _result(accepted_impacts=impacts)

    default_report = render_verify_change_terminal(result)
    assert default_report.count("INFERRED · ") == 3
    assert "behavior_0" in default_report and "behavior_1" in default_report and "behavior_2" in default_report
    assert "behavior_3" not in default_report and "behavior_4" not in default_report
    assert "2 more inferred impacts — use --verbose" in default_report
    # Ranked by the existing confidence field, highest first.
    assert default_report.index("behavior_0") < default_report.index("behavior_1") < default_report.index("behavior_2")

    verbose_report = render_verify_change_terminal(result, verbose=True)
    for i in range(5):
        assert f"behavior_{i}" in verbose_report


# --- 4. CI PASS + unverified behavior ---------------------------------------

def test_ci_pass_with_unverified_behavior() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [
        _obligation("GET /stats returns 200", flow_id=flow.id, supporting=True, supporting_count=5),
    ]
    ci = CiSuiteRun(command=["python3", "-m", "pytest"], status=VERIFICATION_PASSED,
                     tests_passed=59, tests_failed=0)
    result = _result(affected_flows=[flow], ci_suite=ci)
    report = render_verify_change_terminal(result)

    assert "CI" in report
    assert "✓ 59 tests passed" in report
    assert "Verification evidence" in report
    assert "? GET /stats returns 200" in report
    assert "Sydes found 5 existing tests that exercise this flow," in report
    assert "but none explicitly establish this behavior." in report
    # No obligation-machinery vocabulary leaks into the default report.
    assert "mapped_tests" not in report
    assert "supporting_tests" not in report


def test_a_passed_obligation_reaches_the_default_report() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="POST /students", method="POST", path="/students")
    flow.obligations = [_obligation("POST /students returns 201", flow_id=flow.id, status=VERIFICATION_PASSED)]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "✓ POST /students returns 201" in report


def test_a_failed_obligation_shows_its_reason() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="POST /refund", method="POST", path="/refund")
    flow.obligations = [
        _obligation("refund is idempotent", flow_id=flow.id, status=VERIFICATION_FAILED,
                     reason="`test_refund_idempotent` failed in the repository test suite"),
    ]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "✗ refund is idempotent" in report
    assert "test_refund_idempotent" in report


def test_zero_supporting_tests_says_no_existing_evidence() -> None:
    """Task item: zero supporting tests must never be worded as if some
    partial coverage exists."""
    flow = AffectedFlow(id="flow:1", entry_label="POST /refund", method="POST", path="/refund")
    flow.obligations = [_obligation("refund is idempotent", flow_id=flow.id, supporting=False)]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "No existing verification evidence establishes this behavior." in report
    assert "Sydes found 0" not in report


# --- 5 (task's numbered list). long implementation-shaped statement --------

def test_response_skeleton_statement_is_shortened() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [
        _obligation(
            "GET /stats responds 200 — Default 200 response skeleton.",
            flow_id=flow.id, kind="route_contract",
        ),
    ]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "? GET /stats returns 200" in report
    assert "Default 200 response skeleton" not in report


def test_long_implementation_heavy_statement_gets_a_neutral_label() -> None:
    raw = (
        "GET /stats accessed entity_subquery = ( db_session.query( "
        "func.jsonb_build_array( SignalStats.id, SignalStats.value ) ) )"
    )
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [_obligation(raw, flow_id=flow.id, kind="state_consistency")]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)

    assert raw not in report
    assert "entity_subquery" not in report
    assert "db_session.query" not in report
    assert "?" in report  # some concise neutral label was rendered instead

    # The original statement is never modified — only this renderer's label
    # differs from it.
    assert flow.obligations[0].statement == raw


def test_short_readable_statement_passes_through_unchanged() -> None:
    """Task: preserve short, already-readable statements as-is."""
    flow = AffectedFlow(id="flow:1", entry_label="POST /orders", method="POST", path="/orders")
    flow.obligations = [_obligation("order total is recalculated", flow_id=flow.id, kind="side_effect")]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "? order total is recalculated" in report


# --- Specific downstream identity in the fallback label ---------------------

_RAW_IMPLEMENTATION_STATEMENT = (
    "GET /stats accessed entity_subquery = ( db_session.query( "
    "func.jsonb_build_array( SignalStats.id, SignalStats.value ) ) )"
)


def test_1_identifiable_downstream_function_from_evidence_and_sink_gives_specific_label() -> None:
    """Priority 1: a symbol the obligation's own evidence/matched sink
    already names, sharpened with the sink's kind/operation."""
    flow = AffectedFlow(
        id="flow:1", entry_label="GET /stats", method="GET", path="/stats", handler="return_signal_stats",
        sinks=[{"kind": "database", "operation": "query", "name": "SELECT signal_stats",
                "symbol": "get_signal_stats"}],
    )
    obligation = VerificationObligation(
        id="ob:1", flow_id=flow.id, kind="state_consistency", statement=_RAW_IMPLEMENTATION_STATEMENT,
        origin="trace_sink", required=True, status=VERIFICATION_UNVERIFIED,
        source_refs=["sink:SELECT signal_stats"],
        evidence=[EvidenceRef(file="service.py", symbol="get_signal_stats", label="trace_sink")],
    )
    flow.obligations = [obligation]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)

    assert "? database query performed by get_signal_stats" in report
    assert _RAW_IMPLEMENTATION_STATEMENT not in report
    assert "entity_subquery" not in report
    # JSON/model statement is untouched.
    assert obligation.statement == _RAW_IMPLEMENTATION_STATEMENT


def test_2_identifiable_handler_only_gives_handler_based_label() -> None:
    """Priority 2: no evidence symbol and no matched sink, but the flow's
    own handler is known."""
    flow = AffectedFlow(
        id="flow:1", entry_label="GET /stats", method="GET", path="/stats", handler="return_signal_stats",
    )
    obligation = VerificationObligation(
        id="ob:1", flow_id=flow.id, kind="route_contract", statement=_RAW_IMPLEMENTATION_STATEMENT,
        origin="api_contract", required=True, status=VERIFICATION_UNVERIFIED,
    )
    flow.obligations = [obligation]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "? return_signal_stats response behavior" in report


def test_3_named_sink_alone_gives_a_useful_sink_label() -> None:
    """A matched sink with no attributable symbol still contributes its own
    kind/operation, rather than falling all the way to the generic label."""
    flow = AffectedFlow(
        id="flow:1", entry_label="GET /stats", method="GET", path="/stats",
        sinks=[{"kind": "external_api", "operation": "call", "name": "payments client"}],
    )
    obligation = VerificationObligation(
        id="ob:1", flow_id=flow.id, kind="cross_repo_call", statement=_RAW_IMPLEMENTATION_STATEMENT,
        origin="trace_sink", required=True, status=VERIFICATION_UNVERIFIED,
        source_refs=["sink:payments client"],
    )
    flow.obligations = [obligation]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "? external_api call" in report


def test_4_no_useful_identity_keeps_the_generic_fallback() -> None:
    """Priority 5: nothing usable anywhere (no evidence symbol, no matched
    sink, no handler, no flow at all) — the pre-existing generic label."""
    obligation = VerificationObligation(
        id="ob:1", flow_id="flow:unknown", kind="side_effect", statement=_RAW_IMPLEMENTATION_STATEMENT,
        origin="trace_sink", required=True, status=VERIFICATION_UNVERIFIED,
    )
    flow = AffectedFlow(id="flow:other", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [obligation]  # obligation.flow_id deliberately does not match any rendered flow
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    assert "? Changed downstream behavior" in report


def test_6_verbose_output_retains_the_original_obligation_statement() -> None:
    flow = AffectedFlow(
        id="flow:1", entry_label="GET /stats", method="GET", path="/stats", handler="return_signal_stats",
        sinks=[{"kind": "database", "operation": "query", "name": "SELECT signal_stats",
                "symbol": "get_signal_stats"}],
    )
    obligation = VerificationObligation(
        id="ob:1", flow_id=flow.id, kind="state_consistency", statement=_RAW_IMPLEMENTATION_STATEMENT,
        origin="trace_sink", required=True, status=VERIFICATION_UNVERIFIED,
        source_refs=["sink:SELECT signal_stats"],
        evidence=[EvidenceRef(file="service.py", symbol="get_signal_stats", label="trace_sink")],
    )
    flow.obligations = [obligation]
    result = _result(affected_flows=[flow])

    default_report = render_verify_change_terminal(result)
    verbose_report = render_verify_change_terminal(result, verbose=True)

    assert _RAW_IMPLEMENTATION_STATEMENT not in default_report
    assert _RAW_IMPLEMENTATION_STATEMENT in verbose_report


# --- 5. CI UNKNOWN due to missing dependency --------------------------------

def test_ci_unknown_due_to_missing_dependency() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [_obligation("GET /stats returns 200", flow_id=flow.id)]
    ci = CiSuiteRun(
        command=["python3", "-m", "pytest"], status=VERIFICATION_UNKNOWN, blocker="missing_dependency",
        reason="Suite requires `easydict`, which is not available in this environment",
    )
    result = _result(affected_flows=[flow], ci_suite=ci)
    report = render_verify_change_terminal(result)

    assert "? python3 -m pytest could not run" in report
    assert "missing dependency: easydict" in report
    assert "The repository test suite could not run because `easydict` is unavailable." in report


# --- 6. runtime requirements shown compactly --------------------------------

def test_runtime_requirements_shown_compactly() -> None:
    deps = [
        RuntimeDependency(id="d1", name="PostgreSQL", kind="database"),
        RuntimeDependency(id="d2", name="Dispatch UI service", kind="service"),
        RuntimeDependency(id="d3", name="AWS services", kind="service"),
        RuntimeDependency(id="d4", name="PostgreSQL", kind="database"),  # duplicate name, deduped
    ]
    result = _result(runtime_dependencies=deps)
    report = render_verify_change_terminal(result)

    assert "Full verification requires" in report
    assert "PostgreSQL · Dispatch UI service · AWS services" in report
    assert report.count("PostgreSQL") == 1


def test_runtime_requirements_section_omitted_when_none_detected() -> None:
    result = _result()
    report = render_verify_change_terminal(result)
    assert "Full verification requires" not in report


# --- 7. partial/unresolved analysis summarized under "could not establish" -

def test_could_not_establish_summarizes_unmodeled_and_unresolved_facts() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [_obligation("GET /stats returns 200", flow_id=flow.id)]
    modeled = AcceptedImpact(id=flow.id, label="GET /stats", status="proven", verification_model_status="modeled")
    unmodeled = [
        AcceptedImpact(id=f"u{i}", label="other", status="proven", verification_model_status="unsupported_or_partial")
        for i in range(3)
    ]
    result = _result(
        affected_flows=[flow], accepted_impacts=[modeled, *unmodeled], unresolved_changed_symbols=5,
    )
    report = render_verify_change_terminal(result)

    assert "What Sydes could not establish" in report
    assert "1 affected behavior is not established by existing tests." in report
    assert "3 additional affected areas are not yet verification-modeled." in report
    assert "5 changed symbols have unresolved impact paths." in report


def test_could_not_establish_section_omitted_when_everything_is_established() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="POST /students", method="POST", path="/students")
    flow.obligations = [_obligation("POST /students returns 201", flow_id=flow.id, status=VERIFICATION_PASSED)]
    impact = AcceptedImpact(id=flow.id, label="POST /students", status="proven", verification_model_status="modeled")
    result = _result(affected_flows=[flow], accepted_impacts=[impact])
    report = render_verify_change_terminal(result)
    assert "What Sydes could not establish" not in report


# --- Wording principle: describe the boundary, never prescribe an action ---

def test_could_not_establish_never_prescribes_developer_actions() -> None:
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [_obligation("GET /stats returns 200", flow_id=flow.id)]
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)
    for banned in ("Add test_", "Install ", "Create a mock", "You must", "You should"):
        assert banned not in report


# --- Coverage section removed from the default report only -----------------

def test_coverage_section_removed_from_default_but_counts_remain_available() -> None:
    """Task item 3: "Coverage" is gone from the concise report (confusable
    with code coverage; the same information already lives in Verification
    evidence / What Sydes could not establish) — but the underlying counts
    are untouched on the model, so JSON/verbose still carry them."""
    flow = AffectedFlow(id="flow:1", entry_label="GET /stats", method="GET", path="/stats")
    flow.obligations = [_obligation("GET /stats returns 200", flow_id=flow.id)]
    result = _result(affected_flows=[flow])

    default_report = render_verify_change_terminal(result)
    assert "Coverage" not in default_report
    assert "not established ·" not in default_report

    # The counts themselves are untouched — same values a JSON consumer or
    # --verbose reader would see.
    assert result.summary.counts.obligations == 1
    assert result.summary.counts.obligations_passed == 0
    verbose_report = render_verify_change_terminal(result, verbose=True)
    assert "Obligations: 1" in verbose_report


# --- 8. verbose mode still exposes detailed information ---------------------

def test_verbose_mode_still_exposes_the_detailed_report() -> None:
    impacts = [
        AcceptedImpact(id=f"p{i}", label=f"struct_{i}", status="proven", verification_model_status="unsupported_or_partial")
        for i in range(5)
    ]
    result = _result(accepted_impacts=impacts)

    default_report = render_verify_change_terminal(result)
    verbose_report = render_verify_change_terminal(result, verbose=True)

    assert "SYDES CHANGE VERIFICATION" not in default_report
    assert "SYDES CHANGE VERIFICATION" in verbose_report
    assert "AFFECTED BEHAVIOR" in verbose_report
    assert "Proven impacts: 5" in verbose_report
    for i in range(5):
        assert f"struct_{i}" in verbose_report  # nothing summarized away in verbose


# --- 9. default output does not headline raw changed-symbol/debug counts ---

def test_default_report_does_not_headline_raw_debug_counts() -> None:
    change = ChangeSet(
        base="main", head="abc", files=[],
        symbols=[ChangedSymbol(id=str(i), repo="app", file="a.py", name=f"f{i}", start_line=i) for i in range(8)],
    )
    result = _result(change=change)
    report = render_verify_change_terminal(result)
    assert "Changed symbols: 8" not in report
    assert "Proven impacts:" not in report
    assert "Inferred impacts:" not in report
    assert "PROVEN:" not in report
    assert "INFERRED:" not in report


# --- 10. long propagation traces are preserved, not truncated --------------

def test_long_propagation_trace_is_preserved_not_truncated() -> None:
    steps = [{"kind": "endpoint", "symbol": "handler"}]
    for i in range(120):
        steps.append({"kind": "followed_call", "symbol": f"step_{i}"})
    flow = AffectedFlow(id="flow:1", entry_label="POST /orders", method="POST", path="/orders", steps=steps)
    result = _result(affected_flows=[flow])
    report = render_verify_change_terminal(result)

    for i in (0, 59, 119):
        assert f"→ step_{i}" in report
    assert "hidden" not in report.lower()
    assert "collapsed" not in report.lower()


# --- clean empty-state rendering (no crashes, no empty sections) -----------

def test_empty_result_renders_cleanly() -> None:
    report = render_verify_change_terminal(_result())
    assert "SYDES VERIFICATION" in report
    assert "No changes against main." in report
    assert "No structural propagation path was established." in report
    assert "Verdict" in report
    assert "VERIFICATION INCOMPLETE" in report


def test_verified_verdict_renders_in_default_report() -> None:
    result = _result(summary=ChangeSummary(risk=RISK_HIGH, verdict=VERDICT_VERIFIED, counts=VerificationCounts()))
    report = render_verify_change_terminal(result)
    assert "VERIFIED" in report
