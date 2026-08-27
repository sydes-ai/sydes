"""Terminal rendering for `sydes verify-change`.

A renderer over `ChangeVerificationResult` and nothing more: it reads no
terminal state and holds no analysis logic, so the same model can be rendered
for GitHub or a UI later.
"""

from __future__ import annotations

import re

from sydes.verify.models import (
    ANALYSIS_COMPLETE,
    OBLIGATION_CROSS_REPO_CALL,
    OBLIGATION_EVENT_EMISSION,
    OBLIGATION_ROUTE_CONTRACT,
    OBLIGATION_SIDE_EFFECT,
    OBLIGATION_STATE_CONSISTENCY,
    OBLIGATION_VALIDATION,
    ORIGIN_TRACE_SINK,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    AcceptedImpact,
    AffectedBoundary,
    AffectedFlow,
    ChangeSemanticAnalysis,
    ChangeVerificationResult,
    VerificationObligation,
)

#: `AcceptedImpact.status` values, duplicated here rather than imported from
#: `sydes.impact` — the renderer depends only on `sydes.verify.models`, the
#: same boundary every other section already respects.
_IMPACT_PROVEN = "proven"
_IMPACT_INFERRED = "inferred"

#: `sydes.ingest.file_roles.FILE_ROLE_TEST_USAGE_CANDIDATE`, duplicated here
#: for the same reason as `_IMPACT_PROVEN` above — this renderer depends
#: only on `sydes.verify.models`.
_FILE_ROLE_TEST_USAGE_CANDIDATE = "test_usage_candidate"

_MARK = {
    VERIFICATION_PASSED: "✓",
    VERIFICATION_FAILED: "✗",
    VERIFICATION_UNVERIFIED: "?",
    VERIFICATION_UNKNOWN: "?",
}

_LABEL = {
    VERIFICATION_PASSED: "PASS",
    VERIFICATION_FAILED: "FAIL",
    VERIFICATION_UNVERIFIED: "UNVERIFIED",
    VERIFICATION_UNKNOWN: "UNKNOWN",
}


def _section(lines: list[str], title: str) -> None:
    """Append a section header."""
    lines.append("")
    lines.append(title)
    lines.append("")


def _render_obligation(
    obligation: VerificationObligation, lines: list[str], *, verbose: bool
) -> None:
    """Render one obligation compactly: claim, status, and its strongest evidence."""
    mark = _MARK.get(obligation.status, "?")
    label = _LABEL.get(obligation.status, obligation.status.upper())
    lines.append(f"  {mark} {label}  {obligation.statement}")

    # At most one line of evidence: the reader wants to know whether the claim
    # is demonstrated, not to read the whole test inventory.
    if obligation.mapped_tests:
        primary = obligation.mapped_tests[0]
        extra = len(obligation.mapped_tests) - 1
        suffix = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"        evidence: {primary.name}{suffix}")
        lines.append(f"        why: {primary.match_rule}  [{primary.evidence_tier}]")
    else:
        lines.append(f"        {obligation.reason or 'No verifying test located'}")
    if obligation.status in {VERIFICATION_FAILED, VERIFICATION_UNKNOWN} and obligation.reason:
        lines.append(f"        {obligation.reason}")
    if obligation.supporting_tests:
        lines.append(
            f"        regression context: {len(obligation.supporting_tests)} test(s) "
            "exercise this flow without asserting this behavior"
        )
    if verbose and obligation.source_refs:
        lines.append(f"        refs: {', '.join(obligation.source_refs[:3])}")


def _render_flow(flow: AffectedFlow, lines: list[str], *, verbose: bool) -> None:
    """Render one affected flow: change-critical obligations first, then the rest."""
    impact_tag = "  [AI-INFERRED IMPACT]" if flow.impact_status == _IMPACT_INFERRED else ""
    lines.append(f"{_MARK.get(flow.status, '?')} {flow.entry_label}   [{flow.status.upper()}]{impact_tag}")
    if flow.impact_status == _IMPACT_INFERRED:
        lines.append(
            "    this flow was proposed by AI semantic inference — see AFFECTED BEHAVIOR "
            "above for the model's confidence and reasoning; verification below is real "
            "evidence, never inferred"
        )
    if flow.handler:
        location = flow.artifact_refs.get("handler_file") or flow.artifact_refs.get("route_file")
        lines.append(f"    handler: {flow.handler}  ({location})")
    if flow.analysis_status != ANALYSIS_COMPLETE:
        lines.append(
            f"    analysis: {flow.analysis_status.upper()} — downstream effects may be missing"
        )
        for note in flow.analysis_notes[: 3 if verbose else 1]:
            lines.append(f"      - {note}")

    required = [item for item in flow.obligations if item.required]
    advisory = [item for item in flow.obligations if not item.required]
    downstream = [item for item in advisory if item.origin == ORIGIN_TRACE_SINK]
    advisory = [item for item in advisory if item.origin != ORIGIN_TRACE_SINK]
    critical = [item for item in required if item.introduced_by_change]
    other = [item for item in required if not item.introduced_by_change]

    if critical:
        lines.append("")
        lines.append("  CHANGE-CRITICAL")
        for obligation in critical:
            _render_obligation(obligation, lines, verbose=verbose)
    if other:
        unresolved = [item for item in other if item.status != VERIFICATION_PASSED]
        passed = len(other) - len(unresolved)
        lines.append("")
        lines.append("  OTHER OBLIGATIONS ON THIS FLOW")
        if passed:
            lines.append(f"  ✓ PASS  {passed} further obligation(s) covered by existing tests")
        for obligation in unresolved[: None if verbose else 4]:
            _render_obligation(obligation, lines, verbose=verbose)
        hidden = len(unresolved) - (4 if not verbose else len(unresolved))
        if hidden > 0:
            lines.append(f"        … {hidden} more unresolved (use --verbose)")
    if downstream:
        # Blast-radius context: effects this flow reaches that the change does
        # not modify. Shown because they matter to a reviewer, not verified.
        by_ref = {
            f"sink:{sink.get('id') or sink.get('name')}": sink for sink in flow.sinks
        }
        lines.append("")
        lines.append("  RELATED DOWNSTREAM EFFECTS  (context; not required by this change)")
        for obligation in downstream:
            sink = next(
                (by_ref[ref] for ref in obligation.source_refs if ref in by_ref), None
            )
            if sink is None:
                lines.append(f"    • {obligation.statement}")
                continue
            kind = " ".join(
                token for token in (sink.get("kind"), sink.get("operation")) if token
            )
            location = f"  ({sink.get('file')})" if sink.get("file") else ""
            lines.append(f"    • {kind}: {sink.get('name')}{location}")

    if advisory:
        lines.append("")
        lines.append("  ADDITIONAL TEST SUGGESTIONS")
        lines.append(
            f"  {len(advisory)} suggestion(s) from the existing Sydes test matrix "
            "(advisory; excluded from the verdict)"
        )
        if verbose:
            for obligation in advisory:
                lines.append(f"    - {obligation.statement}")
    lines.append("")


def _render_ci_suite(suite, lines: list[str], *, verbose: bool) -> None:
    """Render the repository's own test run as the regression baseline."""
    if suite is None:
        lines.append("  Not executed.")
        return
    mark = _MARK.get(suite.status, "?")
    counts = ""
    if suite.tests_passed is not None:
        counts = f"{suite.tests_passed} passed"
        if suite.tests_failed:
            counts += f", {suite.tests_failed} failed"
    duration = f"  {suite.duration_ms / 1000:.1f}s" if suite.duration_ms is not None else ""
    lines.append(f"  {mark} {_LABEL.get(suite.status, suite.status.upper())}  {counts}{duration}")
    if suite.command:
        lines.append(f"        $ {suite.command_text}")
    if suite.source:
        lines.append(f"        from {suite.source}")
    if suite.reason:
        lines.append(f"        {suite.reason}")
    for node in suite.failed_test_ids[: 10 if verbose else 3]:
        lines.append(f"        failed: {node}")
    if suite.status == VERIFICATION_PASSED:
        lines.append(
            "        A passing suite is regression evidence; it does not demonstrate "
            "the changed behavior."
        )


def _render_accepted_impact(impact: AcceptedImpact, lines: list[str], *, verbose: bool) -> None:
    """Render one canonical impact — PROVEN compactly, INFERRED with its
    reasoning, since an inferred entry is exactly the one a reviewer cannot
    already see for themselves in the diff."""
    label = impact.label
    if impact.status == _IMPACT_INFERRED:
        lines.append(f"  {label}")
        if impact.llm_confidence is not None:
            lines.append(f"      LLM confidence: {impact.llm_confidence:.2f}  (model self-assessment, not a calibrated probability)")
        if impact.llm_reason:
            lines.append(f"      Why: {impact.llm_reason}")
        lines.append(
            "      Structural confirmation: "
            + ("matched a known entrypoint" if impact.corroborated else "unavailable")
        )
        if impact.llm_uncertainty:
            lines.append(f"      Uncertainty: {impact.llm_uncertainty}")
        if impact.verification_model_status != "modeled":
            lines.append(
                "      Verification: not yet modeled as a full flow — "
                "no obligations were generated for this specific impact"
            )
        if verbose and impact.changed_symbols:
            lines.append(f"      changed: {', '.join(impact.changed_symbols[:3])}")
    else:
        suffix = (
            "" if impact.verification_model_status == "modeled"
            else "   (not yet modeled as a full verification flow)"
        )
        lines.append(f"  {label}{suffix}")


#: Non-verbose caps. A real PR can carry hundreds of PROVEN structural
#: impacts (every symbol reachable from the change, most of them never
#: reaching a full verification flow) — dumping all of them makes a review
#: unreadable and drowns out the handful that actually matter. INFERRED
#: impacts are never capped: see the loop below.
_MODELED_SAMPLE_CAP = 20
_STRUCTURAL_LABEL_SAMPLE_CAP = 8


def _group_by_label(impacts: list[AcceptedImpact]) -> list[tuple[str, int]]:
    """Collapse repeated low-information labels (`GET /` a hundred times,
    say) into one row with a count — canonical identity (each `AcceptedImpact`
    keeps its own `id`) is untouched; this only affects how the default
    human report presents them, never the structured/JSON output."""
    counts: dict[str, int] = {}
    for item in impacts:
        counts[item.label] = counts.get(item.label, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))


def _render_affected_behavior(result: ChangeVerificationResult, lines: list[str], *, verbose: bool) -> None:
    """The canonical "what does Sydes believe is affected" view — the same
    `accepted_impacts` list obligations/counts are derived from, so nothing
    shown elsewhere can disagree with this section.

    Default (non-verbose) prioritizes what a reviewer actually needs:
    modeled/verification-relevant PROVEN impacts first, a summarized count
    for the remaining structural-only PROVEN impacts (with low-information
    duplicate labels collapsed), and every INFERRED impact in full — its
    rationale is exactly the thing a reviewer has no other way to see.
    `--verbose` (or the JSON artifact) still exposes the complete PROVEN list,
    nothing is ever dropped from `accepted_impacts` itself.
    """
    impacts = result.accepted_impacts
    proven = [item for item in impacts if item.status == _IMPACT_PROVEN]
    inferred = [item for item in impacts if item.status == _IMPACT_INFERRED]
    lines.append(f"Proven impacts: {len(proven)}")
    lines.append(f"Inferred impacts: {len(inferred)}")
    if not impacts:
        lines.append("")
        lines.append("  No affected behavior identified.")
        return

    if proven:
        lines.append("")
        if verbose:
            lines.append("PROVEN")
            for item in proven:
                _render_accepted_impact(item, lines, verbose=verbose)
        else:
            modeled = [item for item in proven if item.verification_model_status == "modeled"]
            structural = [item for item in proven if item.verification_model_status != "modeled"]
            if modeled:
                lines.append("Modeled / verification-relevant impacts:")
                for item in modeled[:_MODELED_SAMPLE_CAP]:
                    _render_accepted_impact(item, lines, verbose=verbose)
                hidden = len(modeled) - _MODELED_SAMPLE_CAP
                if hidden > 0:
                    lines.append(f"  … {hidden} more (use --verbose)")
            if structural:
                if modeled:
                    lines.append("")
                lines.append("Additional structural impacts:")
                lines.append(
                    f"  {len(structural)} more impact(s) were identified but are not yet "
                    "modeled as full verification flows."
                )
                grouped = _group_by_label(structural)
                for label, count in grouped[:_STRUCTURAL_LABEL_SAMPLE_CAP]:
                    suffix = f"  (×{count})" if count > 1 else ""
                    lines.append(f"    {label}{suffix}")
                if len(grouped) > _STRUCTURAL_LABEL_SAMPLE_CAP:
                    lines.append(f"    … {len(grouped) - _STRUCTURAL_LABEL_SAMPLE_CAP} more distinct label(s)")
                lines.append("  Use --verbose or structured JSON for the complete list.")
    if inferred:
        lines.append("")
        lines.append("INFERRED")
        for item in inferred:
            # Never truncated, even without --verbose: an inferred impact is
            # exactly the thing a reviewer has no other way to see, and
            # hiding it behind a flag would recreate the silent-disappearance
            # problem this section exists to close.
            _render_accepted_impact(item, lines, verbose=verbose)
    lines.append("")


# ==========================================================================
# Default (concise) report — what a developer opening a PR actually reads.
#
# The detailed sections above (`_render_affected_behavior`, `_render_flow`,
# etc.) remain exactly as they were, feeding `_render_verbose_report` below
# for `--verbose`. Everything from here down is presentation only: it reads
# the same canonical `ChangeVerificationResult` fields — `accepted_impacts`,
# `affected_flows`, `summary.counts`, `ci_suite`, `runtime_dependencies` —
# and never recomputes or reinterprets any of them. No internal field name
# (`accepted_impacts`, `verification_model_status`, `mapped_tests`, ...) is
# ever printed here; those stay in --verbose/JSON.
# ==========================================================================

_MISSING_DEPENDENCY_RE = re.compile(r"requires `([^`]+)`")
#: Approximately top few, per the product brief — not a new ranking
#: algorithm, just how many of the *existing* confidence-ordered list to
#: show before pointing at --verbose for the rest.
_INFERRED_DEFAULT_CAP = 3


def _underline(title: str) -> str:
    return "─" * len(title)


def _header(lines: list[str], title: str) -> None:
    """A concise-report section header: blank line, title, underline —
    content follows immediately, no extra blank line before it (matches the
    target report style, distinct from `_section`'s verbose-report spacing)."""
    lines.append("")
    lines.append(title)
    lines.append(_underline(title))


def _changed_symbol_identities(result: ChangeVerificationResult) -> set[str]:
    """Every changed symbol's short and qualified name — the same identity
    a propagation step's own `symbol` field already carries, used only to
    tag a hop `[changed]`, never to add or infer a new one."""
    identities: set[str] = set()
    for symbol in result.change.symbols:
        if symbol.qualified_name:
            identities.add(symbol.qualified_name)
        identities.add(symbol.name)
    return identities


#: Roughly 5, per the product brief — not a new ranking system, just how
#: many of the *already-filtered* relevant production symbols to show
#: before pointing at --verbose for the rest.
_CHANGED_DEFAULT_CAP = 5


def _flow_participant_identities(result: ChangeVerificationResult) -> set[str]:
    """Every symbol name that actually appears as a hop in a *rendered*
    flow's own trace — the same identity set `_flow_chain_lines` already
    computes per flow, pooled across all shown flows. A changed symbol
    matching one of these genuinely participates in a shown system-impact
    path (Task inclusion criterion 1)."""
    identities: set[str] = set()
    for flow in result.affected_flows:
        for step in flow.steps:
            if not isinstance(step, dict):
                continue
            symbol = step.get("symbol") or step.get("name")
            if symbol:
                identities.add(str(symbol))
    return identities


def _accepted_impact_identities(result: ChangeVerificationResult) -> set[str]:
    """Every symbol name any accepted impact already attributes itself to
    (`AcceptedImpact.changed_symbols`) — a changed symbol matching one of
    these produced or IS an accepted affected behavior/entrypoint (Task
    inclusion criterion 2), independent of whether that impact became a
    rendered flow."""
    identities: set[str] = set()
    for impact in result.accepted_impacts:
        identities.update(impact.changed_symbols)
    return identities


def _is_relevant_changed_symbol(name: str, short: str, relevant_identities: set[str]) -> bool:
    return name in relevant_identities or short in relevant_identities


def _render_changed_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """Concrete file:line + function for changed *production* symbols that
    are relevant to what the report actually shows — never the raw
    `changed symbols: N` debug count, and never every changed test function
    (already visible in GitHub's own Files Changed view).

    Relevance (Task inclusion criteria 1-2, using only fields the pipeline
    already computed): a changed production symbol qualifies when it
    participates in a rendered flow's own trace, or when it is among the
    changed symbols an accepted impact already attributes itself to. No new
    ranking or relevance score is introduced — a symbol either matches one
    of these two existing sets or it does not.
    """
    _header(lines, "Changed")
    if not result.change.symbols:
        if not result.change.files:
            lines.append(f"No changes against {result.change.base}.")
            return
        for changed_file in result.change.files:
            lines.append(changed_file.path)
        return

    test_files = {
        item.path for item in result.change.files
        if item.role == _FILE_ROLE_TEST_USAGE_CANDIDATE
    }
    production_symbols = [item for item in result.change.symbols if item.file not in test_files]
    if not production_symbols:
        lines.append("Only test files changed — see --verbose for the full list.")
        return

    relevant_identities = _flow_participant_identities(result) | _accepted_impact_identities(result)
    relevant = [
        item for item in production_symbols
        if _is_relevant_changed_symbol(
            item.qualified_name or item.name, item.name, relevant_identities
        )
    ]
    # No shown flow/impact matched any changed symbol by name (e.g. nothing
    # resolved at all) — fall back to the plain production-symbol list
    # rather than rendering an empty "Changed" section.
    shown_pool = relevant if relevant else production_symbols

    seen: set[tuple[str, int | None, str]] = set()
    deduped: list = []
    for symbol in shown_pool:
        name = symbol.qualified_name or symbol.name
        key = (symbol.file, symbol.start_line, name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(symbol)

    for symbol in deduped[:_CHANGED_DEFAULT_CAP]:
        name = symbol.qualified_name or symbol.name
        location = f"{symbol.file}:{symbol.start_line}" if symbol.start_line else symbol.file
        lines.append(f"{location} — {name}")

    hidden = len(deduped) - _CHANGED_DEFAULT_CAP
    if hidden > 0:
        noun = "symbol" if hidden == 1 else "symbols"
        lines.append(f"+{hidden} more relevant changed {noun} — use --verbose")


def _flow_chain_lines(flow: AffectedFlow, changed_identities: set[str]) -> list[str]:
    """One line per distinct hop in the flow's own recorded trace, in the
    order Sydes already produced it — the endpoint itself is skipped (the
    flow header already names it), consecutive steps attributed to the same
    symbol (multiple statements inside one function) collapse to that one
    hop, and every deduped downstream sink is appended as a final leaf.
    Nothing here reorders, drops, or invents an edge Sydes did not already
    record; a long real trace stays exactly as long as it is.
    """
    out: list[str] = []
    last_symbol: str | None = None
    for step in flow.steps:
        if not isinstance(step, dict) or step.get("kind") == "endpoint":
            continue
        symbol = step.get("symbol") or step.get("name")
        if not symbol or symbol == last_symbol:
            continue
        last_symbol = symbol
        short = str(symbol).rsplit(".", 1)[-1]
        tag = " [changed]" if symbol in changed_identities or short in changed_identities else ""
        out.append(f"  → {short}{tag}")
    for sink in flow.sinks:
        if not isinstance(sink, dict):
            continue
        label = " ".join(token for token in (sink.get("kind"), sink.get("operation")) if token)
        label = label or str(sink.get("name") or "")
        if label:
            out.append(f"  → {label}")
    return out


#: Roughly a handful, matching `_CHANGED_DEFAULT_CAP`'s own reasoning — the
#: semantic pass is already asked for short lists, this just caps the
#: concise report's rendering of them too.
_CHANGE_ANALYSIS_DEFAULT_CAP = 5


def _render_change_analysis_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """The semantic perspective — hypotheses from one bounded PR-level LLM
    read of the change, kept visibly distinct from "System impact" (which is
    structural/evidence-backed). Nothing here was ever proven or verified;
    see `ChangeSemanticAnalysis`. Omitted entirely when the pass did not run
    or produced nothing — never rendered as an empty section, and never
    confused with "no impact" (see `ANALYSIS COMPLETENESS`/diagnostics for
    why it may be absent)."""
    analysis = result.pr_semantic_analysis
    if analysis is None:
        return
    if not (
        analysis.change_summary or analysis.behavior_changes
        or analysis.investigation_hints or analysis.uncertainties
    ):
        return
    _header(lines, "CHANGE ANALYSIS")
    if analysis.change_summary:
        lines.append(analysis.change_summary)
    if analysis.behavior_changes:
        lines.append("")
        lines.append("Likely behavioral changes")
        for item in analysis.behavior_changes[:_CHANGE_ANALYSIS_DEFAULT_CAP]:
            lines.append(f"  • {item.description}")
    if analysis.investigation_hints:
        lines.append("")
        lines.append("Investigate")
        for item in analysis.investigation_hints[:_CHANGE_ANALYSIS_DEFAULT_CAP]:
            lines.append(f"  • {item.description}")
    if analysis.uncertainties:
        lines.append("")
        lines.append("Uncertain")
        for item in analysis.uncertainties[:_CHANGE_ANALYSIS_DEFAULT_CAP]:
            lines.append(f"  • {item}")


def _render_change_analysis_verbose(analysis: ChangeSemanticAnalysis, lines: list[str]) -> None:
    """Fuller detail than the concise report's `CHANGE ANALYSIS` section:
    evidence/confidence per behavior change, important symbols, investigation
    hints with their concepts/boundary-type hints, and local risks — still
    entirely `origin=ORIGIN_LLM_HYPOTHESIS`, never evidence."""
    if analysis.change_summary:
        lines.append(f"  {analysis.change_summary}")
        lines.append("")
    for item in analysis.behavior_changes:
        confidence = f"{item.confidence:.2f}" if item.confidence is not None else "?.??"
        lines.append(f"  [{confidence}] {item.description}")
        if item.changed_symbols:
            lines.append(f"      symbols: {', '.join(item.changed_symbols)}")
        for evidence in item.evidence:
            lines.append(f"      evidence: {evidence}")
    if analysis.important_symbols:
        lines.append("")
        lines.append("  Important symbols:")
        for item in analysis.important_symbols:
            location = item.file or ""
            if item.symbol:
                location = f"{location}::{item.symbol}" if location else item.symbol
            lines.append(f"    {location}  — {item.reason}")
    if analysis.investigation_hints:
        lines.append("")
        lines.append("  Investigation hints:")
        for item in analysis.investigation_hints:
            lines.append(f"    {item.description}")
            if item.related_symbols:
                lines.append(f"        related: {', '.join(item.related_symbols)}")
            if item.concepts:
                lines.append(f"        concepts: {', '.join(item.concepts)}")
            if item.likely_boundary_types:
                lines.append(f"        likely boundary types: {', '.join(item.likely_boundary_types)}")
    if analysis.likely_boundary_types:
        lines.append("")
        lines.append(f"  Likely boundary types (overall): {', '.join(analysis.likely_boundary_types)}")
    if analysis.local_risks:
        lines.append("")
        lines.append("  Local risks:")
        for risk in analysis.local_risks:
            lines.append(f"    - {risk}")
    if analysis.uncertainties:
        lines.append("")
        lines.append("  Uncertain:")
        for item in analysis.uncertainties:
            lines.append(f"    - {item}")


#: Roughly a handful — matches `_CHANGED_DEFAULT_CAP`'s own reasoning.
_BOUNDARY_DEFAULT_CAP = 5


def _non_http_boundaries(result: ChangeVerificationResult) -> list[AffectedBoundary]:
    """Boundaries not already covered by an HTTP `AffectedFlow` above them —
    an `api`/`http` boundary is the same real route an `AffectedFlow` already
    renders, so it is excluded here to avoid showing it twice."""
    return [
        item for item in result.affected_boundaries
        if not (item.kind == "api" and item.subtype == "http")
    ]


def _render_system_impact_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """The core section: how the change propagates. HTTP flows use the
    existing structural trace/sink rendering (`_flow_chain_lines`);
    non-HTTP boundaries (callable/async/other api) — Increment C — render as
    a compact `kind · label` line with a one-line evidence trail, never
    forced into the HTTP flow shape."""
    _header(lines, "System impact")
    changed_identities = _changed_symbol_identities(result)
    rendered_anything = False
    for index, flow in enumerate(result.affected_flows):
        if index > 0:
            lines.append("")
        lines.append(flow.entry_label)
        lines.extend(_flow_chain_lines(flow, changed_identities))
        rendered_anything = True
    boundaries = _non_http_boundaries(result)[:_BOUNDARY_DEFAULT_CAP]
    for boundary in boundaries:
        if rendered_anything:
            lines.append("")
        lines.append(f"{boundary.kind} · {boundary.label}")
        for evidence_line in boundary.evidence[:1]:
            lines.append(f"    {evidence_line}")
        rendered_anything = True
    if not rendered_anything:
        lines.append("No structural propagation path was established.")


def _render_inferred_impact_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """LLM-derived findings, kept visibly distinct from structural System
    impact — confidence, the causal "Why", and whether structural
    corroboration exists. Ranked by the confidence Sydes already attached
    to each candidate (no new scoring); only the top few by default, with
    the rest pointed at --verbose, never dropped."""
    inferred = [item for item in result.accepted_impacts if item.status == _IMPACT_INFERRED]
    if not inferred:
        return
    ranked = sorted(inferred, key=lambda item: (-(item.llm_confidence or 0.0), item.label))
    _header(lines, "Inferred impact")
    shown = ranked[:_INFERRED_DEFAULT_CAP]
    for index, item in enumerate(shown):
        if index > 0:
            lines.append("")
        confidence = f"{item.llm_confidence:.2f}" if item.llm_confidence is not None else "?.??"
        lines.append(f"INFERRED · {confidence}")
        lines.append(item.label)
        if item.llm_reason:
            lines.append("")
            lines.append("Why:")
            lines.append(item.llm_reason)
        lines.append("")
        lines.append("Static confirmation: " + ("available" if item.corroborated else "unavailable"))
    hidden = len(ranked) - len(shown)
    if hidden > 0:
        lines.append("")
        noun = "impact" if hidden == 1 else "impacts"
        lines.append(f"… {hidden} more inferred {noun} — use --verbose for the full list.")


def _required_obligations_default(result: ChangeVerificationResult) -> list[VerificationObligation]:
    """Every required obligation across every affected flow, in the
    existing flow/obligation order — advisory test-matrix suggestions and
    downstream-sink context are verbose-only, matching the product
    boundary: this section describes evidence, not obligation machinery."""
    return [
        obligation
        for flow in result.affected_flows
        for obligation in flow.obligations
        if obligation.required
    ]


#: A statement past this length, or containing an implementation-expression
#: character, is treated as "implementation-shaped" rather than a readable
#: behavior claim — e.g. a raw sink snippet like
#: `entity_subquery = ( db_session.query( func.jsonb_build_array(...`.
_STATEMENT_LENGTH_THRESHOLD = 80
_IMPLEMENTATION_HEAVY_CHARS = ("(", ")", "{", "}", "=")

_RESPONDS_RE = re.compile(r"^(?P<route>.+?) responds (?P<code>\d{3})(?:\s*—.*)?$")

#: Short behavior phrases keyed by the obligation's own existing `kind`
#: (see `sydes.verify.models` `OBLIGATION_*`) — reusing that already-defined
#: vocabulary, not a new taxonomy. Used both to build a specific label
#: ("get_signal_stats query behavior") when a downstream identity is
#: available, and, unqualified ("Changed query behavior"), as the last-resort
#: fallback when it is not.
_KIND_PHRASES = {
    OBLIGATION_ROUTE_CONTRACT: "response behavior",
    OBLIGATION_VALIDATION: "validation behavior",
    OBLIGATION_SIDE_EFFECT: "downstream behavior",
    OBLIGATION_STATE_CONSISTENCY: "query behavior",
    OBLIGATION_EVENT_EMISSION: "event-emission behavior",
    OBLIGATION_CROSS_REPO_CALL: "cross-service call behavior",
}


def _evidence_symbol(obligation: VerificationObligation) -> str:
    """The first downstream function/service symbol the obligation's own
    evidence already names — the same identity `map_tests_to_obligation`
    and every `obligations.py` builder already attaches, never re-derived."""
    for item in obligation.evidence:
        if item.symbol:
            return item.symbol
    return ""


def _sink_for_obligation(flow: AffectedFlow | None, obligation: VerificationObligation) -> dict | None:
    """The flow sink this obligation's `source_refs` already points at
    (`"sink:<id-or-name>"`, set by `_trace_obligations`), if any — the same
    sink `_flow_chain_lines` already renders in System impact."""
    if flow is None:
        return None
    refs = {ref.removeprefix("sink:") for ref in obligation.source_refs if ref.startswith("sink:")}
    if not refs:
        return None
    for sink in flow.sinks:
        if isinstance(sink, dict) and str(sink.get("id") or sink.get("name") or "") in refs:
            return sink
    return None


def _specific_downstream_label(flow: AffectedFlow | None, obligation: VerificationObligation) -> str | None:
    """The most specific downstream identity already present in the flow's
    own data, in priority order: (1) the symbol the obligation's evidence or
    matched sink already names, optionally sharpened with the sink's own
    kind/operation; (2) the flow's own handler; (3) the flow's route/entry
    label. Returns `None` — never a guess — when nothing usable exists.
    """
    phrase = _KIND_PHRASES.get(obligation.kind)
    sink = _sink_for_obligation(flow, obligation)
    sink_phrase = ""
    sink_symbol = ""
    if sink is not None:
        sink_phrase = " ".join(token for token in (sink.get("kind"), sink.get("operation")) if token)
        sink_symbol = str(sink.get("symbol") or "")
    symbol = _evidence_symbol(obligation) or sink_symbol

    if symbol and sink_phrase:
        return f"{sink_phrase} performed by {symbol}"
    if symbol:
        return f"{symbol} {phrase}" if phrase else f"{symbol} behavior"
    if sink_phrase:
        return sink_phrase

    if flow is not None and flow.handler:
        handler = flow.handler.rsplit(".", 1)[-1]
        return f"{handler} {phrase}" if phrase else f"{handler} behavior"

    if flow is not None and flow.entry_label and phrase:
        return f"{flow.entry_label} {phrase}"

    return None


def _concise_obligation_label(
    obligation: VerificationObligation, flow: AffectedFlow | None = None
) -> str:
    """The default-report label for one obligation — the underlying
    `statement`/JSON is never modified, this only decides what this one
    renderer prints. A short, already-readable statement passes through
    unchanged; an obvious "responds NNN — description" response-skeleton
    statement shortens to "<route> returns NNN"; anything else long or
    implementation-expression-heavy prefers the most specific downstream
    identity already present in the flow/obligation data (see
    `_specific_downstream_label`), falling back to a generic label derived
    from the obligation's own `kind` only when no such identity exists.
    """
    statement = obligation.statement
    match = _RESPONDS_RE.match(statement)
    if match:
        return f"{match.group('route')} returns {match.group('code')}"
    implementation_heavy = (
        len(statement) > _STATEMENT_LENGTH_THRESHOLD
        or any(char in statement for char in _IMPLEMENTATION_HEAVY_CHARS)
    )
    if not implementation_heavy:
        return statement
    specific = _specific_downstream_label(flow, obligation)
    if specific:
        return specific
    phrase = _KIND_PHRASES.get(obligation.kind, "behavior")
    return f"Changed {phrase}"


def _render_verification_evidence_default(
    obligations: list[VerificationObligation], flow_by_id: dict[str, AffectedFlow], lines: list[str]
) -> None:
    """Evidence, in plain language — never `mapped_tests`/`supporting_tests`
    by name. Reuses each obligation's existing status/reason/supporting-test
    presence verbatim; nothing here reinterprets a supporting test as proof."""
    _header(lines, "Verification evidence")
    for index, obligation in enumerate(obligations):
        if index > 0:
            lines.append("")
        label = _concise_obligation_label(obligation, flow_by_id.get(obligation.flow_id))
        if obligation.status == VERIFICATION_PASSED:
            lines.append(f"✓ {label}")
        elif obligation.status == VERIFICATION_FAILED:
            lines.append(f"✗ {label}")
            if obligation.reason:
                lines.append(f"  {obligation.reason}")
        elif obligation.status == VERIFICATION_UNKNOWN:
            lines.append(f"? {label}")
            lines.append(f"  {obligation.reason or 'Sydes could not determine whether this holds.'}")
        else:  # VERIFICATION_UNVERIFIED
            lines.append(f"? {label}")
            supporting = len(obligation.supporting_tests)
            if supporting:
                noun = "test" if supporting == 1 else "tests"
                lines.append(f"  Sydes found {supporting} existing {noun} that exercise this flow,")
                lines.append("  but none explicitly establish this behavior.")
            else:
                lines.append("  No existing verification evidence establishes this behavior.")


def _render_ci_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """The repository's own regression suite, compactly — command/working
    directory/discovery source are verbose-only."""
    suite = result.ci_suite
    _header(lines, "CI")
    if suite is None:
        lines.append("Not executed.")
        return
    if suite.status == VERIFICATION_PASSED:
        text = f"✓ {suite.tests_passed or 0} tests passed"
        if suite.tests_failed:
            text += f", {suite.tests_failed} failed"
        lines.append(text)
    elif suite.status == VERIFICATION_FAILED:
        failed = suite.tests_failed if suite.tests_failed is not None else "some"
        lines.append(f"✗ {failed} tests failed · {suite.tests_passed or 0} passed")
    else:
        lines.append(f"? {suite.command_text or 'the test suite'} could not run")
        match = _MISSING_DEPENDENCY_RE.search(suite.reason or "")
        if match:
            lines.append(f"  missing dependency: {match.group(1)}")
        elif suite.reason:
            lines.append(f"  {suite.reason}")


def _could_not_establish_bullets(
    result: ChangeVerificationResult, obligations: list[VerificationObligation]
) -> list[str]:
    """Every bullet here states a boundary of what existing evidence
    establishes — never an instruction for what the repository owner should
    do about it — and every one is read straight off a count/field Sydes
    already computed (`summary.counts`, `accepted_impacts`, `ci_suite`)."""
    counts = result.summary.counts
    bullets: list[str] = []

    not_established = len(obligations) - sum(
        1 for item in obligations if item.status == VERIFICATION_PASSED
    )
    if not_established > 0:
        noun = "behavior is" if not_established == 1 else "behaviors are"
        bullets.append(f"{not_established} affected {noun} not established by existing tests.")

    if counts.impacts_not_modeled > 0:
        noun = "area is" if counts.impacts_not_modeled == 1 else "areas are"
        bullets.append(
            f"{counts.impacts_not_modeled} additional affected {noun} not yet verification-modeled."
        )

    if counts.unresolved_changed_symbols > 0:
        noun = "symbol has" if counts.unresolved_changed_symbols == 1 else "symbols have"
        bullets.append(f"{counts.unresolved_changed_symbols} changed {noun} unresolved impact paths.")

    uncorroborated = sum(
        1 for item in result.accepted_impacts
        if item.status == _IMPACT_INFERRED and not item.corroborated
    )
    if uncorroborated == 1:
        bullets.append("An inferred impact could not be structurally confirmed.")
    elif uncorroborated > 1:
        bullets.append(f"{uncorroborated} inferred impacts could not be structurally confirmed.")

    suite = result.ci_suite
    if suite is not None and suite.status == VERIFICATION_UNKNOWN:
        match = _MISSING_DEPENDENCY_RE.search(suite.reason or "")
        if match:
            bullets.append(f"The repository test suite could not run because `{match.group(1)}` is unavailable.")
        elif suite.reason:
            bullets.append(f"The repository test suite could not run ({suite.reason}).")

    return bullets


def _render_could_not_establish_default(
    result: ChangeVerificationResult, obligations: list[VerificationObligation], lines: list[str]
) -> None:
    bullets = _could_not_establish_bullets(result, obligations)
    if not bullets:
        return
    _header(lines, "What Sydes could not establish")
    for bullet in bullets:
        lines.append(f"• {bullet}")


def _render_runtime_requirements_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """Runtime dependencies Sydes already discovered — never provisioned,
    mocked, or invented. Omitted entirely when there is nothing to say,
    rather than printing an empty section."""
    if not result.runtime_dependencies:
        return
    names: list[str] = []
    seen: set[str] = set()
    for dependency in result.runtime_dependencies:
        if dependency.name in seen:
            continue
        seen.add(dependency.name)
        names.append(dependency.name)
    if not names:
        return
    _header(lines, "Full verification requires")
    lines.append(" · ".join(names))


def _render_headline_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    counts = result.summary.counts
    risk = result.summary.risk.upper()
    if counts.affected_flows > 0:
        noun = "AFFECTED FLOW" if counts.affected_flows == 1 else "AFFECTED FLOWS"
        lines.append(f"{risk} RISK · {counts.affected_flows} {noun}")
    elif result.accepted_impacts:
        total = len(result.accepted_impacts)
        noun = "AFFECTED BEHAVIOR" if total == 1 else "AFFECTED BEHAVIORS"
        lines.append(f"{risk} RISK · {total} {noun}")
    else:
        lines.append(f"{risk} RISK")


def _render_default_report(result: ChangeVerificationResult) -> str:
    """The concise report a developer opening a PR reads in ~20 seconds:
    what changed, how it propagates, what evidence covers it, what remains
    unestablished, and the (unchanged, conservative) verdict. Every section
    reads straight off the canonical `ChangeVerificationResult` — nothing
    here recomputes analysis, obligations, or the verdict; see
    `_render_verbose_report` for the full detail this necessarily omits.
    """
    lines: list[str] = ["SYDES VERIFICATION", ""]
    _render_headline_default(result, lines)

    _render_changed_default(result, lines)
    _render_change_analysis_default(result, lines)
    _render_system_impact_default(result, lines)
    _render_inferred_impact_default(result, lines)

    obligations = _required_obligations_default(result)
    if obligations:
        flow_by_id = {flow.id: flow for flow in result.affected_flows}
        _render_verification_evidence_default(obligations, flow_by_id, lines)

    _render_ci_default(result, lines)
    _render_could_not_establish_default(result, obligations, lines)
    _render_runtime_requirements_default(result, lines)

    _header(lines, "Verdict")
    lines.append(result.summary.verdict)

    return "\n".join(lines).rstrip() + "\n"


def _render_verbose_report(result: ChangeVerificationResult) -> str:
    """The full, inspectable terminal report — every changed symbol, every
    structural/inferred impact, every obligation, all diagnostics. This is
    the pre-existing detailed view, kept intact for `--verbose` and never
    shown by default: see `_render_default_report` for the concise report a
    reviewer actually reads first."""
    verbose = True
    lines: list[str] = []
    counts = result.summary.counts

    lines.append("SYDES CHANGE VERIFICATION")
    lines.append("")
    lines.append(f"Repo:    {result.change.repos[0].name if result.change.repos else '-'}")
    lines.append(f"Base:    {result.change.base}")
    if result.change.includes_working_tree:
        lines.append("Includes uncommitted working-tree changes: yes")
    lines.append("")
    lines.append(f"Risk:     {result.summary.risk}")
    lines.append(f"Verdict:  {result.summary.verdict}")
    lines.append(f"Analysis: {result.analysis_status.upper()}")
    if result.summary.headline:
        lines.append("")
        lines.append(result.summary.headline)
    lines.append("")
    lines.append(
        f"Changed: {counts.changed_files} file(s), {counts.changed_symbols} symbol(s)"
        f"   Flows: {counts.affected_flows}"
    )
    lines.append(
        f"Obligations: {counts.obligations}"
        f"   passed {counts.obligations_passed}"
        f" · failed {counts.obligations_failed}"
        f" · unverified {counts.obligations_unverified}"
        f" · unknown {counts.obligations_unknown}"
        f"   ({counts.obligations_introduced_by_change} introduced by this change)"
    )
    if result.summary.risk_reasons:
        lines.append("")
        lines.append("Risk drivers:")
        for reason in result.summary.risk_reasons:
            lines.append(f"  - {reason}")

    _section(lines, "CHANGED SURFACE")
    if not result.change.files:
        lines.append(f"  No changes against {result.change.base}.")
    for changed_file in result.change.files:
        lines.append(
            f"  [{changed_file.change_type}] {changed_file.path}  "
            f"(+{changed_file.added_lines}/-{changed_file.removed_lines})"
        )
    for symbol in result.change.symbols:
        lines.append(
            f"    {symbol.qualified_name or symbol.name}  ({symbol.kind})  {symbol.file}:{symbol.start_line}"
        )

    if result.pr_semantic_analysis is not None:
        _section(lines, "CHANGE ANALYSIS (semantic hypothesis; not structural evidence)")
        _render_change_analysis_verbose(result.pr_semantic_analysis, lines)

    _section(lines, "AFFECTED BEHAVIOR")
    _render_affected_behavior(result, lines, verbose=verbose)

    if result.affected_boundaries:
        _section(lines, "BOUNDARIES (typed, transport-neutral; Increment C)")
        for boundary in result.affected_boundaries:
            subtype = f" ({boundary.subtype})" if boundary.subtype else ""
            lines.append(f"  {boundary.kind}{subtype} · {boundary.label}")
            lines.append(
                f"      distance={boundary.distance}  evidence={boundary.evidence_strength}"
                f"  status={boundary.status}"
            )
            for evidence_line in boundary.evidence:
                lines.append(f"      {evidence_line}")
            if boundary.changed_symbols:
                lines.append(f"      from: {', '.join(boundary.changed_symbols)}")

    _section(lines, "CI REGRESSION SUITE")
    _render_ci_suite(result.ci_suite, lines, verbose=verbose)

    _section(lines, "VERIFICATION")
    if not result.affected_flows:
        lines.append("  No affected flow resolved from the shared trace stack.")
    for flow in result.affected_flows:
        _render_flow(flow, lines, verbose=verbose)

    if result.code_findings:
        _section(lines, "CODE FINDINGS (advisory; excluded from the verdict)")
        for finding in result.code_findings:
            location = finding.file or ""
            if finding.line:
                location = f"{location}:{finding.line}"
            lines.append(f"  [{finding.severity}] {finding.title}")
            lines.append(f"        {location}")

    _section(lines, "RUNTIME REQUIREMENTS")
    lines.append("  (Sydes does not provision, mock, or contact these)")
    lines.append("")
    if not result.runtime_dependencies:
        lines.append("  None detected.")
    for dependency in result.runtime_dependencies:
        scope = "" if dependency.scope == "affected_flow" else "   (repository-wide)"
        lines.append(f"  {dependency.name}   ({dependency.kind}){scope}")
        for evidence in dependency.detected_from[: 3 if verbose else 1]:
            lines.append(f"      from {evidence.symbol or evidence.label}  ({evidence.file})")

    if result.analysis_notes:
        _section(lines, "ANALYSIS COMPLETENESS")
        for note in result.analysis_notes:
            lines.append(f"  - {note}")

    if result.notes:
        _section(lines, "NOTES")
        for note in result.notes:
            lines.append(f"  - {note}")

    if verbose and result.diagnostics:
        _section(lines, "DIAGNOSTICS")
        for note in result.diagnostics:
            lines.append(f"  - {note}")

    return "\n".join(lines).rstrip() + "\n"


def render_verify_change_terminal(
    result: ChangeVerificationResult, *, verbose: bool = False
) -> str:
    """Render a terminal report for a verification result.

    Default: the concise report a developer reads in ~20 seconds (see
    `_render_default_report`). `--verbose`: the full detailed report,
    unchanged from before this task (see `_render_verbose_report`) — every
    changed symbol, every structural/inferred impact, every obligation,
    advisory suggestions, and diagnostics.
    """
    if not verbose:
        return _render_default_report(result)
    return _render_verbose_report(result)
