"""The frozen-batch diagnostics schema: classification, not collection.

Nothing here runs a PR — these pin `classify_pr_result`'s categories against
plain fixtures, so the schema is provably sane before it is ever pointed at
real data.
"""

from __future__ import annotations

from sydes.impact.pr_evaluation import (
    DANGEROUS_FALSE_VERIFIED,
    DETERMINISTIC_SOLVED,
    INCORRECT_IMPACT,
    INFRA_FAILURE,
    LLM_GUIDED_SOLVED,
    PARTIAL_IMPACT,
    RESULT_CATEGORIES,
    UNRESOLVED_STRUCTURAL_GAP,
    build_pr_evaluation_record,
    classify_pr_result,
)


def test_deterministic_solved_matches_expected_with_no_guide_involvement() -> None:
    category = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=("GET /x",),
        expected_affected_entrypoints=("GET /x",),
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert category == DETERMINISTIC_SOLVED


def test_llm_guided_solved_when_a_recovered_entrypoint_carries_guided_provenance() -> None:
    category = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=("GET /x",),
        expected_affected_entrypoints=("GET /x",),
        guide_triggered=True,
        llm_guided_entrypoints=("GET /x",),
        infra_error=None,
    )
    assert category == LLM_GUIDED_SOLVED


def test_partial_impact_when_some_but_not_all_expected_entrypoints_found() -> None:
    category = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=("GET /x",),
        expected_affected_entrypoints=("GET /x", "POST /y"),
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert category == PARTIAL_IMPACT


def test_unresolved_structural_gap_when_nothing_found_and_nothing_claimed() -> None:
    category = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=(),
        expected_affected_entrypoints=("GET /x",),
        guide_triggered=True,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert category == UNRESOLVED_STRUCTURAL_GAP


def test_incorrect_impact_when_a_wrong_entrypoint_is_claimed() -> None:
    category = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=("GET /wrong",),
        expected_affected_entrypoints=("GET /x",),
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert category == INCORRECT_IMPACT


def test_dangerous_false_verified_when_verified_despite_a_missing_expected_entrypoint() -> None:
    """The one outcome every M3 safety property exists to prevent."""
    category = classify_pr_result(
        verdict="VERIFIED",
        final_affected_entrypoints=(),
        expected_affected_entrypoints=("GET /x",),
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert category == DANGEROUS_FALSE_VERIFIED


def test_dangerous_false_verified_without_a_reference_when_verified_finds_nothing() -> None:
    """Without a human reference, a VERIFIED verdict with zero affected
    entrypoints cannot be trusted as "genuinely inert" — flagged, not assumed
    safe, so a real regression here is never silently miscategorized."""
    category = classify_pr_result(
        verdict="VERIFIED",
        final_affected_entrypoints=(),
        expected_affected_entrypoints=None,
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert category == DANGEROUS_FALSE_VERIFIED


def test_infra_failure_takes_priority_over_every_other_signal() -> None:
    category = classify_pr_result(
        verdict="VERIFIED",
        final_affected_entrypoints=("GET /x",),
        expected_affected_entrypoints=("GET /x",),
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error="CBM indexing timed out",
    )
    assert category == INFRA_FAILURE


def test_classification_without_a_reference_falls_back_to_run_signals() -> None:
    solved = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=("GET /x",),
        expected_affected_entrypoints=None,
        guide_triggered=False,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert solved == DETERMINISTIC_SOLVED

    unresolved = classify_pr_result(
        verdict="VERIFICATION INCOMPLETE",
        final_affected_entrypoints=(),
        expected_affected_entrypoints=None,
        guide_triggered=True,
        llm_guided_entrypoints=(),
        infra_error=None,
    )
    assert unresolved == UNRESOLVED_STRUCTURAL_GAP


def test_all_seven_categories_are_exposed() -> None:
    assert len(RESULT_CATEGORIES) == 7
    assert len(set(RESULT_CATEGORIES)) == 7


def test_build_pr_evaluation_record_from_impact_metrics_shape() -> None:
    """`impact_metrics` is exactly the dict `ImpactResult.metrics` already
    produces — no new instrumentation needed to fill this in."""
    metrics = {
        "guide_triggered": True, "guide_calls": 2,
        "guide_actions": {"inspect_nearby_entrypoints": 1, "inspect_enclosing_function": 1},
        "evidence_confirmed": 2, "guide_latency_ms": 842.5,
    }
    record = build_pr_evaluation_record(
        pr_id="example/repo#123",
        repo="app",
        changed_symbols=["handler"],
        impact_metrics=metrics,
        deterministic_affected_entrypoints=[],
        final_affected_entrypoints=["POST /x"],
        llm_guided_entrypoints=["POST /x"],
        verdict="VERIFICATION INCOMPLETE",
        obligations_generated=3,
        evidence_mapped_count=1,
        total_latency_ms=5000.0,
        cbm_latency_ms=150.0,
    )
    assert record.category == LLM_GUIDED_SOLVED
    assert record.guide_calls == 2
    assert record.guide_latency_ms == 842.5
    payload = record.to_dict()
    assert payload["pr_id"] == "example/repo#123"
    assert payload["category"] == LLM_GUIDED_SOLVED
    assert payload["token_usage"] is None  # not estimated when the client doesn't report it
