"""Orchestration for `sydes verify-change`.

This module owns change attribution and verification semantics. It owns no
system understanding: routes, handlers, call paths, sinks, contracts, and test
matrices all come from the shared Sydes discovery and trace stack, called here
in a different order (many routes reached from a diff, rather than one route
named on the command line).

    git diff                    verify/git_change
      -> changed symbols        shared handler symbol index
      -> reverse reachability   local index over shared symbols
      -> routes                 discover_endpoints
      -> route target           resolve_trace_target
      -> handler + call path    resolve_handler_reference / slice / call follower
      -> steps + sinks          build_layered_trace_contract
      -> contract + matrix      build_api_contract_from_routes / generate_test_matrix
      -> obligations            verify/obligations
      -> mapped tests           verify/test_mapping
      -> executions             verify/test_execution
      -> verdict                conservative aggregation, here
"""

from __future__ import annotations

from collections import defaultdict
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sydes.core.graph import build_graph_from_inferred_flow
from sydes.core.models import (
    ApiRouteContract,
    EndpointCandidate,
    RepoRef,
    TargetSpec,
    TraceResult,
    TraceSummary,
)
from sydes.discover.endpoints import discover_endpoints
from sydes.code_intelligence import get_code_intelligence
from sydes.discover.target_match import resolve_trace_target
from sydes.store.workspace import compute_workspace_id
from sydes.generate.contracts import build_api_contract_from_routes
from sydes.generate.tests import generate_test_matrix, match_route_contract
from sydes.llm.client import LLMClient
from sydes.trace.call_follower import CallFollowBudgets, build_layered_trace_expansion
from sydes.trace.expand import prepare_flow_expansion_context, run_flow_expansion
from sydes.trace.function_body_slicer import slice_resolved_handler_body
from sydes.trace.handler_resolver import resolve_handler_reference
from sydes.trace.layered_contract import build_layered_trace_contract
from sydes.trace.sinks import normalize_sink_candidates
from sydes.verify.git_change import resolve_change_set
from sydes.verify.models import (
    ANALYSIS_COMPLETE,
    ANALYSIS_PARTIAL,
    ANALYSIS_UNKNOWN,
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
    AffectedFlow,
    ChangedSymbol,
    ChangeSummary,
    ChangeVerificationResult,
    CiSuiteRun,
    RuntimeDependency,
    SourceRef,
    TestExecution,
    VerificationCounts,
    VerificationObligation,
)
from sydes.verify.obligations import derive_obligations
from sydes.verify.runtime import infer_runtime_dependencies
from sydes.verify.source_files import load_repo_files
from sydes.verify.test_execution import ExecutionSettings, run_ci_suite
from sydes.verify.test_index import build_test_index
from sydes.verify.test_mapping import map_tests_to_obligation

MAX_FLOWS = 12

# Shared-trace diagnostics that mean "analysis was incomplete", not "nothing
# downstream exists". Treating these as absence is the mistake this guards.
_PARTIAL_ANALYSIS_MARKERS = (
    "python_parse_failed",
    "handler_body_unavailable",
    "unresolved_call",
    "composition is unresolved",
    "could not be resolved",
)

# Limits on *optional context* reads. Checkpoint B separated the complete source
# read that deterministic parsing needs from these bounded evidence reads, so a
# truncated contextual read cannot hide a route, call, or sink that parsing
# already resolved. Reported as a diagnostic; never a reason to say PARTIAL.
_CONTEXTUAL_LIMIT_MARKERS = ("truncated",)

_COLLECTED_TRACE_MARKERS = _PARTIAL_ANALYSIS_MARKERS + _CONTEXTUAL_LIMIT_MARKERS


def _matches_marker(note: str, markers: tuple[str, ...]) -> bool:
    """True when a shared-stack diagnostic carries one of these markers."""
    lowered = note.lower()
    return any(marker in lowered for marker in markers)


@dataclass(slots=True)
class VerifyChangeOptions:
    """Runtime options for one verify-change analysis."""

    base: str = "main"
    include_working_tree: bool = True
    code_review: bool = False
    llm_policy: str = "auto"
    model_spec: str | None = None
    llm_client: LLMClient | None = None
    run_tests: bool = True
    test_timeout_seconds: float = 120.0
    diagnostics: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Change attribution — the verifier's own job
# --------------------------------------------------------------------------


def _symbols_by_file(handler_index: dict) -> dict[str, list[dict]]:
    """Index shared handler-symbol records by file."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for repo_index in handler_index.get("repos", []) or []:
        for file_item in repo_index.get("files", []) or []:
            by_file[file_item.get("path", "")].extend(file_item.get("symbols", []) or [])
    return by_file


def attribute_changed_symbols(change, handler_index: dict) -> list[ChangedSymbol]:
    """Attribute diff hunks to symbols from the shared handler-symbol index."""
    by_file = _symbols_by_file(handler_index)
    changed: dict[str, ChangedSymbol] = {}

    for changed_file in change.files:
        if changed_file.change_type == CHANGE_DELETED or changed_file.binary:
            continue
        candidates = by_file.get(changed_file.path, [])
        if not candidates:
            continue

        def _overlaps(symbol: dict) -> int:
            start = symbol.get("start_line")
            end = symbol.get("end_line") or start
            if not isinstance(start, int) or not isinstance(end, int):
                return 0
            if changed_file.change_type == CHANGE_ADDED:
                return end - start + 1
            return sum(
                1
                for hunk in changed_file.hunks
                for line in range(hunk.start_line, hunk.end_line + 1)
                if start <= line <= end
            )

        hits = [(symbol, _overlaps(symbol)) for symbol in candidates]
        hits = [(symbol, count) for symbol, count in hits if count > 0]
        method_parents = {
            symbol.get("parent") for symbol, _ in hits if symbol.get("kind") == "class_method"
        }

        for symbol, count in hits:
            # Prefer the specific method over its enclosing class.
            if symbol.get("kind") == "class" and symbol.get("name") in method_parents:
                continue
            name = str(symbol.get("name") or "")
            qualified = str(symbol.get("qualified_name") or name)
            identifier = f"{changed_file.repo}:{changed_file.path}:{qualified}"
            record = changed.get(identifier)
            if record is None:
                changed[identifier] = ChangedSymbol(
                    id=identifier,
                    repo=changed_file.repo,
                    file=changed_file.path,
                    name=name,
                    qualified_name=qualified,
                    kind=str(symbol.get("kind") or "function"),
                    language=str(symbol.get("language") or ""),
                    start_line=symbol.get("start_line"),
                    end_line=symbol.get("end_line"),
                    change_type=changed_file.change_type,
                    changed_lines=count,
                    decorators=[str(item) for item in symbol.get("decorators", []) or []],
                )
            else:
                record.changed_lines += count
            changed_file.symbols.append(identifier)

    return sorted(changed.values(), key=lambda item: (item.file, item.start_line or 0))


def build_reverse_reach_index(handler_index: dict) -> dict[str, set[str]]:
    """Map a symbol name to the files that import or call it.

    The shared stack follows calls forward from a known handler. Selecting which
    routes a diff can reach needs the opposite direction, so this small index is
    built over the shared symbol records rather than a second parse.
    """
    reachers: dict[str, set[str]] = defaultdict(set)
    for repo_index in handler_index.get("repos", []) or []:
        for file_item in repo_index.get("files", []) or []:
            path = file_item.get("path", "")
            for entry in file_item.get("imports", []) or []:
                resolved = entry.get("resolved_file")
                if resolved:
                    reachers[resolved].add(path)
                imported = entry.get("imported")
                if isinstance(imported, str):
                    reachers[imported].add(path)
    return reachers


def _candidate_route_files(
    change, changed_symbols: list[ChangedSymbol], reachers: dict[str, set[str]]
) -> set[str]:
    """Files that may declare a route reaching the change, via reverse closure."""
    frontier = {item.path for item in change.files}
    seen: set[str] = set()
    for _ in range(4):
        added: set[str] = set()
        for path in frontier:
            for reacher in reachers.get(path, set()):
                if reacher not in seen:
                    added.add(reacher)
        seen |= frontier
        if not added:
            break
        frontier = added
    seen |= frontier
    seen |= {item.file for item in changed_symbols}
    return seen


# --------------------------------------------------------------------------
# Shared-stack trace of one route
# --------------------------------------------------------------------------


def _analysis_status_from(notes: list[str]) -> tuple[str, list[str]]:
    """Classify how complete the shared analysis was, from its own diagnostics."""
    hits = [note for note in notes if _matches_marker(note, _PARTIAL_ANALYSIS_MARKERS)]
    if not hits:
        return ANALYSIS_COMPLETE, []
    return ANALYSIS_PARTIAL, hits[:6]


def _contextual_limit_notes(notes: list[str]) -> list[str]:
    """Bounded-read limits worth recording, but not evidence of missing topology."""
    return [
        note
        for note in notes
        if _matches_marker(note, _CONTEXTUAL_LIMIT_MARKERS)
        and not _matches_marker(note, _PARTIAL_ANALYSIS_MARKERS)
    ]


def _files_of(handler_index: dict, repo: str, path: str) -> list[dict]:
    """Return the shared index record for one file, if it was indexed."""
    for repo_index in handler_index.get("repos", []) or []:
        if repo_index.get("repo") != repo:
            continue
        return [
            item for item in repo_index.get("files", []) or [] if item.get("path") == path
        ]
    return []


def _trace_route(
    *,
    endpoint: EndpointCandidate,
    repos: list[RepoRef],
    handler_index: dict,
    options: VerifyChangeOptions,
    call_edges: list[dict] | None = None,
) -> tuple[dict, list[str]]:
    """Run the shared trace machinery for one endpoint.

    `call_edges` carries a backend-supplied call graph when the selected
    code-intelligence backend provides one; `None` means the native follower
    reads call names from source as before.
    """
    notes: list[str] = []
    repo_index = next(
        (
            item
            for item in handler_index.get("repos", []) or []
            if item.get("repo") == endpoint.repo
        ),
        {},
    )
    resolution = resolve_handler_reference(endpoint, repo_index)
    primary = resolution.get("primary_handler") or {}
    symbol = primary.get("symbol")

    repo_root = next(
        (Path(item.root) for item in repos if item.name == endpoint.repo), None
    )
    primary_slice = None
    layered_expansion = None
    if symbol is not None and repo_root is not None:
        primary_slice = slice_resolved_handler_body(
            repo_root=repo_root,
            handler_name=primary.get("normalized_handler") or endpoint.handler or "handler",
            symbol=symbol,
            language=str(symbol.get("language") or "unknown"),
        )
        if primary_slice is None:
            notes.append(
                f"handler_body_unavailable: no body slice for {endpoint.handler} in {symbol.get('file')}"
            )
        else:
            layered_expansion = build_layered_trace_expansion(
                repo_root=repo_root,
                matched_endpoint=endpoint.model_dump(),
                resolution=resolution,
                primary_slice=primary_slice,
                repo_index=repo_index,
                budgets=CallFollowBudgets(),
                call_edges=call_edges,
            )
            known_symbols = {
                str(symbol.get("name") or "")
                for file_item in repo_index.get("files", []) or []
                for symbol in file_item.get("symbols", []) or []
            }
            for item in layered_expansion.get("unresolved_calls", []) or []:
                call = str(item.get("call") or "")
                leaf = call.rsplit(".", 1)[-1]
                # A name the repository never defines was never resolvable —
                # that is an attribute on a local, not incomplete analysis.
                if leaf not in known_symbols:
                    continue
                notes.append(f"unresolved_call: {call} ({item.get('reason')})")
    elif symbol is None:
        notes.append(
            f"handler_body_unavailable: handler '{endpoint.handler}' not resolved to a symbol"
        )

    contract = build_layered_trace_contract(
        matched_endpoint=endpoint.model_dump(),
        primary_slice=primary_slice,
        resolved_handlers={"resolution": resolution},
        layered_trace_expansion=layered_expansion,
        llm_summary=None,
        budgets=None,
    )

    # Deterministic flow expansion supplies sinks in the shared taxonomy.
    expansion_context = prepare_flow_expansion_context(
        matched_endpoint=endpoint, repos=repos, max_related_files=0
    )
    notes.extend(
        note
        for note in expansion_context.notes
        if _matches_marker(note, _COLLECTED_TRACE_MARKERS)
    )
    flow_expansion = None
    if options.llm_policy != "never":
        try:
            flow_expansion = run_flow_expansion(
                endpoint,
                repos,
                llm_client=options.llm_client,
                model_spec=options.model_spec,
                strict_llm=False,
            )
            flow_expansion.sinks = normalize_sink_candidates(flow_expansion.sinks)
            notes.extend(
                note
                for note in flow_expansion.notes
                if _matches_marker(note, _COLLECTED_TRACE_MARKERS)
            )
        except Exception as exc:  # noqa: BLE001 - expansion must not abort verification
            notes.append(f"flow_expansion_unavailable: {exc}")

    return (
        {
            "resolution": resolution,
            "layered_contract": contract,
            "layered_expansion": layered_expansion,
            "flow_expansion": flow_expansion,
            "handler_symbol": symbol,
            "primary_slice": primary_slice,
        },
        notes,
    )


def _trace_result_for_matrix(
    endpoint: EndpointCandidate, repos: list[RepoRef], traced: dict
) -> TraceResult:
    """Assemble the TraceResult shape `generate_test_matrix` consumes.

    A thin adapter over shared outputs — the graph is built by the shared
    `build_graph_from_inferred_flow`, not by anything local.
    """
    flow_expansion = traced.get("flow_expansion")
    nodes: list[Any] = []
    edges: list[Any] = []
    flows: list[Any] = []
    if flow_expansion is not None:
        nodes, edges, flows = build_graph_from_inferred_flow(endpoint, flow_expansion)

    contract = traced.get("layered_contract") or {}
    return TraceResult(
        target=TargetSpec(path=endpoint.path or "/", method=endpoint.method),
        repos=repos,
        nodes=nodes,
        edges=edges,
        flows=flows,
        matched_endpoint=endpoint,
        sinks=list(contract.get("sinks", []) or []),
        layers=list(contract.get("layers", []) or []),
        summary=TraceSummary(key_flow_id=flows[0].id if flows else None),
    )


# --------------------------------------------------------------------------
# Verdict semantics
# --------------------------------------------------------------------------


def resolve_obligation_status(
    obligation: VerificationObligation, ci_suite: CiSuiteRun | None
) -> None:
    """Set an obligation's status from the tests mapped to *it*.

    The regression suite supplies the pass/fail signal, but only for tests that
    were actually mapped to this obligation. A green suite says the repository
    is healthy; it never turns an unmatched obligation into evidence.
    """
    if not obligation.mapped_tests:
        obligation.status = VERIFICATION_UNVERIFIED
        obligation.reason = (
            f"{len(obligation.supporting_tests)} test(s) exercise this flow but none assert "
            "this behavior"
            if obligation.supporting_tests
            else "No existing test asserts this behavior"
        )
        return

    if ci_suite is None:
        obligation.status = VERIFICATION_UNKNOWN
        obligation.reason = "The repository test suite was not executed"
        return

    if ci_suite.status == VERIFICATION_UNKNOWN:
        obligation.status = VERIFICATION_UNKNOWN
        obligation.reason = ci_suite.reason or "The repository test suite could not be executed"
        return

    failing = [
        test
        for test in obligation.mapped_tests
        if _test_is_in(test, ci_suite.failed_test_ids)
    ]
    if failing:
        obligation.status = VERIFICATION_FAILED
        obligation.reason = f"`{failing[0].name}` failed in the repository test suite"
        return

    if ci_suite.status == VERIFICATION_FAILED and not ci_suite.failed_test_ids:
        # The suite failed but named no tests, so this obligation's tests cannot
        # be cleared or blamed.
        obligation.status = VERIFICATION_UNKNOWN
        obligation.reason = "The test suite failed without attributable test results"
        return

    obligation.status = VERIFICATION_PASSED
    obligation.reason = None


def _test_is_in(test, failed_ids: list[str]) -> bool:
    """True when a mapped test appears among the suite's failing test ids."""
    case = test.case_name or test.name
    for node in failed_ids:
        if test.file and test.file in node and case and case in node:
            return True
    return False


def resolve_flow_status(flow: AffectedFlow) -> None:
    """Derive a flow's status from its obligations, conservatively.

    Worst-status-wins, so an unverified obligation can never be masked by a
    passing one elsewhere in the same flow.
    """
    required = [item for item in flow.obligations if item.required]
    if not required:
        flow.status = VERIFICATION_UNVERIFIED
        flow.reason = "No verification obligation could be derived for this flow"
        return
    statuses = {item.status for item in required}
    if VERIFICATION_FAILED in statuses:
        flow.status = VERIFICATION_FAILED
    elif VERIFICATION_UNKNOWN in statuses:
        flow.status = VERIFICATION_UNKNOWN
    elif VERIFICATION_UNVERIFIED in statuses:
        flow.status = VERIFICATION_UNVERIFIED
    else:
        flow.status = VERIFICATION_PASSED
    unresolved = [item for item in required if item.status != VERIFICATION_PASSED]
    flow.reason = (
        f"{len(unresolved)} of {len(required)} obligation(s) not demonstrated"
        if unresolved
        else None
    )


def _compute_summary(result: ChangeVerificationResult) -> ChangeSummary:
    """Derive risk and verdict from obligation outcomes only."""
    change = result.change
    obligations = [item for flow in result.affected_flows for item in flow.obligations]
    required = [item for item in obligations if item.required]

    def _count(state: str) -> int:
        return sum(1 for item in required if item.status == state)

    suite = result.ci_suite
    executed = (
        (suite.tests_passed or 0) + (suite.tests_failed or 0)
        if suite is not None and suite.status != VERIFICATION_UNKNOWN
        else 0
    )
    counts = VerificationCounts(
        changed_files=len(change.files),
        changed_source_files=sum(
            1 for item in change.files if item.role == "source_route_candidate"
        ),
        changed_test_files=sum(
            1 for item in change.files if item.role == "test_usage_candidate"
        ),
        changed_symbols=len(change.symbols),
        affected_flows=len(result.affected_flows),
        flows_partially_analyzed=sum(
            1 for flow in result.affected_flows if flow.analysis_status != ANALYSIS_COMPLETE
        ),
        code_findings=len(result.code_findings),
        obligations=len(required),
        obligations_introduced_by_change=sum(
            1 for item in required if item.introduced_by_change
        ),
        obligations_passed=_count(VERIFICATION_PASSED),
        obligations_failed=_count(VERIFICATION_FAILED),
        obligations_unverified=_count(VERIFICATION_UNVERIFIED),
        obligations_unknown=_count(VERIFICATION_UNKNOWN),
        mapped_tests=sum(len(item.mapped_tests) for item in required),
        supporting_tests=sum(len(item.supporting_tests) for item in required),
        tests_executed=executed,
        verification_gaps=len(result.verification_gaps),
        runtime_dependencies=len(result.runtime_dependencies),
        cross_repo_impacts=len(result.cross_repo_impacts),
    )

    reasons: list[str] = []
    if counts.obligations_failed:
        reasons.append(f"{counts.obligations_failed} obligation(s) failed an executed test")
    if counts.obligations_unknown:
        reasons.append(f"{counts.obligations_unknown} obligation(s) could not be executed")
    if counts.obligations_unverified:
        reasons.append(f"{counts.obligations_unverified} obligation(s) have no verifying test")
    if counts.flows_partially_analyzed:
        reasons.append(
            f"{counts.flows_partially_analyzed} flow(s) only partially analyzed — "
            "absence of downstream effects is not established"
        )

    # Conservative aggregation. An unresolved obligation always prevents
    # VERIFIED; passing regression tests elsewhere cannot mask it.
    # A red regression suite is hard evidence of breakage and outranks every
    # classification question: it stands even when the only failing obligations
    # were advisory ones excluded from the required set.
    suite_failed = suite is not None and suite.status == VERIFICATION_FAILED
    if suite_failed:
        reasons.append(
            f"the repository test suite failed ({suite.tests_failed or 'some'} test(s))"
        )

    if counts.obligations_failed or suite_failed:
        verdict, risk = VERDICT_ACTION_REQUIRED, RISK_HIGH
    elif counts.obligations_unknown or counts.obligations_unverified:
        verdict = VERDICT_INCOMPLETE
        risk = RISK_HIGH if counts.obligations_introduced_by_change else RISK_MEDIUM
    elif counts.obligations:
        verdict, risk = VERDICT_VERIFIED, RISK_LOW
    elif change.files:
        verdict, risk = VERDICT_INCOMPLETE, RISK_MEDIUM
        reasons.append("change could not be tied to any verifiable behavior")
    else:
        verdict, risk = VERDICT_OK, RISK_LOW

    if result.analysis_status != ANALYSIS_COMPLETE and verdict == VERDICT_VERIFIED:
        verdict = VERDICT_INCOMPLETE
        reasons.append("analysis was incomplete, so VERIFIED cannot be claimed")

    if not change.files:
        headline = f"No changes found against `{change.base}`."
    elif not result.affected_flows:
        headline = (
            f"{counts.changed_symbols} changed symbol(s); no route flow resolved to them."
        )
    else:
        headline = (
            f"{counts.obligations} obligation(s) across {counts.affected_flows} flow(s): "
            f"{counts.obligations_passed} passed, {counts.obligations_failed} failed, "
            f"{counts.obligations_unverified} unverified, {counts.obligations_unknown} unknown"
        )

    return ChangeSummary(
        risk=risk, verdict=verdict, headline=headline, counts=counts, risk_reasons=reasons
    )


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


def _link_runtime_blockers(execution, dependencies: list[RuntimeDependency]) -> None:
    """Point an environment-blocked run at the runtime dependency behind it."""
    if execution.blocker != BLOCKER_MISSING_DEPENDENCY:
        return
    haystack = " ".join(
        part
        for part in (
            getattr(execution, "missing_dependency", None),
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
            execution.reason = (
                f"{execution.reason or ''} (runtime dependency: {dependency.name})".strip()
            )
            return


def analyze_change(
    *, repos: list[RepoRef], options: VerifyChangeOptions
) -> ChangeVerificationResult:
    """Run the full change-verification pipeline for the primary repository."""
    if not repos:
        raise ValueError("At least one repository is required.")

    primary = repos[0]
    primary_root = Path(primary.root).expanduser().resolve()
    normalized_repos = [
        RepoRef(name=item.name, root=str(Path(item.root).expanduser().resolve()))
        for item in repos
    ]

    change = resolve_change_set(
        repo_name=primary.name,
        repo_root=primary_root,
        base=options.base,
        include_working_tree=options.include_working_tree,
    )
    change.repos = normalized_repos
    result = ChangeVerificationResult(
        generated_at=datetime.now(tz=UTC).isoformat(), change=change
    )
    result.diagnostics.extend(change.notes)

    # --- shared system understanding -------------------------------------
    # Route, symbol, and graph facts all come from one incremental index; the
    # analysis below is unchanged and simply reads from it.
    workspace_id = compute_workspace_id(normalized_repos)
    structural = get_code_intelligence().build_or_update(
        normalized_repos, workspace_id=workspace_id
    )
    result.diagnostics.extend(structural.diagnostics)
    handler_index = structural.symbol_index
    result.diagnostics.append(
        "handler_symbol_index: "
        + ", ".join(f"{key}={value}" for key, value in sorted(handler_index.get("summary", {}).items()))
    )

    change.symbols = attribute_changed_symbols(change, handler_index)
    indexed_files = {
        file_item.get("path")
        for repo_index in handler_index.get("repos", []) or []
        for file_item in repo_index.get("files", []) or []
    }
    unattributed = [
        item.path
        for item in change.files
        if item.role == "source_route_candidate"
        and item.change_type != CHANGE_DELETED
        and not item.binary
        and not any(symbol.file == item.path for symbol in change.symbols)
    ]
    if unattributed:
        result.analysis_status = ANALYSIS_PARTIAL
        for path in unattributed[:6]:
            reason = (
                "file could not be parsed into symbols"
                if path in indexed_files
                else "file was not indexed by the shared symbol index"
            )
            result.analysis_notes.append(
                f"Changed source file `{path}` yielded no symbols: {reason}. "
                "Downstream effects of this file are not established."
            )
    reachers = build_reverse_reach_index(handler_index)
    candidate_files = _candidate_route_files(change, change.symbols, reachers)
    result.diagnostics.append(f"reverse_reach_candidate_files={len(candidate_files)}")

    routes = discover_endpoints(
        normalized_repos,
        model_spec=options.model_spec,
        llm_policy=options.llm_policy if options.llm_policy in {"auto", "always", "never"} else "auto",
        strict_llm=False,
        route_index_batch=structural.route_index,
    )
    result.diagnostics.extend(
        note for note in routes.notes if "coverage" in note or "routes" in note.lower()
    )
    if any("composition is unresolved" in note for note in routes.notes):
        result.analysis_notes.append(
            "Route composition is unresolved in this repository; some routes may be missing."
        )
        result.analysis_status = ANALYSIS_PARTIAL

    selected = [
        endpoint
        for endpoint in routes.routes
        if (endpoint.file or "") in candidate_files
    ]
    if not selected and change.symbols:
        result.analysis_notes.append(
            "No discovered route declaration reaches the changed symbols."
        )

    contract_artifact = build_api_contract_from_routes(
        routes, repo_roots={item.name: item.root for item in normalized_repos}
    )

    changed_files = {item.path for item in change.files}
    changed_symbol_names = {item.name for item in change.symbols}
    changed_symbol_names |= {
        item.qualified_name for item in change.symbols if item.qualified_name
    }

    repo_files = load_repo_files(primary.name, primary_root)
    test_index = build_test_index(repo_files)
    result.diagnostics.extend(f"{primary.name}: {note}" for note in test_index.notes)

    # --- one flow per reachable route ------------------------------------
    changed_symbol_keys = {(item.file, item.name) for item in change.symbols}
    changed_symbol_keys |= {
        (item.file, item.qualified_name) for item in change.symbols if item.qualified_name
    }
    changed_hunks = {
        item.path: [(hunk.start_line, hunk.end_line) for hunk in item.hunks]
        for item in change.files
    }

    for endpoint in selected[:MAX_FLOWS * 3]:
        if len(result.affected_flows) >= MAX_FLOWS:
            break
        match = resolve_trace_target(
            routes.routes, path=endpoint.path or "/", method=endpoint.method
        )
        resolved_endpoint = match.selected or endpoint
        traced, trace_notes = _trace_route(
            endpoint=resolved_endpoint,
            repos=normalized_repos,
            handler_index=handler_index,
            options=options,
            # Present only when the backend supplies a call graph. Sydes never
            # substitutes its own extraction for a backend that was selected
            # and returned nothing: the absence is reported as uncertainty.
            call_edges=structural.call_edges if structural.provides_call_graph else None,
        )
        contract = traced["layered_contract"]
        analysis_status, analysis_notes = _analysis_status_from(trace_notes)
        for note in _contextual_limit_notes(trace_notes):
            if note not in result.diagnostics:
                result.diagnostics.append(note)

        # A route sharing a file with the change is a candidate, not a hit. Keep
        # the flow only when the change is actually on its path: its handler was
        # changed, its declaration line was edited, or a followed call lands in a
        # changed symbol.
        handler_symbol = traced.get("handler_symbol") or {}
        # A resolved handler exposes `qualified_name`; the index exposes `name`.
        handler_names = {
            str(handler_symbol.get("name") or ""),
            str(handler_symbol.get("qualified_name") or ""),
            str(handler_symbol.get("qualified_name") or "").rsplit(".", 1)[-1],
        } - {""}
        handler_file = str(handler_symbol.get("file") or "")
        reached = any((handler_file, candidate) in changed_symbol_keys for candidate in handler_names)
        if not reached:
            expansion = traced.get("layered_expansion") or {}
            for followed in expansion.get("followed_calls", []) or []:
                if (str(followed.get("file") or ""), str(followed.get("resolved_to") or "")) in changed_symbol_keys:
                    reached = True
                    break
        if not reached:
            declaration_line = None
            for evidence in resolved_endpoint.evidence:
                if (evidence.label or "") == "route_declaration":
                    declaration_line = None  # line not carried on the contract
            spans = changed_hunks.get(resolved_endpoint.file or "", [])
            handler_start = handler_symbol.get("start_line")
            handler_end = handler_symbol.get("end_line") or handler_start
            if isinstance(handler_start, int) and isinstance(handler_end, int):
                reached = any(
                    start <= handler_end and handler_start <= end for start, end in spans
                )
        # Last resort: the handler's own body names a changed symbol. The shared
        # call follower could not prove the edge (a known limitation for calls
        # through instance attributes), so the flow is kept and explicitly marked
        # incomplete rather than silently dropped. An import alone is not enough:
        # importing a changed module says nothing about *this* handler.
        import_reached = False
        if not reached:
            body_text = " ".join(
                str(statement.get("text") or "")
                for statement in ((traced.get("primary_slice") or {}).get("statements") or [])
            )
            referenced = sorted(
                name
                for _file, name in changed_symbol_keys
                if name and re.search(rf"\b{re.escape(name.rsplit('.', 1)[-1])}\s*\(", body_text)
            )
            import_reached = bool(referenced)
            if import_reached:
                analysis_status = ANALYSIS_PARTIAL
                analysis_notes = [
                    *analysis_notes,
                    f"Handler body calls `{referenced[0]}`, but the call edge could not be "
                    "resolved by the shared call follower; downstream effects are incomplete.",
                ]
        if not reached and not import_reached:
            continue

        flow = AffectedFlow(
            id=f"flow:{(resolved_endpoint.method or 'ANY').upper()}:{resolved_endpoint.path}",
            entry_label=f"{(resolved_endpoint.method or 'ANY').upper()} {resolved_endpoint.path}",
            repo=resolved_endpoint.repo,
            method=(resolved_endpoint.method or "ANY").upper(),
            path=resolved_endpoint.path,
            handler=resolved_endpoint.handler,
            artifact_refs={
                "route_file": resolved_endpoint.file or "",
                "handler_file": str(handler_symbol.get("file") or ""),
                "layered_trace_contract": contract.get("version", "v1"),
            },
            changed_nodes=[
                SourceRef(
                    repo=item.repo,
                    file=item.file,
                    symbol=item.qualified_name or item.name,
                    line=item.start_line,
                )
                for item in change.symbols
            ],
            steps=list((contract.get("flow") or {}).get("steps", []) or []),
            sinks=list(contract.get("sinks", []) or []),
            analysis_status=analysis_status,
            analysis_notes=analysis_notes,
        )

        route_contract: ApiRouteContract | None = match_route_contract(
            contract_artifact, method=flow.method, path=flow.path or "/"
        )
        matrix = None
        try:
            matrix = generate_test_matrix(
                _trace_result_for_matrix(resolved_endpoint, normalized_repos, traced),
                route_contract=route_contract,
            )
        except Exception as exc:  # noqa: BLE001 - matrix is one obligation source of several
            flow.analysis_notes.append(f"test_matrix_unavailable: {exc}")

        flow.obligations = derive_obligations(
            flow=flow,
            route_contract=route_contract,
            test_matrix=matrix,
            changed_symbols=change.symbols,
            changed_files=changed_files,
        )
        for obligation in flow.obligations:
            evidence, supporting = map_tests_to_obligation(
                obligation=obligation,
                flow=flow,
                test_index=test_index,
                changed_symbol_names=changed_symbol_names,
                changed_files=changed_files,
            )
            obligation.mapped_tests = evidence
            obligation.supporting_tests = supporting

        result.affected_flows.append(flow)

    if any(flow.analysis_status != ANALYSIS_COMPLETE for flow in result.affected_flows):
        result.analysis_status = ANALYSIS_PARTIAL
    if selected and not result.affected_flows:
        result.analysis_status = ANALYSIS_UNKNOWN

    # --- runtime dependencies, then execution ----------------------------
    result.runtime_dependencies = infer_runtime_dependencies(
        files=repo_files, flows=result.affected_flows, changed_files=changed_files
    )
    _run_test_execution(result, options, repo_files, primary_root)

    for flow in result.affected_flows:
        resolve_flow_status(flow)
    result.summary = _compute_summary(result)
    return result


def _run_test_execution(
    result: ChangeVerificationResult,
    options: VerifyChangeOptions,
    repo_files,
    repo_root: Path,
) -> None:
    """Run the repository's own test suite once and resolve obligations from it."""
    settings = ExecutionSettings(
        enabled=options.run_tests, timeout_seconds=options.test_timeout_seconds
    )
    ci_suite, notes = run_ci_suite(
        files=repo_files, repo_root=repo_root, settings=settings
    )
    result.diagnostics.extend(notes)
    result.ci_suite = ci_suite

    if not options.run_tests:
        result.notes.append("test_execution=skipped reason=--no-run-tests")

    if ci_suite is not None and ci_suite.blocker == BLOCKER_MISSING_DEPENDENCY:
        _link_runtime_blockers(ci_suite, result.runtime_dependencies)

    for flow in result.affected_flows:
        for obligation in flow.obligations:
            if not options.run_tests and obligation.mapped_tests:
                obligation.status = VERIFICATION_UNKNOWN
                obligation.reason = "Test execution was disabled (--no-run-tests)"
                continue
            resolve_obligation_status(obligation, ci_suite)
