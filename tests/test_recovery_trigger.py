"""`sydes.recovery.trigger` — deciding whether a first-pass result has a
high-value gap worth AI recovery. Every fixture here uses only the generic
`ChangeVerificationResult` schema; none of these tests, or the module under
test, ever mentions a framework, library, or language name.
"""

from __future__ import annotations

from sydes.recovery.trigger import (
    GAP_BOUNDARY_WITHOUT_FLOW,
    GAP_MISSING_TEST_MAPPING,
    GAP_NO_ESTABLISHED_FLOW,
    GAP_ONLY_INFERRED_IMPACT,
    GAP_UNRESOLVED_CHANGED_SYMBOLS,
    evaluate_trigger,
)
from sydes.verify.models import (
    AcceptedImpact,
    AffectedBoundary,
    AffectedFlow,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    ChangeSummary,
    ChangeVerificationResult,
    VerificationCounts,
    VERDICT_INCOMPLETE,
    VERDICT_VERIFIED,
)

REPO = "app"


def _change(files: list[ChangedFile] | None = None) -> ChangeSet:
    return ChangeSet(
        base="main", head="abc",
        symbols=[ChangedSymbol(id="app:svc.py:handler", repo=REPO, file="svc.py", name="handler")],
        files=files or [],
    )


def _result(*, files: list[ChangedFile] | None = None, **overrides) -> ChangeVerificationResult:
    base = {
        "change": _change(files),
        "summary": ChangeSummary(verdict=VERDICT_INCOMPLETE, counts=VerificationCounts()),
    }
    base.update(overrides)
    return ChangeVerificationResult(**base)


def test_no_trigger_when_result_already_has_complete_established_path():
    result = _result(
        summary=ChangeSummary(verdict=VERDICT_VERIFIED, counts=VerificationCounts(mapped_tests=1)),
        affected_flows=[AffectedFlow(id="f1", entry_label="POST /users", impact_status="proven")],
    )
    assert evaluate_trigger(result) is None


def test_no_trigger_on_a_minor_note_alone():
    result = _result(analysis_notes=["a minor structural note with no real gap"])
    assert evaluate_trigger(result) is None


def test_trigger_when_no_path_exists_at_all_but_real_signal_present():
    result = _result(
        accepted_impacts=[AcceptedImpact(id="i1", label="something", status="proven")],
    )
    trigger = evaluate_trigger(result)
    assert trigger is not None
    assert GAP_NO_ESTABLISHED_FLOW in trigger.gap_kinds


def test_trigger_when_only_inferred_impact_exists():
    result = _result(
        accepted_impacts=[AcceptedImpact(id="i1", label="maybe affected", status="inferred")],
    )
    trigger = evaluate_trigger(result)
    assert trigger is not None
    assert GAP_ONLY_INFERRED_IMPACT in trigger.gap_kinds


def test_trigger_when_boundary_exists_with_no_matching_flow():
    result = _result(
        affected_boundaries=[AffectedBoundary(id="b1", kind="api", status="proven")],
    )
    trigger = evaluate_trigger(result)
    assert trigger is not None
    assert GAP_BOUNDARY_WITHOUT_FLOW in trigger.gap_kinds


def test_trigger_when_test_mapping_missing_despite_a_new_test_file():
    result = _result(
        files=[ChangedFile(repo=REPO, path="svc_test.py", change_type="added", role="test_usage_candidate")],
        summary=ChangeSummary(verdict=VERDICT_INCOMPLETE, counts=VerificationCounts(mapped_tests=0)),
    )
    trigger = evaluate_trigger(result)
    assert trigger is not None
    assert GAP_MISSING_TEST_MAPPING in trigger.gap_kinds
    assert "svc_test.py" in trigger.extra_test_candidate_files


def test_no_trigger_when_tests_are_mapped_even_with_a_changed_test_file():
    result = _result(
        files=[ChangedFile(repo=REPO, path="svc_test.py", change_type="added", role="test_usage_candidate")],
        summary=ChangeSummary(verdict=VERDICT_VERIFIED, counts=VerificationCounts(mapped_tests=3)),
        affected_flows=[AffectedFlow(id="f1", entry_label="POST /users", impact_status="proven")],
    )
    assert evaluate_trigger(result) is None


# ---------------------------------------------------------------------------
# Issue 3: one established flow must not mask a genuinely separate,
# unresolved gap elsewhere on the same result.
# ---------------------------------------------------------------------------


def test_trigger_fires_for_a_separate_inferred_impact_despite_an_established_flow():
    """One flow IS established -- the top-level VERIFIED/OK short-circuit
    does not apply because the verdict is INCOMPLETE -- and a genuinely
    separate accepted impact on the same result is still only inferred.
    That must still trigger recovery for the separate gap, not be masked
    by the flow that's already fine."""
    result = _result(
        affected_flows=[AffectedFlow(id="f1", entry_label="POST /users", impact_status="proven")],
        accepted_impacts=[
            AcceptedImpact(id="f1", label="POST /users", status="proven"),
            AcceptedImpact(id="i2", label="a separate maybe-affected behavior", status="inferred"),
        ],
    )
    trigger = evaluate_trigger(result)
    assert trigger is not None
    assert GAP_ONLY_INFERRED_IMPACT in trigger.gap_kinds


def test_trigger_fires_for_unresolved_changed_symbols_despite_an_established_flow():
    """An established flow exists, but the first pass still could not
    connect some OTHER changed symbol to any entrypoint -- that gap is
    exactly as real as it would be with nothing established at all."""
    result = _result(
        affected_flows=[AffectedFlow(id="f1", entry_label="POST /users", impact_status="proven")],
        accepted_impacts=[AcceptedImpact(id="f1", label="POST /users", status="proven")],
        unresolved_changed_symbols=2,
    )
    trigger = evaluate_trigger(result)
    assert trigger is not None
    assert GAP_UNRESOLVED_CHANGED_SYMBOLS in trigger.gap_kinds


def test_no_trigger_when_everything_is_already_established_and_resolved():
    """Sanity check: a fully clean, all-proven result with no residual gap
    of any kind must still not trigger -- the fix must not make recovery
    fire unconditionally just because a result has more than one flow."""
    result = _result(
        summary=ChangeSummary(verdict=VERDICT_VERIFIED, counts=VerificationCounts(mapped_tests=1)),
        affected_flows=[
            AffectedFlow(id="f1", entry_label="POST /users", impact_status="proven"),
            AffectedFlow(id="f2", entry_label="GET /users", impact_status="proven"),
        ],
        accepted_impacts=[
            AcceptedImpact(id="f1", label="POST /users", status="proven"),
            AcceptedImpact(id="f2", label="GET /users", status="proven"),
        ],
    )
    assert evaluate_trigger(result) is None
