"""Orchestration for `sydes verify-change`.

Pipeline:

    git diff -> changed files/symbols
              -> Sydes structural intelligence (symbols, calls, routes, events)
              -> affected system flows
              -> existing verification
              -> verification gaps
              -> runtime requirements

The analyzer produces a `ChangeVerificationResult` and nothing else. It depends
on no terminal state, so it runs identically in CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.llm.client import LLMClient, LLMClientError
from sydes.verify.cross_repo import detect_cross_repo_impacts
from sydes.verify.events import detect_event_signals
from sydes.verify.git_change import read_unified_diff, resolve_change_set
from sydes.verify.llm_findings import (
    build_change_context,
    generate_code_findings,
    generate_verification_gaps,
)
from sydes.verify.models import (
    CHANGE_ADDED,
    CHANGE_DELETED,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    VERDICT_ACTION_REQUIRED,
    VERDICT_OK,
    VERDICT_REVIEW,
    VERIFICATION_UNVERIFIED,
    VERIFICATION_VERIFIED,
    ChangedSymbol,
    ChangeSummary,
    ChangeVerificationResult,
    VerificationCounts,
)
from sydes.verify.repo_scan import scan_repository
from sydes.verify.runtime import infer_runtime_dependencies
from sydes.verify.surface import FlowBuilder, build_system_surface
from sydes.verify.symbol_index import build_symbol_index
from sydes.verify.test_index import build_test_index, map_existing_verification


@dataclass(slots=True)
class VerifyChangeOptions:
    """Runtime options for one verify-change analysis."""

    base: str = "main"
    include_working_tree: bool = True
    code_review: bool = True
    llm_policy: str = "auto"
    model_spec: str | None = None
    llm_client: LLMClient | None = None
    max_scan_files: int = 8_000
    diagnostics: list[str] = field(default_factory=list)


def _attribute_symbols(change, index) -> list[ChangedSymbol]:
    """Attribute diff hunks to indexed symbols, avoiding whole-file blast radius."""
    changed: dict[str, ChangedSymbol] = {}

    for changed_file in change.files:
        if changed_file.change_type == CHANGE_DELETED or changed_file.binary:
            continue

        file_symbols = index.symbols_in_file(changed_file.path)
        if not file_symbols:
            continue

        if changed_file.change_type == CHANGE_ADDED:
            targets = [(symbol, symbol.end_line - symbol.start_line + 1) for symbol in file_symbols]
        else:
            hits: dict[str, int] = {}
            for hunk in changed_file.hunks:
                for line in range(hunk.start_line, hunk.end_line + 1):
                    symbol = index.symbol_at(changed_file.path, line)
                    if symbol is None:
                        continue
                    hits[symbol.id] = hits.get(symbol.id, 0) + 1
            targets = [
                (index.symbols[symbol_id], count)
                for symbol_id, count in hits.items()
                if symbol_id in index.symbols
            ]

        for symbol, changed_lines in targets:
            if symbol.kind == "class" and any(
                other.class_name == symbol.name for other, _ in targets
            ):
                # Prefer the specific method over its enclosing class.
                continue
            record = changed.get(symbol.id)
            if record is None:
                changed[symbol.id] = ChangedSymbol(
                    id=symbol.id,
                    repo=symbol.repo,
                    file=symbol.file,
                    name=symbol.name,
                    qualified_name=symbol.qualified_name,
                    kind=symbol.kind,
                    language=symbol.language,
                    start_line=symbol.start_line,
                    end_line=symbol.end_line,
                    change_type=changed_file.change_type,
                    changed_lines=changed_lines,
                    decorators=list(symbol.decorators),
                )
            else:
                record.changed_lines += changed_lines
            changed_file.symbols.append(symbol.id)

    return sorted(changed.values(), key=lambda item: (item.file, item.start_line))


def _compute_summary(result: ChangeVerificationResult) -> ChangeSummary:
    """Derive risk and verdict from concrete counts and evidence states."""
    change = result.change
    source_files = [item for item in change.files if item.role == "source_route_candidate"]
    test_files = [item for item in change.files if item.role == "test_usage_candidate"]

    verified = [item for item in result.verification if item.status == VERIFICATION_VERIFIED]
    unverified = [item for item in result.verification if item.status == VERIFICATION_UNVERIFIED]

    counts = VerificationCounts(
        changed_files=len(change.files),
        changed_source_files=len(source_files),
        changed_test_files=len(test_files),
        changed_symbols=len(change.symbols),
        affected_flows=len(result.affected_flows),
        code_findings=len(result.code_findings),
        existing_verification=len(verified),
        verification_gaps=len(result.verification_gaps),
        runtime_dependencies=len(result.runtime_dependencies),
        cross_repo_impacts=len(result.cross_repo_impacts),
    )

    reasons: list[str] = []
    severe_findings = [item for item in result.code_findings if item.severity in {"P0", "P1"}]
    if severe_findings:
        reasons.append(f"{len(severe_findings)} P0/P1 code finding(s)")

    has_event_or_boundary = any(
        node.kind in {"event", "consumer", "client"}
        for flow in result.affected_flows
        for node in flow.nodes
    )
    if unverified:
        reasons.append(f"{len(unverified)} affected flow(s) with no located verification")
    if result.verification_gaps:
        reasons.append(f"{len(result.verification_gaps)} verification gap(s)")
    if has_event_or_boundary:
        reasons.append("change reaches an event or outbound service boundary")
    if result.cross_repo_impacts:
        reasons.append(f"{len(result.cross_repo_impacts)} cross-repo impact(s)")

    risk = RISK_LOW
    if severe_findings or (unverified and has_event_or_boundary) or len(result.verification_gaps) >= 2:
        risk = RISK_HIGH
    elif counts.affected_flows and (unverified or result.verification_gaps or counts.cross_repo_impacts):
        risk = RISK_MEDIUM
    elif counts.affected_flows and counts.code_findings:
        risk = RISK_MEDIUM

    verdict = VERDICT_OK
    if risk == RISK_HIGH:
        verdict = VERDICT_ACTION_REQUIRED
    elif risk == RISK_MEDIUM:
        verdict = VERDICT_REVIEW

    if not change.files:
        headline = f"No changes found against `{change.base}`."
    elif not result.affected_flows:
        headline = (
            f"{counts.changed_symbols} changed symbol(s); no route or event flow resolved to them."
        )
    else:
        entries = ", ".join(
            sorted({flow.entry_label for flow in result.affected_flows})[:3]
        )
        headline = f"{counts.changed_symbols} changed symbol(s) reach {counts.affected_flows} flow(s): {entries}"

    return ChangeSummary(
        risk=risk,
        verdict=verdict,
        headline=headline,
        counts=counts,
        risk_reasons=reasons,
    )


def analyze_change(
    *,
    repos: list[RepoRef],
    options: VerifyChangeOptions,
) -> ChangeVerificationResult:
    """Run the full change-verification pipeline for the primary repository."""
    if not repos:
        raise ValueError("At least one repository is required.")

    primary = repos[0]
    primary_root = Path(primary.root).expanduser().resolve()

    change = resolve_change_set(
        repo_name=primary.name,
        repo_root=primary_root,
        base=options.base,
        include_working_tree=options.include_working_tree,
    )
    change.repos = [RepoRef(name=item.name, root=str(Path(item.root).expanduser().resolve())) for item in repos]

    result = ChangeVerificationResult(
        generated_at=datetime.now(tz=UTC).isoformat(),
        change=change,
    )
    result.diagnostics.extend(change.notes)

    scan = scan_repository(primary.name, primary_root, max_files=options.max_scan_files)
    result.diagnostics.extend(scan.notes)
    result.diagnostics.append(f"{primary.name}: files_scanned={len(scan.files)}")

    index = build_symbol_index(scan)
    result.diagnostics.extend(f"{primary.name}: {note}" for note in index.notes)

    change.symbols = _attribute_symbols(change, index)

    events = detect_event_signals(scan)
    result.diagnostics.append(f"{primary.name}: event_signals={len(events)}")

    surface = build_system_surface(repo=primary, scan=scan, index=index, events=events)
    result.diagnostics.extend(f"{primary.name}: {note}" for note in surface.notes)

    changed_files = {item.path for item in change.files}
    changed_hunks = {
        item.path: [(hunk.start_line, hunk.end_line) for hunk in item.hunks]
        for item in change.files
    }
    builder = FlowBuilder(index=index, surface=surface, scan=scan, events=events)
    result.affected_flows = builder.build(
        [item.id for item in change.symbols], changed_files, changed_hunks
    )

    test_index = build_test_index(scan)
    result.diagnostics.extend(f"{primary.name}: {note}" for note in test_index.notes)

    changed_symbol_names = {item.name for item in change.symbols}
    changed_symbol_names |= {
        item.qualified_name for item in change.symbols if item.qualified_name
    }
    result.verification = map_existing_verification(
        flows=result.affected_flows,
        test_index=test_index,
        symbol_index=index,
        changed_symbol_names=changed_symbol_names,
        changed_files=changed_files,
    )

    surfaces = {primary.name: surface}
    for sibling in repos[1:]:
        sibling_root = Path(sibling.root).expanduser().resolve()
        sibling_scan = scan_repository(sibling.name, sibling_root, max_files=options.max_scan_files)
        sibling_index = build_symbol_index(sibling_scan)
        sibling_events = detect_event_signals(sibling_scan)
        surfaces[sibling.name] = build_system_surface(
            repo=sibling, scan=sibling_scan, index=sibling_index, events=sibling_events
        )
        result.diagnostics.append(
            f"{sibling.name}: routes_discovered={len(surfaces[sibling.name].routes)}"
        )

    result.cross_repo_impacts = detect_cross_repo_impacts(
        origin_repo=primary.name,
        flows=result.affected_flows,
        surfaces=surfaces,
    )
    if len(repos) == 1:
        result.notes.append("cross_repo=single_repo_configured")

    result.runtime_dependencies = infer_runtime_dependencies(
        scan=scan,
        flows=result.affected_flows,
        changed_files=changed_files,
        cross_repo_impacts=result.cross_repo_impacts,
    )

    _run_llm_stages(result, options, primary_root)

    result.summary = _compute_summary(result)
    return result


def _run_llm_stages(
    result: ChangeVerificationResult,
    options: VerifyChangeOptions,
    repo_root: Path,
) -> None:
    """Run bounded LLM passes for code findings and verification gaps."""
    if options.llm_policy == "never":
        result.notes.append("llm=skipped reason=policy_never")
        return
    if not result.change.files:
        result.notes.append("llm=skipped reason=no_changes")
        return

    diff_text = read_unified_diff(
        repo_root=repo_root,
        base_rev=result.change.merge_base or result.change.base,
        paths=[item.path for item in result.change.files if not item.binary][:40] or None,
    )
    context = build_change_context(
        change=result.change,
        flows=result.affected_flows,
        verification=result.verification,
        diff_text=diff_text,
    )
    covered_flow_ids = {
        flow_id
        for item in result.verification
        if item.status == VERIFICATION_VERIFIED
        for flow_id in item.related_flow_ids
    }

    if options.code_review:
        try:
            findings, warnings = generate_code_findings(
                context=context,
                model_spec=options.model_spec,
                llm_client=options.llm_client,
            )
            result.code_findings = findings
            result.diagnostics.extend(f"code_findings: {item}" for item in warnings)
        except LLMClientError as exc:
            result.notes.append(f"code_findings=failed reason={exc}")
    else:
        result.notes.append("code_findings=skipped reason=--no-code-review")

    if not result.affected_flows:
        result.notes.append("verification_gaps=skipped reason=no_affected_flows")
        return

    try:
        gaps, warnings = generate_verification_gaps(
            context=context,
            covered_flow_ids=covered_flow_ids,
            model_spec=options.model_spec,
            llm_client=options.llm_client,
        )
        result.verification_gaps = gaps
        result.diagnostics.extend(f"verification_gaps: {item}" for item in warnings)
    except LLMClientError as exc:
        result.notes.append(f"verification_gaps=failed reason={exc}")
