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
    """Render one obligation and the evidence behind its status."""
    mark = _MARK.get(obligation.status, "?")
    flag = "  [CHANGED BY THIS DIFF]" if obligation.introduced_by_change else ""
    lines.append(f"  {mark} {_LABEL.get(obligation.status, obligation.status.upper())}  {obligation.statement}{flag}")
    lines.append(f"        kind={obligation.kind}  origin={obligation.origin}")

    executions = {item.test_id: item for item in obligation.executions}
    for test in obligation.mapped_tests:
        execution = executions.get(test.id)
        detail = ""
        if execution and execution.duration_ms is not None:
            detail = f"  {execution.duration_ms / 1000:.1f}s"
        state = _LABEL.get(execution.status, "UNKNOWN") if execution else "UNKNOWN"
        lines.append(f"        {state}  {test.name}{detail}")
        lines.append(f"              why: {test.match_rule}  [{test.evidence_tier}]")
        if execution and execution.failure_summary:
            lines.append(f"              {execution.failure_summary}")
        if execution and execution.status == VERIFICATION_UNKNOWN and execution.reason:
            lines.append(f"              {execution.reason}")
            if execution.blocking_runtime_dependency_ids:
                names = ", ".join(
                    item.split(":")[-1] for item in execution.blocking_runtime_dependency_ids
                )
                lines.append(f"              runtime dependency: {names}")
        if execution and execution.command:
            lines.append(f"              $ {execution.command_text}")

    if not obligation.mapped_tests:
        lines.append(f"        {obligation.reason or 'No verifying test located'}")
        if obligation.supporting_tests:
            lines.append(
                f"        {len(obligation.supporting_tests)} test(s) exercise this flow "
                "but do not assert this behavior:"
            )
            for test in obligation.supporting_tests[: 5 if verbose else 2]:
                lines.append(f"              - {test.name}  ({test.match_rule})")
    if verbose and obligation.source_refs:
        lines.append(f"        refs: {', '.join(obligation.source_refs[:4])}")
    lines.append("")


def _render_flow(flow: AffectedFlow, lines: list[str], *, verbose: bool) -> None:
    """Render one affected flow, its topology summary, and its obligations."""
    lines.append(f"{_MARK.get(flow.status, '?')} {flow.entry_label}   [{flow.status.upper()}]")
    if flow.handler:
        lines.append(f"    handler: {flow.handler}  ({flow.artifact_refs.get('handler_file') or flow.artifact_refs.get('route_file')})")
    if flow.sinks:
        for sink in flow.sinks[:6]:
            name = sink.get("name") or sink.get("kind")
            lines.append(f"    → {sink.get('kind')}: {name}")
    if flow.analysis_status != ANALYSIS_COMPLETE:
        lines.append(f"    analysis: {flow.analysis_status.upper()} — downstream effects may be missing")
        for note in flow.analysis_notes[: 4 if verbose else 2]:
            lines.append(f"      - {note}")
    lines.append("")
    for obligation in flow.obligations:
        _render_obligation(obligation, lines, verbose=verbose)


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
    lines.append(f"Changed files:        {counts.changed_files}")
    lines.append(f"Changed symbols:      {counts.changed_symbols}")
    lines.append(f"Affected flows:       {counts.affected_flows}")
    lines.append("")
    lines.append(f"Obligations:          {counts.obligations}")
    lines.append(f"  Passed:             {counts.obligations_passed}")
    lines.append(f"  Failed:             {counts.obligations_failed}")
    lines.append(f"  Unverified:         {counts.obligations_unverified}")
    lines.append(f"  Unknown:            {counts.obligations_unknown}")
    lines.append(f"  Introduced by diff: {counts.obligations_introduced_by_change}")
    lines.append("")
    lines.append(f"Mapped tests:         {counts.mapped_tests}")
    lines.append(f"Tests executed:       {counts.tests_executed}")
    lines.append(f"Runtime dependencies: {counts.runtime_dependencies}")
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
            f"(+{changed_file.added_lines}/-{changed_file.removed_lines}, {changed_file.role or 'unknown'})"
        )
    for symbol in result.change.symbols:
        lines.append(
            f"    {symbol.qualified_name or symbol.name}  ({symbol.kind})  {symbol.file}:{symbol.start_line}"
        )

    _section(lines, "VERIFICATION")
    lines.append("  (obligations are demonstrated by executing the tests mapped to them)")
    lines.append("")
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
