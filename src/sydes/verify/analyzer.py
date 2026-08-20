"""Orchestration for `sydes verify-change`.

Pipeline:

    git diff -> changed files/symbols
              -> Sydes structural intelligence (symbols, calls, routes, events)
              -> affected system flows
              -> mapped existing tests
              -> test execution
              -> behavior verification state
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
    BLOCKER_EXECUTION_DISABLED,
    BLOCKER_MISSING_DEPENDENCY,
    CHANGE_ADDED,
    CHANGE_DELETED,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    VERDICT_ACTION_REQUIRED,
    VERDICT_INCOMPLETE,
    VERDICT_OK,
    VERDICT_VERIFIED,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VERIFICATION_UNVERIFIED,
    ChangedSymbol,
    ChangeSummary,
    ChangeVerificationResult,
    RuntimeDependency,
    TestExecution,
    VerificationCounts,
    VerificationItem,
)
from sydes.verify.repo_scan import scan_repository
from sydes.verify.runtime import infer_runtime_dependencies
from sydes.verify.surface import FlowBuilder, build_system_surface
from sydes.verify.test_execution import ExecutionSettings, execute_mapped_tests
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
    run_tests: bool = True
    test_timeout_seconds: float = 120.0
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


def _link_runtime_blockers(
    execution: TestExecution, dependencies: list[RuntimeDependency]
) -> None:
    """Point an environment-blocked execution at the runtime dependency behind it.

    V1 already discovered what this repository needs to be running; when a test
    cannot execute for environment reasons, naming that dependency is the useful
    half of the answer.
    """
    if execution.blocker != BLOCKER_MISSING_DEPENDENCY:
        return
    haystack = " ".join(
        part
        for part in (
            execution.missing_dependency,
            execution.reason,
            execution.stderr_excerpt,
            execution.stdout_excerpt,
        )
        if part
    ).lower()
    for dependency in dependencies:
        tokens = {dependency.kind.replace("_", " ")}
        tokens.update(word for word in dependency.name.lower().split() if len(word) > 3)
        if any(token in haystack for token in tokens if token):
            execution.blocking_runtime_dependency_ids.append(dependency.id)


def _resolve_behavior_status(
    item: VerificationItem, executions_by_test: dict[str, TestExecution]
) -> None:
    """Set a behavior's state from the executions of its mapped tests.

    Precedence is failed > passed > unknown > unverified: a real failure always
    wins, and a genuine passing execution is not downgraded because a *second*
    mapped test could not be run.
    """
    if not item.tests:
        item.status = VERIFICATION_UNVERIFIED
        item.reason = item.reason or "No applicable existing verification found"
        return

    item.executions = [
        executions_by_test[test.id] for test in item.tests if test.id in executions_by_test
    ]
    if not item.executions:
        item.status = VERIFICATION_UNKNOWN
        item.reason = item.reason or "Mapped tests were not executed"
        return

    statuses = {execution.status for execution in item.executions}
    if VERIFICATION_FAILED in statuses:
        failed = next(e for e in item.executions if e.status == VERIFICATION_FAILED)
        item.status = VERIFICATION_FAILED
        item.reason = failed.failure_summary or "A mapped test failed"
        return
    if VERIFICATION_PASSED in statuses:
        item.status = VERIFICATION_PASSED
        blocked = [e for e in item.executions if e.status == VERIFICATION_UNKNOWN]
        item.reason = (
            f"{len(blocked)} further mapped test(s) could not be executed" if blocked else None
        )
        return
    item.status = VERIFICATION_UNKNOWN
    item.reason = next(
        (e.reason for e in item.executions if e.reason),
        "Mapped tests could not be executed or interpreted",
    )


def _run_test_execution(
    result: ChangeVerificationResult,
    options: VerifyChangeOptions,
    scan,
    repo_root: Path,
) -> None:
    """Execute the mapped tests and fold the evidence into each behavior."""
    settings = ExecutionSettings(
        enabled=options.run_tests,
        timeout_seconds=options.test_timeout_seconds,
    )
    mapped = [test for item in result.verification for test in item.tests]

    executions, notes = execute_mapped_tests(
        tests=mapped,
        scan=scan,
        repo_root=repo_root,
        settings=settings,
    )
    result.diagnostics.extend(notes)
    result.test_executions = executions

    if not options.run_tests:
        result.notes.append("test_execution=skipped reason=--no-run-tests")

    executions_by_test = {execution.test_id: execution for execution in executions}
    for execution in executions:
        _link_runtime_blockers(execution, result.runtime_dependencies)

    for item in result.verification:
        if not options.run_tests and item.tests:
            item.status = VERIFICATION_UNKNOWN
            item.reason = "Test execution was disabled (--no-run-tests)"
            item.executions = [
                TestExecution(
                    test_id=test.id,
                    framework="unknown",
                    status=VERIFICATION_UNKNOWN,
                    blocker=BLOCKER_EXECUTION_DISABLED,
                    reason="Test execution was disabled (--no-run-tests)",
                )
                for test in item.tests
            ]
            continue
        _resolve_behavior_status(item, executions_by_test)


def _compute_summary(result: ChangeVerificationResult) -> ChangeSummary:
    """Derive risk and verdict from executed evidence and concrete counts."""
    change = result.change
    source_files = [item for item in change.files if item.role == "source_route_candidate"]
    test_files = [item for item in change.files if item.role == "test_usage_candidate"]

    by_status = {
        state: [item for item in result.verification if item.status == state]
        for state in (
            VERIFICATION_PASSED,
            VERIFICATION_FAILED,
            VERIFICATION_UNVERIFIED,
            VERIFICATION_UNKNOWN,
        )
    }
    executed = [
        item
        for item in result.test_executions
        if item.status in {VERIFICATION_PASSED, VERIFICATION_FAILED}
    ]

    counts = VerificationCounts(
        changed_files=len(change.files),
        changed_source_files=len(source_files),
        changed_test_files=len(test_files),
        changed_symbols=len(change.symbols),
        affected_flows=len(result.affected_flows),
        code_findings=len(result.code_findings),
        affected_behaviors=len(result.verification),
        behaviors_passed=len(by_status[VERIFICATION_PASSED]),
        behaviors_failed=len(by_status[VERIFICATION_FAILED]),
        behaviors_unverified=len(by_status[VERIFICATION_UNVERIFIED]),
        behaviors_unknown=len(by_status[VERIFICATION_UNKNOWN]),
        mapped_tests=sum(len(item.tests) for item in result.verification),
        tests_executed=len(executed),
        verification_gaps=len(result.verification_gaps),
        runtime_dependencies=len(result.runtime_dependencies),
        cross_repo_impacts=len(result.cross_repo_impacts),
    )

    reasons: list[str] = []
    severe_findings = [item for item in result.code_findings if item.severity in {"P0", "P1"}]
    if counts.behaviors_failed:
        reasons.append(f"{counts.behaviors_failed} behavior(s) failed an executed test")
    if severe_findings:
        reasons.append(f"{len(severe_findings)} P0/P1 code finding(s)")
    if counts.behaviors_unverified:
        reasons.append(f"{counts.behaviors_unverified} behavior(s) with no applicable test")
    if counts.behaviors_unknown:
        reasons.append(f"{counts.behaviors_unknown} behavior(s) whose tests could not be executed")
    if result.verification_gaps:
        reasons.append(f"{len(result.verification_gaps)} verification gap(s)")
    if any(
        node.kind in {"event", "consumer", "client"}
        for flow in result.affected_flows
        for node in flow.nodes
    ):
        reasons.append("change reaches an event or outbound service boundary")

    # Verdict is decided by executed evidence, then by what could not be checked.
    if counts.behaviors_failed or severe_findings:
        verdict = VERDICT_ACTION_REQUIRED
        risk = RISK_HIGH
    elif counts.behaviors_unverified or counts.behaviors_unknown:
        verdict = VERDICT_INCOMPLETE
        risk = RISK_HIGH if len(result.verification_gaps) >= 2 else RISK_MEDIUM
    elif counts.affected_behaviors:
        verdict = VERDICT_VERIFIED
        risk = RISK_LOW
    elif change.files:
        verdict = VERDICT_INCOMPLETE
        risk = RISK_MEDIUM
        reasons.append("change could not be tied to any affected behavior")
    else:
        verdict = VERDICT_OK
        risk = RISK_LOW

    if not change.files:
        headline = f"No changes found against `{change.base}`."
    elif not result.affected_flows:
        headline = (
            f"{counts.changed_symbols} changed symbol(s); no route or event flow resolved to them."
        )
    else:
        headline = (
            f"{counts.affected_behaviors} affected behavior(s): "
            f"{counts.behaviors_passed} passed, {counts.behaviors_failed} failed, "
            f"{counts.behaviors_unverified} unverified, {counts.behaviors_unknown} unknown "
            f"({counts.tests_executed} test(s) executed)"
        )

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

    # Execution runs after runtime inference so an environment blocker can be
    # attributed to a dependency Sydes already discovered.
    _run_test_execution(result, options, scan, primary_root)

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
    # A behavior with an executed result is already answered; gaps are for the
    # behaviors execution could not settle.
    covered_flow_ids = {
        flow_id
        for item in result.verification
        if item.status in {VERIFICATION_PASSED, VERIFICATION_FAILED}
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
