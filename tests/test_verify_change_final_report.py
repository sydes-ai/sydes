"""M4 loop-closing: PROVEN + INFERRED impacts in the final human-readable report.

`render_verify_change_terminal` is exercised directly against hand-built
`ChangeVerificationResult` objects — the product's canonical artifact — so
these pin exactly what a reviewer sees, independent of any live CBM/LLM run.
"""

from __future__ import annotations

from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.verify.models import (
    AcceptedImpact,
    ChangeSet,
    ChangeVerificationResult,
)


def _result(accepted_impacts: list[AcceptedImpact], **overrides) -> ChangeVerificationResult:
    change = ChangeSet(base="main", head="abc123", files=[], symbols=[])
    return ChangeVerificationResult(change=change, accepted_impacts=accepted_impacts, **overrides)


def test_proven_impact_reaches_the_final_human_report() -> None:
    impact = AcceptedImpact(
        id="flow:GET:/engagements", label="GET /engagements", repo="app",
        kind="http_route", status="proven", route_method="GET", route_path="/engagements",
        verification_model_status="modeled",
    )
    report = render_verify_change_terminal(_result([impact]))
    assert "AFFECTED BEHAVIOR" in report
    assert "Proven impacts: 1" in report
    assert "GET /engagements" in report


def test_inferred_uncorroborated_impact_reaches_the_final_human_report() -> None:
    """An uncorroborated inference must appear — `corroborated=False` must
    never cause it to disappear from the report."""
    impact = AcceptedImpact(
        id="impact:app:restricted_case_filter", label="GET /cases", repo="app",
        kind="http_route", status="inferred", route_method="GET", route_path="/cases",
        llm_confidence=0.86,
        llm_reason="restricted_case_filter appears to participate in the shared case-query filtering path",
        llm_inference_type="shared_utility",
        llm_uncertainty="indirect invocation is not represented by the current graph",
        corroborated=False, verification_model_status="unsupported_or_partial",
    )
    report = render_verify_change_terminal(_result([impact]))
    assert "Inferred impacts: 1" in report
    assert "GET /cases" in report
    assert "INFERRED" in report


def test_inferred_rationale_confidence_and_uncertainty_are_visible() -> None:
    impact = AcceptedImpact(
        id="impact:app:x", label="GET /cases", status="inferred",
        llm_confidence=0.86, llm_reason="shares a query helper",
        llm_uncertainty="graph has no direct edge", corroborated=False,
        verification_model_status="unsupported_or_partial",
    )
    report = render_verify_change_terminal(_result([impact]))
    assert "0.86" in report
    assert "LLM confidence" in report
    assert "shares a query helper" in report
    assert "graph has no direct edge" in report
    # Honest wording: the confidence line must disclaim itself as model
    # self-assessment, never presented as a calibrated probability outright.
    assert "not a calibrated probability" in report.lower()
    assert "probability of correctness" not in report.lower()
    assert "verification confidence" not in report.lower()


def test_corroborated_inferred_impact_shows_corroboration_status() -> None:
    impact = AcceptedImpact(
        id="flow:GET:/x", label="GET /x", status="inferred",
        llm_confidence=0.9, corroborated=True, verification_model_status="modeled",
    )
    report = render_verify_change_terminal(_result([impact]))
    assert "matched a known entrypoint" in report


def test_generic_non_http_accepted_behavior_remains_visible() -> None:
    """A behavior with no HTTP shape (a scheduled job, e.g.) must still be
    named in the report — never silently discarded for lacking a route."""
    impact = AcceptedImpact(
        id="impact:app:incident_report_weekly", label="incident_report_weekly",
        status="inferred", kind="decorated", llm_confidence=0.55,
        llm_reason="reads a cached value the changed function produces",
        verification_model_status="unsupported_or_partial",
    )
    report = render_verify_change_terminal(_result([impact]))
    assert "incident_report_weekly" in report
    assert "Inferred impacts: 1" in report


def test_accepted_impact_not_modeled_is_labelled_rather_than_dropped() -> None:
    """Test 5: an impact the downstream flow pipeline could not fully
    represent must stay visible with a clear reason, never vanish."""
    impact = AcceptedImpact(
        id="impact:app:helper", label="helper", status="proven",
        verification_model_status="unsupported_or_partial",
    )
    report = render_verify_change_terminal(_result([impact]))
    assert "helper" in report
    assert "not yet modeled" in report


def test_summary_counts_are_derived_from_canonical_merged_impacts() -> None:
    """Test 12: the top-of-report counts must match `accepted_impacts`,
    never a separately-tallied number."""
    impacts = [
        AcceptedImpact(id="a", label="a", status="proven", verification_model_status="modeled"),
        AcceptedImpact(id="b", label="b", status="proven", verification_model_status="modeled"),
        AcceptedImpact(id="c", label="c", status="inferred", verification_model_status="unsupported_or_partial"),
    ]
    result = _result(impacts)
    result.summary.counts.impacts_proven = sum(1 for i in impacts if i.status == "proven")
    result.summary.counts.impacts_inferred = sum(1 for i in impacts if i.status == "inferred")
    assert result.summary.counts.impacts_proven == 2
    assert result.summary.counts.impacts_inferred == 1
    report = render_verify_change_terminal(result)
    assert "Proven impacts: 2" in report
    assert "Inferred impacts: 1" in report


def test_provider_failure_is_visible_in_the_human_readable_report() -> None:
    """Tests 9/10: a sanitized AI-inference failure must appear in the
    report's default (non-verbose) view, making clear that deterministic
    analysis still ran and coverage may be incomplete."""
    result = _result(
        [AcceptedImpact(id="a", label="GET /x", status="proven", verification_model_status="modeled")],
        analysis_notes=[
            "AI impact inference unavailable: OpenAI request failed for model 'gpt-5.5': boom",
            "Deterministic impact analysis still ran and is reflected below; "
            "AI-inferred impact coverage may be incomplete.",
        ],
    )
    report = render_verify_change_terminal(result)
    assert "AI impact inference unavailable" in report
    assert "gpt-5.5" in report
    assert "Deterministic impact analysis still ran" in report
    # No secrets/keys/stack traces — the note text itself is already
    # sanitized upstream; this just confirms nothing extra was appended.
    assert "Traceback" not in report


def test_empty_accepted_impacts_renders_cleanly() -> None:
    report = render_verify_change_terminal(_result([]))
    assert "Proven impacts: 0" in report
    assert "Inferred impacts: 0" in report
    assert "No affected behavior identified." in report


# --- Report prioritization: #6155 produced 222 PROVEN impacts, most of them
# structural-only and many sharing the same low-information label (e.g.
# "GET /" dozens of times) — dumping all of them individually made the
# default report unreadable. These tests pin the fix. ------------------------

def _modeled(n: int, prefix: str = "GET /route") -> list[AcceptedImpact]:
    return [
        AcceptedImpact(
            id=f"flow:{i}", label=f"{prefix}{i}", status="proven",
            route_method="GET", route_path=f"/route{i}", verification_model_status="modeled",
        )
        for i in range(n)
    ]


def _structural(n: int, label: str = "case_update_flow") -> list[AcceptedImpact]:
    return [
        AcceptedImpact(id=f"impact:{i}", label=label, status="proven", kind="function",
                        verification_model_status="unsupported_or_partial")
        for i in range(n)
    ]


def test_default_report_does_not_dump_hundreds_of_unsupported_impacts() -> None:
    impacts = _modeled(3) + _structural(219, label="case_update_flow")
    report = render_verify_change_terminal(_result(impacts))

    assert "Proven impacts: 222" in report
    # None of the 219 structural duplicates get an individual line — the
    # label is collapsed into one summarized row with a count instead.
    assert report.count("case_update_flow") <= 2  # the collapsed row itself, not per-item
    assert "219 more impact(s) were identified but are not yet modeled" in report
    assert "Use --verbose or structured JSON for the complete list." in report


def test_modeled_impacts_remain_visible_and_prioritized_over_structural_noise() -> None:
    impacts = _structural(50) + _modeled(3)
    report = render_verify_change_terminal(_result(impacts))

    assert "Modeled / verification-relevant impacts:" in report
    for i in range(3):
        assert f"GET /route{i}" in report
    modeled_pos = report.index("Modeled / verification-relevant impacts:")
    structural_pos = report.index("Additional structural impacts:")
    assert modeled_pos < structural_pos  # modeled comes first, not buried after 50 structural rows


def test_inferred_impacts_remain_fully_visible_even_alongside_many_proven() -> None:
    impacts = _structural(200) + [
        AcceptedImpact(
            id="impact:inferred:1", label="GET /cases", status="inferred",
            llm_confidence=0.72, llm_reason="shares a query helper", corroborated=False,
            verification_model_status="unsupported_or_partial",
        ),
    ]
    report = render_verify_change_terminal(_result(impacts))
    assert "Inferred impacts: 1" in report
    assert "GET /cases" in report
    assert "LLM confidence: 0.72" in report


def test_repeated_low_information_labels_are_collapsed_with_a_count() -> None:
    impacts = _structural(15, label="GET /")
    report = render_verify_change_terminal(_result(impacts))
    assert "GET /  (×15)" in report
    assert report.count("GET /") <= 2  # one collapsed row, not 15 individual lines


def test_verbose_report_still_exposes_the_complete_proven_list() -> None:
    impacts = _modeled(2) + _structural(30, label="case_update_flow")
    report = render_verify_change_terminal(_result(impacts), verbose=True)
    assert report.count("case_update_flow") == 30  # every one rendered, nothing summarized away
    assert "GET /route0" in report and "GET /route1" in report


def test_report_summarization_never_mutates_the_canonical_accepted_impacts_list() -> None:
    impacts = _modeled(2) + _structural(30)
    result = _result(impacts)
    original_count = len(result.accepted_impacts)
    render_verify_change_terminal(result)
    render_verify_change_terminal(result, verbose=True)
    assert len(result.accepted_impacts) == original_count == 32
