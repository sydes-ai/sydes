"""`sydes.recovery.merge` — read-only, additive views. Nothing here ever
takes a `ChangeVerificationResult` as input, which is itself the structural
guarantee that this prototype cannot mutate the canonical result or the
CBM graph: there is no code path in this module capable of doing so."""

from __future__ import annotations

from sydes.recovery.merge import build_recovery_view, summarize_for_notes
from sydes.recovery.schema import (
    RecoveredEdge,
    RecoveredEvidence,
    RecoveredNode,
    RecoveredPath,
    RecoveredTest,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
)


def _established_path() -> RecoveredPath:
    edge = RecoveredEdge(
        **{"from": "route", "to": "handler"}, relationship="registers", status=STATUS_ESTABLISHED,
        provenance="ai_recovery", evidence=[RecoveredEvidence(file="a.ts", fact="registers the route")],
    )
    return RecoveredPath(
        entrypoint="GET /users", target_node="handler",
        nodes=[RecoveredNode(symbol="route", file="a.ts"), RecoveredNode(symbol="handler", file="a.ts")],
        edges=[edge], status=STATUS_ESTABLISHED,
    )


def _partial_path() -> RecoveredPath:
    kept_edge = RecoveredEdge(
        **{"from": "route", "to": "controller"}, relationship="routes", status=STATUS_ESTABLISHED,
        provenance="ai_recovery", evidence=[RecoveredEvidence(file="a.ts", fact="route declaration")],
    )
    dropped_edge = RecoveredEdge(
        **{"from": "controller", "to": "handler"}, relationship="dispatches", status=STATUS_UNRESOLVED,
        provenance="ai_recovery_exhausted", rejection_reason="no dispatch evidence",
    )
    return RecoveredPath(
        entrypoint="POST /orders", target_node="handler",
        nodes=[RecoveredNode(symbol="route", file="a.ts"), RecoveredNode(symbol="controller", file="a.ts")],
        edges=[kept_edge], status=STATUS_PARTIAL, unresolved_suffix=[dropped_edge],
    )


def test_build_recovery_view_tags_every_edge_with_its_own_provenance():
    recovery = RecoveryResult(status=STATUS_ESTABLISHED, recovered_paths=[_established_path(), _partial_path()])
    view = build_recovery_view(recovery)
    assert view["recovered_paths"][0]["edges"][0]["provenance"] == "ai_recovery"
    assert view["recovered_paths"][1]["unresolved_suffix"][0]["provenance"] == "ai_recovery_exhausted"


def test_build_recovery_view_reports_dropped_padding_as_absent_not_unresolved():
    # A path whose padding was already dropped by verify.py has nothing
    # beyond target_node in either nodes or unresolved_suffix.
    path = _established_path()
    view = build_recovery_view(RecoveryResult(status=STATUS_ESTABLISHED, recovered_paths=[path]))
    assert view["recovered_paths"][0]["unresolved_suffix"] == []
    assert len(view["recovered_paths"][0]["nodes"]) == 2


def test_build_recovery_view_is_additive_recovered_tests_never_drop_anything():
    tests = [
        RecoveredTest(file="a.spec.ts", test="rejects too-large limit", covers="limit validation", status="accepted"),
        RecoveredTest(file="a.spec.ts", test="allows a normal limit", covers="limit validation happy path", status="rejected"),
    ]
    recovery = RecoveryResult(status=STATUS_UNRESOLVED, recovered_tests=tests)
    view = build_recovery_view(recovery)
    assert len(view["recovered_tests"]) == 2


def test_summarize_for_notes_established_never_claims_structural_provenance():
    recovery = RecoveryResult(status=STATUS_ESTABLISHED, recovered_paths=[_established_path()])
    note = summarize_for_notes(recovery)
    assert "ai_recovery" in note
    assert "experimental" in note.lower()


def test_summarize_for_notes_distinguishes_partial_from_established():
    recovery = RecoveryResult(status=STATUS_PARTIAL, recovered_paths=[_partial_path()])
    note = summarize_for_notes(recovery)
    assert "partial" in note.lower()
    assert "established" not in note.lower().split("partial")[0]  # doesn't lead with a false "established" claim


def test_summarize_for_notes_on_fully_unresolved_recovery_is_honest():
    recovery = RecoveryResult(
        status=STATUS_UNRESOLVED,
        unresolved=[{"question": "does anything call this?", "missing_evidence": "no registration found"}],
    )
    note = summarize_for_notes(recovery)
    assert "could not establish" in note.lower()
