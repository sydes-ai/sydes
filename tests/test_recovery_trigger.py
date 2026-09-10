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
