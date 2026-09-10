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


def _has_only_inferred_impacts(result: ChangeVerificationResult) -> bool:
    if not result.accepted_impacts:
        return False
    return all(impact.status != IMPACT_STATUS_PROVEN for impact in result.accepted_impacts)


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
    """
    if result.summary.verdict in (VERDICT_OK, VERDICT_VERIFIED) and _has_established_flow(result):
        return None

    gap_kinds: list[str] = []
    reasons: list[str] = []
    extra_tests: tuple[str, ...] = ()

    has_signal = _has_any_real_impact_signal(result)

    if has_signal and not _has_established_flow(result):
        if _boundary_without_matching_flow(result):
            gap_kinds.append(GAP_BOUNDARY_WITHOUT_FLOW)
            reasons.append(
                "a structural boundary was found but no complete route/entrypoint flow reaches it"
            )
        elif _has_only_inferred_impacts(result):
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

    missing_test_candidates = _missing_test_mapping_despite_new_tests(result)
    if missing_test_candidates:
        gap_kinds.append(GAP_MISSING_TEST_MAPPING)
        reasons.append(
            "no relevant test was mapped even though this diff changed a test file "
            f"({', '.join(missing_test_candidates)})"
        )
        extra_tests = missing_test_candidates

    if result.unresolved_changed_symbols > 0 and has_signal and not _has_established_flow(result):
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
