"""Terminal rendering for `sydes verify-change`.

A renderer over `ChangeVerificationResult` and nothing more: it reads no
terminal state and holds no analysis logic, so the same model can be rendered
for GitHub or a UI later.
"""

from __future__ import annotations

from sydes.verify.models import (
    ANALYSIS_COMPLETE,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    AffectedFlow,
    ChangeVerificationResult,
    VerificationObligation,
)

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
    lines.append(f"{_MARK.get(flow.status, '?')} {flow.entry_label}   [{flow.status.upper()}]")
    if flow.handler:
        location = flow.artifact_refs.get("handler_file") or flow.artifact_refs.get("route_file")
        lines.append(f"    handler: {flow.handler}  ({location})")
    if flow.sinks:
        summary = ", ".join(
            f"{sink.get('kind')}: {sink.get('name')}" for sink in flow.sinks[:3]
        )
        more = f" (+{len(flow.sinks) - 3} more)" if len(flow.sinks) > 3 else ""
        lines.append(f"    downstream: {summary}{more}")
    if flow.analysis_status != ANALYSIS_COMPLETE:
        lines.append(
            f"    analysis: {flow.analysis_status.upper()} — downstream effects may be missing"
        )
        for note in flow.analysis_notes[: 3 if verbose else 1]:
            lines.append(f"      - {note}")

    required = [item for item in flow.obligations if item.required]
    advisory = [item for item in flow.obligations if not item.required]
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
