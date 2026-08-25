"""Terminal rendering for `sydes verify-change`.

A renderer over `ChangeVerificationResult` and nothing more: it reads no
terminal state and holds no analysis logic, so the same model can be rendered
for GitHub or a UI later.
"""

from __future__ import annotations

import re

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


def _render_changed_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """Concrete file:line + function — never the raw `changed symbols: N`
    debug count."""
    _header(lines, "Changed")
    if not result.change.symbols:
        if not result.change.files:
            lines.append(f"No changes against {result.change.base}.")
            return
        for changed_file in result.change.files:
            lines.append(changed_file.path)
        return
    seen: set[tuple[str, int | None, str]] = set()
    first = True
    for symbol in result.change.symbols:
        name = symbol.qualified_name or symbol.name
        key = (symbol.file, symbol.start_line, name)
        if key in seen:
            continue
        seen.add(key)
        if not first:
            lines.append("")
        first = False
        location = f"{symbol.file}:{symbol.start_line}" if symbol.start_line else symbol.file
        lines.append(location)
        lines.append(name)


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


def _render_system_impact_default(result: ChangeVerificationResult, lines: list[str]) -> None:
    """The core section: how the change propagates, using the structural
    trace/sink data Sydes already produced per flow — see `_flow_chain_lines`."""
    _header(lines, "System impact")
    if not result.affected_flows:
        lines.append("No structural propagation path was established.")
        return
    changed_identities = _changed_symbol_identities(result)
    for index, flow in enumerate(result.affected_flows):
        if index > 0:
            lines.append("")
        lines.append(flow.entry_label)
        lines.extend(_flow_chain_lines(flow, changed_identities))


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


def _render_verification_evidence_default(
    obligations: list[VerificationObligation], lines: list[str]
) -> None:
    """Evidence, in plain language — never `mapped_tests`/`supporting_tests`
    by name. Reuses each obligation's existing status/reason/supporting-test
    presence verbatim; nothing here reinterprets a supporting test as proof."""
    _header(lines, "Verification evidence")
    for index, obligation in enumerate(obligations):
        if index > 0:
            lines.append("")
        if obligation.status == VERIFICATION_PASSED:
            lines.append(f"✓ {obligation.statement}")
        elif obligation.status == VERIFICATION_FAILED:
            lines.append(f"✗ {obligation.statement}")
            if obligation.reason:
                lines.append(f"  {obligation.reason}")
        elif obligation.status == VERIFICATION_UNKNOWN:
            lines.append(f"? {obligation.statement}")
            lines.append(f"  {obligation.reason or 'Sydes could not determine whether this holds.'}")
        else:  # VERIFICATION_UNVERIFIED
            lines.append(f"? {obligation.statement}")
            if obligation.supporting_tests:
                lines.append("  Existing tests exercise this flow,")
                lines.append("  but Sydes found no evidence that they establish this behavior.")
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


def _render_coverage_default(obligations: list[VerificationObligation], lines: list[str]) -> None:
    established = sum(1 for item in obligations if item.status == VERIFICATION_PASSED)
    not_established = len(obligations) - established
    _header(lines, "Coverage")
    lines.append(f"{established} established · {not_established} not established")


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
    _render_system_impact_default(result, lines)
    _render_inferred_impact_default(result, lines)

    obligations = _required_obligations_default(result)
    if obligations:
        _render_verification_evidence_default(obligations, lines)

    _render_ci_default(result, lines)
    _render_could_not_establish_default(result, obligations, lines)
    _render_runtime_requirements_default(result, lines)
    if obligations:
        _render_coverage_default(obligations, lines)

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
