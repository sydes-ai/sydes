"""Assembling what the recovery agent is shown before its first turn.

Everything here is read from the already-computed `ChangeVerificationResult`
and the diff it carries — no new graph query, no new source read. The
agent's own tool loop (see `sydes.recovery.tools`/`agent`) is what does
unrestricted repository investigation; this module only frames the
starting question.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sydes.recovery.trigger import RecoveryTrigger
from sydes.verify.models import ChangeVerificationResult


@dataclass(frozen=True)
class RecoveryContext:
    """Plain-text fields ready to drop into the recovery prompt. Kept as
    strings/tuples rather than passing the pydantic result through
    directly, so the prompt-building code in `agent.py` has no reason to
    reach back into `ChangeVerificationResult` internals itself."""

    reason_first_pass_stopped: str
    gap_kinds: tuple[str, ...]
    diff_summary: str
    changed_symbols: tuple[str, ...]
    current_result_summary: str
    cbm_fragments: str
    known_entrypoints: tuple[str, ...]
    test_candidates: tuple[str, ...]
    unresolved_gaps: tuple[str, ...]
    #: The exact repo-relative file paths this diff actually changed, taken
    #: directly from `ChangeVerificationResult.change.symbols`/`.files` —
    #: never a guess. This is the ground truth
    #: `sydes.recovery.verify`'s identity layer checks a path's
    #: `target_node` against: an entity whose resolved file is not in this
    #: set cannot be the actually-changed behavior, no matter how
    #: plausible its name looks.
    changed_files: tuple[str, ...] = ()


def _diff_summary(result: ChangeVerificationResult) -> str:
    lines: list[str] = []
    for f in result.change.files:
        hunks = ", ".join(f"L{h.start_line}-{h.end_line}" for h in f.hunks) or "(no hunk detail)"
        lines.append(f"- {f.change_type} {f.path} [{f.role or 'unknown'}] hunks: {hunks}")
    return "\n".join(lines) if lines else "(no changed files recorded)"


def _changed_symbols(result: ChangeVerificationResult) -> tuple[str, ...]:
    out: list[str] = []
    for symbol in result.change.symbols:
        qname = symbol.qualified_name or symbol.name
        out.append(f"{symbol.name} ({qname}) in {symbol.file}:{symbol.start_line or '?'}")
    return tuple(out)


def _current_result_summary(result: ChangeVerificationResult) -> str:
    counts = result.summary.counts
    return (
        f"verdict={result.summary.verdict} risk={result.summary.risk} "
        f"analysis_status={result.analysis_status} "
        f"affected_flows={counts.affected_flows} "
        f"mapped_tests={counts.mapped_tests} tests_executed={counts.tests_executed} "
        f"unresolved_changed_symbols={result.unresolved_changed_symbols}"
    )


def _cbm_fragments(result: ChangeVerificationResult) -> str:
    """Everything the first pass already found, compactly — the agent's
    starting hypothesis to verify, extend, or correct, never a boundary on
    what it may go look at."""
    lines: list[str] = []
    for flow in result.affected_flows:
        lines.append(
            f"AffectedFlow: {flow.entry_label} handler={flow.handler} "
            f"impact_status={flow.impact_status} analysis_status={flow.analysis_status} "
            f"changed_nodes={[n.symbol for n in flow.changed_nodes]}"
        )
    for boundary in result.affected_boundaries:
        lines.append(
            f"AffectedBoundary: kind={boundary.kind} symbol={boundary.symbol} "
            f"status={boundary.status} label={boundary.label!r} file={boundary.file} "
            f"distance={boundary.distance}"
        )
    for impact in result.accepted_impacts:
        lines.append(
            f"AcceptedImpact: {impact.label!r} status={impact.status} "
            f"route={impact.route_method or ''} {impact.route_path or ''} "
            f"changed_symbols={impact.changed_symbols}"
        )
    return "\n".join(lines) if lines else "(first pass found no flows, boundaries, or accepted impacts)"


def _known_entrypoints(result: ChangeVerificationResult) -> tuple[str, ...]:
    out: set[str] = set()
    for flow in result.affected_flows:
        out.add(flow.entry_label)
    for impact in result.accepted_impacts:
        if impact.route_method and impact.route_path:
            out.add(f"{impact.route_method} {impact.route_path}")
    return tuple(sorted(out))


def _changed_files(result: ChangeVerificationResult) -> tuple[str, ...]:
    out: set[str] = set()
    for symbol in result.change.symbols:
        if symbol.file:
            out.add(symbol.file)
    for f in result.change.files:
        if f.path and f.role != "test_usage_candidate":
            out.add(f.path)
    return tuple(sorted(out))


def build_context(result: ChangeVerificationResult, trigger: RecoveryTrigger) -> RecoveryContext:
    return RecoveryContext(
        reason_first_pass_stopped=trigger.reason,
        gap_kinds=trigger.gap_kinds,
        diff_summary=_diff_summary(result),
        changed_symbols=_changed_symbols(result),
        current_result_summary=_current_result_summary(result),
        cbm_fragments=_cbm_fragments(result),
        known_entrypoints=_known_entrypoints(result),
        test_candidates=trigger.extra_test_candidate_files,
        unresolved_gaps=tuple(result.analysis_notes),
        changed_files=_changed_files(result),
    )
