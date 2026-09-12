"""`sydes.recovery.merge` — read-only, additive views, built independently
for path recovery and test recovery. Nothing here ever takes a
`ChangeVerificationResult` as input, which is itself the structural
guarantee that this prototype cannot mutate the canonical result or the
CBM graph: there is no code path in this module capable of doing so."""

from __future__ import annotations

from sydes.recovery.merge import build_recovery_view, summarize_for_notes
from sydes.recovery.schema import (
    EntityRef,
    PathRecoveryResult,
    ROOT_CANDIDATE_BOUNDARY,
    RecoveredEdge,
    RecoveredPath,
    RecoveredTest,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    TEST_STATUS_ACCEPTED,
    TEST_STATUS_REJECTED,
    TestRecoveryResult,
)


def _entity(symbol: str, file: str) -> EntityRef:
    return EntityRef(symbol=symbol, file=file)


def _established_path() -> RecoveredPath:
    route, handler = _entity("route", "a.ts"), _entity("handler", "a.ts")
    edge = RecoveredEdge(
        **{"from": route, "to": handler}, relationship="registers", status=STATUS_ESTABLISHED,
        provenance="ai_recovery", evidence=[{"file": "a.ts", "fact": "registers the route"}],
    )
    return RecoveredPath(entrypoint="GET /users", target_node="handler", nodes=[route, handler], edges=[edge], status=STATUS_ESTABLISHED)


def _partial_path() -> RecoveredPath:
    route, controller, handler = _entity("route", "a.ts"), _entity("controller", "a.ts"), _entity("handler", "a.ts")
    kept_edge = RecoveredEdge(
        **{"from": route, "to": controller}, relationship="routes", status=STATUS_ESTABLISHED,
        provenance="ai_recovery", evidence=[{"file": "a.ts", "fact": "route declaration"}],
    )
    dropped_edge = RecoveredEdge(
        **{"from": controller, "to": handler}, relationship="dispatches", status=STATUS_UNRESOLVED,
        provenance="ai_recovery_exhausted", rejection_reason="no dispatch evidence",
    )
    return RecoveredPath(
        entrypoint="POST /orders", target_node="handler", nodes=[route, controller], edges=[kept_edge],
        status=STATUS_PARTIAL, unresolved_suffix=[dropped_edge],
    )


def test_build_path_recovery_view_tags_every_edge_with_its_own_provenance():
    path_recovery = PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[_established_path(), _partial_path()])
    view = build_recovery_view(path_recovery, TestRecoveryResult())["path_recovery"]
    assert view["paths"][0]["edges"][0]["provenance"] == "ai_recovery"
    assert view["paths"][1]["unresolved_suffix"][0]["rejection_reason"] == "no dispatch evidence"


def test_build_path_recovery_view_reports_dropped_padding_as_absent():
    path = _established_path()
    view = build_recovery_view(PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[path]), TestRecoveryResult())["path_recovery"]
    assert view["paths"][0]["unresolved_suffix"] == []
    assert len(view["paths"][0]["nodes"]) == 2


def test_build_test_recovery_view_is_additive_never_drops_anything():
    target = _entity("handler", "a.ts")
    tests = [
        RecoveredTest(file="a.spec.ts", test="t1", covers="c1", target=target, status=TEST_STATUS_ACCEPTED),
        RecoveredTest(file="a.spec.ts", test="t2", covers="c2", target=target, status=TEST_STATUS_REJECTED),
    ]
    view = build_recovery_view(PathRecoveryResult(), TestRecoveryResult(status=STATUS_ESTABLISHED, tests=tests))["test_recovery"]
    assert len(view["tests"]) == 2


def test_view_reports_path_and_test_recovery_as_independent_sections():
    """A path-recovery-only view (no tests) and a test-recovery-only view
    (no paths) must each be complete and legible on their own -- this is
    the structural guarantee behind 'test recovery should be usable
    earlier than path recovery'."""
    target = _entity("handler", "a.ts")
    test_only = build_recovery_view(
        PathRecoveryResult(), TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[
            RecoveredTest(file="a.spec.ts", test="t1", covers="c1", target=target, status=TEST_STATUS_ACCEPTED)
        ]),
    )
    assert test_only["path_recovery"]["status"] == STATUS_UNRESOLVED
    assert test_only["path_recovery"]["paths"] == []
    assert test_only["test_recovery"]["status"] == STATUS_ESTABLISHED

    path_only = build_recovery_view(PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[_established_path()]), TestRecoveryResult())
    assert path_only["test_recovery"]["status"] == STATUS_UNRESOLVED
    assert path_only["test_recovery"]["tests"] == []
    assert path_only["path_recovery"]["status"] == STATUS_ESTABLISHED


def test_summarize_for_notes_reports_both_facts_independently():
    target = _entity("handler", "a.ts")
    note = summarize_for_notes(
        PathRecoveryResult(status=STATUS_UNRESOLVED),
        TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[
            RecoveredTest(file="a.spec.ts", test="t1", covers="c1", target=target, status=TEST_STATUS_ACCEPTED)
        ]),
    )
    assert "could not establish" in note.lower()
    assert "recovered 1 verified test" in note.lower()


def test_summarize_for_notes_established_path_never_claims_structural_provenance():
    note = summarize_for_notes(PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[_established_path()]), TestRecoveryResult())
    assert "ai_recovery" in note
    assert "experimental" in note.lower()


def test_summarize_for_notes_distinguishes_partial_from_established():
    note = summarize_for_notes(PathRecoveryResult(status=STATUS_PARTIAL, paths=[_partial_path()]), TestRecoveryResult())
    assert "partial" in note.lower()


def test_build_path_recovery_view_includes_root_boundary_status():
    view = build_recovery_view(PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[_established_path()]), TestRecoveryResult())
    assert view["path_recovery"]["paths"][0]["root_boundary_status"] == "verified_boundary"


def test_summarize_for_notes_warns_when_an_established_path_has_an_unverified_root():
    """An established chain to a root `sydes.recovery.graph_path`'s
    topology fallback merely suggested (never one already known) must
    never be reported the same way as reaching a real entrypoint -- the
    note has to say so explicitly, not silently fold it into 'established
    N path(s)'."""
    path = _established_path().model_copy(update={"root_boundary_status": ROOT_CANDIDATE_BOUNDARY})
    note = summarize_for_notes(PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[path]), TestRecoveryResult())
    assert "established 1 path" in note
    assert "UNVERIFIED" in note
    assert path.entrypoint in note


def test_summarize_for_notes_established_verified_path_carries_no_unverified_warning():
    note = summarize_for_notes(PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[_established_path()]), TestRecoveryResult())
    assert "UNVERIFIED" not in note
