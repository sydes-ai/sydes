"""`sydes.recovery.merge` — read-only, additive views. Nothing here ever
takes a `ChangeVerificationResult` as input, which is itself the structural
guarantee that this prototype cannot mutate the canonical result or the
CBM graph: there is no code path in this module capable of doing so."""

from __future__ import annotations

from sydes.recovery.merge import build_recovery_view, summarize_for_notes
from sydes.recovery.schema import (
    RecoveredEvidence,
    RecoveredPath,
    RecoveredTest,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_UNRESOLVED,
)


def test_build_recovery_view_tags_every_path_with_its_own_provenance():
    established = RecoveredPath(
        entrypoint="GET /users", status=STATUS_ESTABLISHED, provenance="ai_recovery",
        evidence=[RecoveredEvidence(file="a.ts", fact="registers the route")],
    )
    unresolved = RecoveredPath(entrypoint="POST /orders", status=STATUS_UNRESOLVED, provenance="ai_recovery_exhausted")
    recovery = RecoveryResult(status=STATUS_ESTABLISHED, recovered_paths=[established, unresolved])

    view = build_recovery_view(recovery)

    assert view["recovered_paths"][0]["provenance"] == "ai_recovery"
    assert view["recovered_paths"][1]["provenance"] == "ai_recovery_exhausted"
    # Never silently "structural" -- both are always one of the two AI
    # provenance values, never absent and never a bare "established".
    assert all(p["provenance"].startswith("ai_recovery") for p in view["recovered_paths"])


def test_build_recovery_view_is_additive_recovered_tests_never_drop_anything():
    tests = [
        RecoveredTest(file="a.spec.ts", test="rejects too-large limit", covers="limit validation"),
        RecoveredTest(file="a.spec.ts", test="allows a normal limit", covers="limit validation happy path"),
    ]
    recovery = RecoveryResult(status=STATUS_UNRESOLVED, recovered_tests=tests)
    view = build_recovery_view(recovery)
    assert len(view["recovered_tests"]) == 2


def test_summarize_for_notes_never_claims_structural_provenance():
    established = RecoveredPath(
        entrypoint="GET /users", status=STATUS_ESTABLISHED, provenance="ai_recovery",
        evidence=[RecoveredEvidence(file="a.ts", fact="registers the route")],
    )
    recovery = RecoveryResult(status=STATUS_ESTABLISHED, recovered_paths=[established])
    note = summarize_for_notes(recovery)
    assert "ai_recovery" in note
    assert "experimental" in note.lower()
    assert "structural" not in note.lower() or "not merged into" in note.lower()


def test_summarize_for_notes_on_fully_unresolved_recovery_is_honest():
    recovery = RecoveryResult(
        status=STATUS_UNRESOLVED,
        unresolved=[{"question": "does anything call this?", "missing_evidence": "no registration found"}],
    )
    note = summarize_for_notes(recovery)
    assert "could not establish" in note.lower()
