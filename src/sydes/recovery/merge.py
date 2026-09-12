"""Read-only, additive views of a `RecoveryOutcome` — never a write path.

Nothing here mutates `ChangeVerificationResult` or the CBM graph. This
prototype keeps recovered edges/results run-local: `build_recovery_view`
produces a plain dict a caller (the evaluation harness, or eventually a
renderer) can inspect or print, with every recovered path/edge/test tagged
by its own provenance — never presented as though CBM itself established
it. Path recovery and test recovery are built as two independent sections,
matching `sydes.recovery.schema.PathRecoveryResult`/`TestRecoveryResult` —
a caller that only wants one is never forced to interpret the other's
absence as a failure of the one it asked for. A real merge into the
canonical result — if this prototype proves out — is deliberately out of
scope here; see the module-level "experiment first, integration second"
framing in `sydes.recovery`.
"""

from __future__ import annotations

from typing import Any

from sydes.recovery.schema import (
    PathRecoveryResult, ROOT_VERIFIED_BOUNDARY, STATUS_ESTABLISHED, STATUS_PARTIAL, TestRecoveryResult,
)


def _entity_view(entity) -> dict[str, Any]:
    return {"symbol": entity.symbol, "file": entity.file, "qualified_name": entity.qualified_name}


def build_path_recovery_view(path_recovery: PathRecoveryResult) -> dict[str, Any]:
    """A plain, JSON-ready dict summarizing path recovery. Each path
    already carries only its verified, shortest-sufficient nodes/edges
    (padding beyond `target_node` was dropped by `sydes.recovery.verify`
    before this ever sees it) plus `unresolved_suffix` for the edges that
    would have been needed but were not proven."""
    return {
        "status": path_recovery.status,
        "paths": [
            {
                "entrypoint": path.entrypoint,
                "target_node": path.target_node,
                "status": path.status,
                #: See `sydes.recovery.schema.ROOT_VERIFIED_BOUNDARY`/
                #: `ROOT_CANDIDATE_BOUNDARY` -- a `status == "established"`
                #: chain whose root is only `candidate_boundary` is a real,
                #: edge-proven chain to an UNVERIFIED root, never
                #: equivalent to reaching an already-known entrypoint. A
                #: consumer of this view must read both fields together,
                #: not `status` alone, to know whether this represents a
                #: fully verified end-to-end system flow.
                "root_boundary_status": path.root_boundary_status,
                "nodes": [_entity_view(n) for n in path.nodes],
                "edges": [
                    {
                        "from": _entity_view(e.from_entity),
                        "to": _entity_view(e.to_entity),
                        "relationship": e.relationship,
                        "evidence": [ev.model_dump() for ev in e.evidence],
                        "status": e.status,
                        "provenance": e.provenance,
                        "rejection_reason": e.rejection_reason,
                        "from_decomposition": e.from_decomposition,
                    }
                    for e in path.edges
                ],
                "unresolved_suffix": [
                    {
                        "from": _entity_view(e.from_entity),
                        "to": _entity_view(e.to_entity),
                        "relationship": e.relationship,
                        "rejection_reason": e.rejection_reason,
                    }
                    for e in path.unresolved_suffix
                ],
            }
            for path in path_recovery.paths
        ],
        "corrected_first_pass_claims": [c.model_dump() for c in path_recovery.corrected_first_pass_claims],
        "unresolved": [u.model_dump() for u in path_recovery.unresolved],
    }


def build_test_recovery_view(test_recovery: TestRecoveryResult) -> dict[str, Any]:
    return {
        "status": test_recovery.status,
        "tests": [
            {
                "file": t.file, "test": t.test, "covers": t.covers,
                "target": _entity_view(t.target),
                "evidence": [ev.model_dump() for ev in t.evidence],
                "status": t.status, "rejection_reason": t.rejection_reason,
            }
            for t in test_recovery.tests
        ],
    }


def build_recovery_view(path_recovery: PathRecoveryResult, test_recovery: TestRecoveryResult) -> dict[str, Any]:
    return {
        "path_recovery": build_path_recovery_view(path_recovery),
        "test_recovery": build_test_recovery_view(test_recovery),
    }


def summarize_for_notes(path_recovery: PathRecoveryResult, test_recovery: TestRecoveryResult) -> str:
    """One line suitable for `ChangeVerificationResult.notes` — the only
    touch point this prototype has with the canonical result today (see
    `sydes.cli.verify_change`'s AI-recovery integration, on by default).
    Never claims
    structural provenance for what recovery found; always reports path and
    test recovery as two separate facts, since one may have succeeded
    while the other did not."""
    established_paths = [p for p in path_recovery.paths if p.status == STATUS_ESTABLISHED]
    partial_paths = [p for p in path_recovery.paths if p.status == STATUS_PARTIAL]
    accepted_tests = [t for t in test_recovery.tests if t.status == "accepted"]
    # An established chain to a root CBM's graph shape only SUGGESTED (see
    # `sydes.recovery.graph_path`'s topology fallback), never one already
    # known, is a distinct, weaker claim than reaching a real entrypoint --
    # this note must say so explicitly rather than reporting both the same
    # way under one "established" word.
    unverified_root_paths = [p for p in established_paths if p.root_boundary_status != ROOT_VERIFIED_BOUNDARY]

    if established_paths:
        path_note = f"established {len(established_paths)} path(s) not found by the first pass: " + ", ".join(
            p.entrypoint for p in established_paths
        )
        if unverified_root_paths:
            path_note += (
                f" (WARNING: {len(unverified_root_paths)} of these reach a root CBM's graph shape only "
                "suggested -- an UNVERIFIED system boundary, not confirmed equivalent to a known entrypoint: "
                + ", ".join(p.entrypoint for p in unverified_root_paths) + ")"
            )
    elif partial_paths:
        path_note = "found a partial, edge-verified prefix but could not prove the full path to the changed behavior"
    else:
        path_note = "could not establish a source-backed path beyond the first pass"

    if accepted_tests:
        test_note = f"recovered {len(accepted_tests)} verified test(s)"
    else:
        test_note = "recovered no verified tests"

    return (
        f"AI recovery (experimental, provenance=ai_recovery, run-local, not merged into structural results): "
        f"path recovery {path_note}; test recovery {test_note}."
    )
