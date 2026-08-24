"""Terminal rendering for `sydes verify-change`.

A renderer over `ChangeVerificationResult` and nothing more: it reads no
terminal state and holds no analysis logic, so the same model can be rendered
for GitHub or a UI later.
"""

from __future__ import annotations

from sydes.verify.models import (
    ANALYSIS_COMPLETE,
    ORIGIN_TRACE_SINK,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    AcceptedImpact,
    AffectedFlow,
    ChangeVerificationResult,
    VerificationObligation,
)

#: `AcceptedImpact.status` values, duplicated here rather than imported from
#: `sydes.impact` — the renderer depends only on `sydes.verify.models`, the
#: same boundary every other section already respects.
_IMPACT_PROVEN = "proven"
_IMPACT_INFERRED = "inferred"

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


def render_verify_change_terminal(
    result: ChangeVerificationResult, *, verbose: bool = False
) -> str:
    """Render a detailed, inspectable terminal report for a verification result."""
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

    _section(lines, "AFFECTED BEHAVIOR")
    _render_affected_behavior(result, lines, verbose=verbose)

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
