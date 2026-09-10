"""Deciding whether a first-pass result has a gap worth AI recovery.

Every condition here reads only the generic, already-canonical
`ChangeVerificationResult` fields (verdict, counts, accepted impacts,
affected flows/boundaries, analysis notes, changed-file roles) — never a
framework, library, or language name. This module has no idea what any
particular web framework, dispatch library, or message-bus pattern looks
like, and it must stay that way: the whole point of this prototype is
testing whether an LLM can recover a missing relationship from repository
evidence, not teaching Sydes new framework rules through the back door of a
"trigger condition."

Only a genuinely high-value gap triggers recovery — a minor note or a
verdict that is already clean must not. See `evaluate_trigger` for the
exact bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sydes.impact.models import IMPACT_STATUS_PROVEN
from sydes.ingest.file_roles import FILE_ROLE_TEST_USAGE_CANDIDATE
from sydes.verify.models import (
    CHANGE_ADDED,
    ChangeVerificationResult,
    VERDICT_OK,
    VERDICT_VERIFIED,
)

#: Sydes' own diagnostic vocabulary (not a framework's) — one of several
#: signals a route/flow could not be resolved, never required on its own.
_ROUTE_COMPOSITION_UNRESOLVED_NOTE = "route composition is unresolved"

GAP_NO_ESTABLISHED_FLOW = "no_established_flow"
GAP_BOUNDARY_WITHOUT_FLOW = "boundary_without_flow"
GAP_ONLY_INFERRED_IMPACT = "only_inferred_impact"
GAP_ROUTE_COMPOSITION_UNRESOLVED = "route_composition_unresolved"
GAP_MISSING_TEST_MAPPING = "missing_test_mapping_despite_new_tests"
GAP_UNRESOLVED_CHANGED_SYMBOLS = "unresolved_changed_symbols"


@dataclass(frozen=True)
class RecoveryTrigger:
    """Why recovery is worth attempting for this result, and what to tell
    the agent it's trying to fix — `reason` feeds directly into the recovery
    prompt (see `sydes.recovery.context`)."""

    gap_kinds: tuple[str, ...]
    reason: str
    extra_test_candidate_files: tuple[str, ...] = field(default_factory=tuple)


def _has_established_flow(result: ChangeVerificationResult) -> bool:
    return any(flow.impact_status == IMPACT_STATUS_PROVEN for flow in result.affected_flows)


def _has_any_real_impact_signal(result: ChangeVerificationResult) -> bool:
    return bool(result.affected_flows or result.affected_boundaries or result.accepted_impacts)


def _has_unresolved_inferred_impact(result: ChangeVerificationResult) -> bool:
    """True iff at least one accepted impact is not (yet) structurally
    proven -- regardless of whether OTHER impacts on this same result ARE
    proven. An established flow elsewhere on the same PR must never mask
    a genuinely separate, still-inferred impact (see task item 3: "a PR
    with one established flow can still have another unresolved path")."""
    return any(impact.status != IMPACT_STATUS_PROVEN for impact in result.accepted_impacts)


def _boundary_without_matching_flow(result: ChangeVerificationResult) -> bool:
    return bool(result.affected_boundaries) and not result.affected_flows


def _route_composition_unresolved(result: ChangeVerificationResult) -> bool:
    return any(_ROUTE_COMPOSITION_UNRESOLVED_NOTE in note.lower() for note in result.analysis_notes)


def _new_or_changed_test_files(result: ChangeVerificationResult) -> tuple[str, ...]:
    return tuple(
        f.path for f in result.change.files
        if f.role == FILE_ROLE_TEST_USAGE_CANDIDATE
    )


def _missing_test_mapping_despite_new_tests(result: ChangeVerificationResult) -> tuple[str, ...]:
    """Non-empty iff at least one test file changed in this diff (added is
    the strongest signal, but a modified existing test file counts too) and
    yet nothing was mapped at all. Returns the candidate file paths so the
    caller can hand them to the agent as a concrete lead, not just a flag."""
    if result.summary.counts.mapped_tests > 0:
        return ()
    test_files = _new_or_changed_test_files(result)
    added_test_files = tuple(
        f.path for f in result.change.files
        if f.role == FILE_ROLE_TEST_USAGE_CANDIDATE and f.change_type == CHANGE_ADDED
    )
    return added_test_files or test_files


def evaluate_trigger(result: ChangeVerificationResult) -> RecoveryTrigger | None:
    """Return a `RecoveryTrigger` iff a high-value gap exists, else `None`.

    Deliberately does NOT trigger merely because `analysis_notes` is
    non-empty, or merely because `unresolved_changed_symbols > 0` on its
    own with no other signal, or when the verdict is already clean
    (`VERIFIED`/`OK` with an established flow) — a minor note must not fire
    recovery. Each condition composes with "there is something real to
    recover" (either a real impact signal already found, or a concretely
    named new/changed test file with zero mapping) so the agent is never
    sent to investigate a change that plausibly has nothing to find.

    An established flow existing somewhere on the result does NOT, by
    itself, suppress every other check below: a PR can have one flow fully
    established and a genuinely separate impact/changed-symbol/test-mapping
    gap alongside it, and that gap is just as real as it would be with no
    established flow at all (task item: "one established flow does not
    prevent recovery of unresolved flow B"). Only the top-level VERIFIED/OK
    short-circuit and the `_boundary_without_matching_flow`/
    `GAP_NO_ESTABLISHED_FLOW` checks below are inherently "zero flows"
    conditions — every other gap kind is evaluated regardless of whether
    something else on the same result is already established, and the
    downstream canonical merge (`sydes.recovery.canonical_merge`) is what
    already guarantees recovering one gap never duplicates or downgrades
    evidence recovery didn't touch.
    """
    if result.summary.verdict in (VERDICT_OK, VERDICT_VERIFIED) and _has_established_flow(result):
        return None

    gap_kinds: list[str] = []
    reasons: list[str] = []
    extra_tests: tuple[str, ...] = ()

    has_signal = _has_any_real_impact_signal(result)
    has_established = _has_established_flow(result)

    if has_signal and not has_established:
        if _boundary_without_matching_flow(result):
            gap_kinds.append(GAP_BOUNDARY_WITHOUT_FLOW)
            reasons.append(
                "a structural boundary was found but no complete route/entrypoint flow reaches it"
            )
        elif _has_unresolved_inferred_impact(result):
            gap_kinds.append(GAP_ONLY_INFERRED_IMPACT)
            reasons.append(
                "only inferred impact exists; no impact was structurally established with a complete path"
            )
        else:
            gap_kinds.append(GAP_NO_ESTABLISHED_FLOW)
            reasons.append("changed behavior has no established entrypoint path")

        if _route_composition_unresolved(result):
            gap_kinds.append(GAP_ROUTE_COMPOSITION_UNRESOLVED)
            reasons.append("the first pass reported route composition as unresolved in this repository")

    elif has_established and _has_unresolved_inferred_impact(result):
        # One or more flows ARE established, but a genuinely separate
        # accepted impact on this same result remains only AI-inferred —
        # an established flow elsewhere must not mask that remaining gap.
        gap_kinds.append(GAP_ONLY_INFERRED_IMPACT)
        reasons.append(
            "an established flow already exists, but a separate accepted impact remains "
            "only AI-inferred, not structurally proven"
        )

    missing_test_candidates = _missing_test_mapping_despite_new_tests(result)
    if missing_test_candidates:
        gap_kinds.append(GAP_MISSING_TEST_MAPPING)
        reasons.append(
            "no relevant test was mapped even though this diff changed a test file "
            f"({', '.join(missing_test_candidates)})"
        )
        extra_tests = missing_test_candidates

    # Deliberately NOT gated on `not has_established`: a changed symbol the
    # first pass never connected to any entrypoint is exactly as real a
    # gap when some OTHER flow is already established as when nothing is.
    if result.unresolved_changed_symbols > 0 and has_signal:
        gap_kinds.append(GAP_UNRESOLVED_CHANGED_SYMBOLS)
        reasons.append(
            f"{result.unresolved_changed_symbols} changed symbol(s) reach no entrypoint the first pass found"
        )

    if not gap_kinds:
        return None

    return RecoveryTrigger(
        gap_kinds=tuple(gap_kinds),
        reason="; ".join(reasons),
        extra_test_candidate_files=extra_tests,
    )
