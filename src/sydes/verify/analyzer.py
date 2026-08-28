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
from sydes.code_intelligence.base import StructuralFacts
from sydes.code_intelligence.cbm import CBM_BACKEND
from sydes.impact import (
    COMPLETENESS_COMPLETE,
    ENTRYPOINT_HTTP,
    GUIDE_OFF,
    IMPACT_STATUS_INFERRED,
    IMPACT_STATUS_PROVEN,
    GuideBudget,
    ImpactInterpreter,
    ImpactResult,
    LLMImpactGuide,
    reconcile_entrypoints,
)
from sydes.discover.target_match import resolve_trace_target
from sydes.observability import trace as _trace
from sydes.store.workspace import compute_workspace_id
from sydes.generate.contracts import build_api_contract_from_routes
from sydes.generate.tests import generate_test_matrix, match_route_contract
from sydes.llm.client import LLMClient, LLMClientError, create_default_llm_client
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
    AcceptedImpact,
    AffectedBoundary,
    AffectedFlow,
    ChangedSymbol,
    ChangeSet,
    ChangeSummary,
    ChangeVerificationResult,
    CiSuiteRun,
    RuntimeDependency,
    SourceRef,
    TestExecution,
    VerificationCounts,
    VerificationObligation,
)
from sydes.verify.boundary_reasoning import infer_boundaries
from sydes.verify.obligations import derive_obligations
from sydes.verify.pr_semantic_analysis import generate_pr_semantic_analysis
from sydes.verify.repo_profile import get_or_build_repo_profile
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
    #: M3 guide policy for unresolved impact: `off` (default), `auto`
    #: (invoke only on the structural triggers `ImpactInterpreter` already
    #: recognises), or `always`. Conservative default: no LLM call happens
    #: for impact analysis unless this is explicitly set.
    impact_guide: str = GUIDE_OFF
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


def _build_accepted_impacts(
    impact_result: ImpactResult | None, affected_flows: list[AffectedFlow],
) -> list[AcceptedImpact]:
    """The one canonical merged impact list every downstream reader uses.

    cbm backend: built from `impact_result.affected`, which already merges
    PROVEN and INFERRED entries (PROVEN winning on any duplicate — see
    `ImpactInterpreter._record_inferred`) — nothing is re-derived here, only
    reshaped for the product-facing artifact. An entry that reached a real
    `AffectedFlow` (matched by method+path) is `verification_model_status
    ="modeled"`; anything else — a generic non-HTTP behavior, or a route
    that never matched a real one — is `"unsupported_or_partial"` and stays
    in this list rather than disappearing.

    native backend (no `ImpactResult` exists): every affected flow is by
    definition PROVEN and already modeled, so the canonical list is built
    directly from `affected_flows` instead — still one code path for the
    renderer, never two competing views depending on backend.
    """
    if impact_result is None:
        native_impacts = [
            AcceptedImpact(
                id=flow.id, label=flow.entry_label, repo=flow.repo,
                kind=ENTRYPOINT_HTTP if flow.method and flow.path else "unknown",
                status=IMPACT_STATUS_PROVEN,
                changed_symbols=sorted({node.symbol for node in flow.changed_nodes}),
                route_method=flow.method, route_path=flow.path,
                verification_model_status="modeled",
            )
            for flow in affected_flows
        ]
        _trace_verification_decisions(native_impacts, affected_flows)
        return native_impacts

    flow_by_route = {
        ((flow.method or "").upper(), flow.path): flow
        for flow in affected_flows if flow.method and flow.path
    }
    # Dedup is the interpreter's job first (`ImpactInterpreter._record_inferred`
    # never emits a PROVEN+INFERRED pair for the same entrypoint), but this
    # function is the canonical boundary Part 1 describes — it must guarantee
    # "PROVEN wins, no duplicates" itself rather than merely assume its input
    # already satisfies that, so a future caller of `ImpactResult.affected`
    # cannot reintroduce a second competing view by construction.
    by_id: dict[str, AcceptedImpact] = {}
    for entry in impact_result.affected:
        modeled_flow = None
        if entry.route_method and entry.route_path:
            modeled_flow = flow_by_route.get((entry.route_method.upper(), entry.route_path))
        impact_id = (
            modeled_flow.id if modeled_flow is not None
            else f"impact:{entry.repo}:{entry.qualified_name or entry.symbol}"
        )
        is_inferred = entry.status == IMPACT_STATUS_INFERRED
        # A human-facing label prefers the route, then the short symbol name
        # — never the raw qualified_name CBM produces, which carries a
        # checkout-path-derived project prefix (e.g.
        # "Users-name-repos-project.pkg.module.symbol") that means nothing
        # to a reader. `entry.label` (used for diagnostics/internal logging)
        # falls back to that full qualified_name; the product-facing report
        # deliberately does not.
        label = (
            f"{entry.route_method} {entry.route_path}"
            if entry.route_method and entry.route_path
            else (entry.symbol or entry.label)
        )
        existing = by_id.get(impact_id)
        if existing is not None and existing.status == IMPACT_STATUS_PROVEN:
            # PROVEN already dominates at this id; an inferred duplicate
            # contributes nothing further to the canonical entry.
            continue
        by_id[impact_id] = AcceptedImpact(
            id=impact_id, label=label, repo=entry.repo, kind=entry.kind,
            status=entry.status,
            changed_symbols=sorted(set(entry.changed_symbols)),
            route_method=entry.route_method, route_path=entry.route_path,
            llm_confidence=entry.llm_confidence if is_inferred else None,
            llm_reason=(entry.llm_reason or None) if is_inferred else None,
            llm_inference_type=(entry.llm_inference_type or None) if is_inferred else None,
            llm_uncertainty=(entry.llm_uncertainty or None) if is_inferred else None,
            corroborated=entry.corroborated if is_inferred else None,
            verification_model_status="modeled" if modeled_flow is not None else "unsupported_or_partial",
        )
    accepted_impacts = list(by_id.values())
    _trace_verification_decisions(accepted_impacts, affected_flows)
    return accepted_impacts


def _trace_verification_decisions(
    accepted_impacts: list[AcceptedImpact], affected_flows: list[AffectedFlow],
) -> None:
    """Serialize why each accepted impact was (or was not) modeled for
    verification — no new logic, just the `verification_model_status` and
    obligation count `_build_accepted_impacts` already computed."""
    if not _trace.is_enabled():
        return
    flows_by_id = {flow.id: flow for flow in affected_flows}
    for impact in accepted_impacts:
        flow = flows_by_id.get(impact.id)
        obligations = len(flow.obligations) if flow is not None else 0
        if impact.verification_model_status == "modeled":
            reason = "matched a modeled AffectedFlow with obligations derived from it"
        elif impact.route_method and impact.route_path:
            reason = (
                f"route {impact.route_method} {impact.route_path} did not match any "
                "modeled AffectedFlow (no obligations could be derived for it)"
            )
        else:
            reason = (
                "no route information available for this impact (non-HTTP behavior); "
                "no AffectedFlow/obligations exist for it"
            )
        _trace.record_verification_decision(
            impact_id=impact.id,
            label=impact.label,
            status=impact.status,
            verification_model_status=impact.verification_model_status,
            reason=reason,
            obligations=obligations,
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
        impacts_proven=sum(1 for item in result.accepted_impacts if item.status == IMPACT_STATUS_PROVEN),
        impacts_inferred=sum(1 for item in result.accepted_impacts if item.status == IMPACT_STATUS_INFERRED),
        impacts_not_modeled=sum(
            1 for item in result.accepted_impacts if item.verification_model_status != "modeled"
        ),
        unresolved_changed_symbols=result.unresolved_changed_symbols,
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

    # Three independent safety invariants over `accepted_impacts` — the
    # canonical "what did Sydes find" set — none of which the obligation
    # ladder above can see, since it only ever looks at `affected_flows`.
    # Gated once on the pre-existing verdict so every applicable reason is
    # still recorded even when more than one invariant fires at once —
    # checking `verdict == VERDICT_VERIFIED` freshly before each one would
    # let the first downgrade silently swallow the rest. None of these ever
    # upgrades or otherwise changes an already-worse verdict.
    if verdict == VERDICT_VERIFIED:
        if counts.impacts_not_modeled:
            # An accepted impact with no `AffectedFlow`/obligations has
            # nothing that could have been checked — VERIFIED would be a
            # claim about coverage that was never actually modeled,
            # regardless of how many *other*, modeled obligations passed.
            verdict = VERDICT_INCOMPLETE
            reasons.append(
                f"{counts.impacts_not_modeled} accepted impact(s) are not yet modeled for verification"
            )
        if counts.impacts_inferred:
            # LLM confidence is not verification proof (see
            # `sydes.impact.models` `IMPACT_STATUS_INFERRED`). A modeled
            # inferred flow's obligations may all pass, but the impact
            # itself is still a model's semantic guess, not a structurally
            # proven fact — VERIFIED must wait for that to become PROVEN,
            # not for its obligations alone to pass.
            verdict = VERDICT_INCOMPLETE
            reasons.append(
                f"{counts.impacts_inferred} affected impact(s) remain AI-inferred rather than "
                "structurally proven"
            )
        if counts.unresolved_changed_symbols:
            # A changed symbol the deterministic interpreter never connected
            # to any entrypoint is missing impact coverage, full stop — an
            # `ImpactCandidate` may have been proposed for it (see
            # `ChangeVerificationResult.unresolved_changed_symbols`), but a
            # candidate is not proof the symbol's real impact was found.
            verdict = VERDICT_INCOMPLETE
            reasons.append(
                f"{counts.unresolved_changed_symbols} changed symbol(s) have no established "
                "impact path (unresolved); full impact coverage cannot be claimed"
            )

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


# --------------------------------------------------------------------------
# Impact-interpreter wiring (backend=cbm only)
# --------------------------------------------------------------------------
#
# This section decides *which entrypoints* the diff reaches. It builds no
# flow, obligation, or verdict of its own — it produces the same kind of
# `EndpointCandidate` list the native reachability heuristic below already
# produces, and hands it to the identical downstream pipeline
# (`resolve_trace_target` -> `_trace_route` -> obligations -> evidence ->
# verdict). Native backend behavior is untouched; this only replaces how the
# candidate list is chosen when backend=cbm.


def _changed_symbols_for_impact(change: Any) -> list[dict[str, Any]]:
    """`ChangedSymbol` models, in the mapping shape `ImpactInterpreter` reads."""
    return [
        {
            "name": item.name,
            "file": item.file,
            "repo": item.repo,
            "qualified_name": item.qualified_name or "",
            "start_line": item.start_line,
        }
        for item in change.symbols
    ]


def _match_endpoint_candidate(
    entrypoint: Any, candidates: list[EndpointCandidate]
) -> EndpointCandidate | None:
    """The Sydes-discovered route matching one reconciled affected entrypoint.

    Tried by route method+path first (the identity that matters to a
    developer), then by handler symbol+file (the identity CBM and Sydes both
    derived from the same source). Neither is invented here: both are facts
    already present on the candidate list `discover_endpoints` produced.
    """
    if entrypoint.route_method and entrypoint.route_path:
        for candidate in candidates:
            if (
                (candidate.method or "").upper() == entrypoint.route_method.upper()
                and (candidate.path or "") == entrypoint.route_path
            ):
                return candidate
    for candidate in candidates:
        if candidate.file == entrypoint.file and candidate.handler == entrypoint.symbol:
            return candidate
    return None


def _build_impact_guide(options: VerifyChangeOptions) -> tuple[Any | None, list[str]]:
    """Build the M3 guide from the same LLM configuration `--code-review`
    already uses — no second provider system, no separate model flag.

    Returns `(None, notes)` for `impact_guide=off` or when no client could be
    built; the caller then runs `ImpactInterpreter` exactly as M2 did. A
    provider failure here is reported, never silently escalated to a strict
    error and never a reason to fall back to a guessed impact result.
    """
    if options.impact_guide == GUIDE_OFF:
        return None, []
    if options.llm_client is not None:
        return LLMImpactGuide(options.llm_client), []
    try:
        # No pinned temperature: some models reject an explicit value (e.g.
        # one observed rejecting 0.0, accepting only their own default), and
        # the guide's own request already sends `temperature=None` — the
        # client must be built the same way or its own default would still
        # override the request's.
        client = create_default_llm_client(
            model_spec=options.model_spec, temperature=None, stage="impact_guide",
        )
    except LLMClientError as exc:
        return None, [f"impact_guide unavailable: {exc}"]
    return LLMImpactGuide(client), []


def _semantic_ranking_texts(analysis: Any | None) -> list[str]:
    """Plain strings pulled from `pr_semantic_analysis` to *rank* the
    boundary-discovery frontier — never to create a candidate or an edge.
    Kept as flat text (not the pydantic model itself) so `sydes.impact`
    stays decoupled from `sydes.verify`'s types."""
    if analysis is None:
        return []
    texts: list[str] = []
    for change in analysis.behavior_changes:
        texts.append(change.description)
        texts.extend(change.changed_symbols)
    for hint in analysis.investigation_hints:
        texts.append(hint.description)
        texts.extend(hint.concepts)
        texts.extend(hint.related_symbols)
    texts.extend(analysis.likely_boundary_types)
    return [text for text in texts if text]


def _select_via_impact_interpreter(
    *,
    change: Any,
    routes: Any,
    structural: Any,
    repo_name: str,
    options: VerifyChangeOptions,
    repo_root: Path | None,
    semantic_analysis: Any | None = None,
) -> tuple[list[EndpointCandidate], ImpactResult, list[str]]:
    """Choose affected entrypoints through the impact interpreter.

    Returns the same `EndpointCandidate` shape the native path returns, so
    everything downstream of the caller is unaware anything changed.
    """
    guide, guide_notes = _build_impact_guide(options)
    changed = _changed_symbols_for_impact(change)
    interpreter = ImpactInterpreter(
        guide=guide,
        guide_policy=options.impact_guide,
        guide_budget=GuideBudget(),
        repo_root=repo_root,
    )
    impact_result = interpreter.interpret(
        changed, structural, repo=repo_name,
        semantic_texts=_semantic_ranking_texts(semantic_analysis),
    )
    reconciled = reconcile_entrypoints(impact_result.affected, structural.route_graph)
    # CBM's own route facts are pre-composition (router-relative — e.g.
    # "/{student_id}" rather than "/students/{student_id}");
    # `reconcile_entrypoints` is what corrects that against Sydes' composed
    # route graph. `impact_result.affected` must carry the *corrected*
    # entrypoints from here on — every downstream reader of `impact_result`
    # (diagnostics, and `_build_accepted_impacts` below) needs the same
    # composed paths `affected_flows` uses, or a route.method+path match
    # between them silently fails even though it is the same real route.
    impact_result.affected = reconciled

    selected: list[EndpointCandidate] = []
    seen: set[str] = set()
    for entrypoint in reconciled:
        if entrypoint.kind != "http_route":
            # Non-HTTP entrypoints (including non-HTTP INFERRED ones) have no
            # EndpointCandidate to map to — visible in `impact_result.affected`
            # and diagnostics, not in `affected_flows`. Unchanged from before
            # M4: this is the same boundary PROVEN non-HTTP entries already
            # had, not a new restriction on inferred ones.
            continue
        candidate = _match_endpoint_candidate(entrypoint, routes.routes)
        if candidate is None:
            continue
        dedupe_key = f"{candidate.method}:{candidate.path}:{candidate.file}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if entrypoint.status == IMPACT_STATUS_INFERRED:
            # `candidate` is the real, deterministically-discovered route the
            # inferred entrypoint matched — only its provenance tag changes;
            # its handler/file/method/path are exactly what deterministic
            # discovery already found, never anything the guide guessed.
            candidate = candidate.model_copy(update={"impact_status": IMPACT_STATUS_INFERRED})
        selected.append(candidate)
    return selected, impact_result, guide_notes


#: Prefix for the one analysis note a bounded/truncated structural
#: exploration produces. Used to keep that note unique per run.
_BOUNDED_EXPLORATION_NOTE_PREFIX = "Structural exploration was bounded"


def _graph_slice_seeds(change: ChangeSet, routes: Any) -> list[str]:
    """Seed symbols for the bounded slice: the changed symbols, plus the
    route handlers whose outbound calls flow tracing follows.

    Both consumers of the edge tables are represented. Missing either would
    change existing behavior rather than only its cost: the impact
    interpreter walks inbound from changed symbols, while
    `build_layered_trace_expansion` walks outbound from a route handler.
    Qualified and short names are both offered because CBM edges carry
    qualified names while a changed symbol often has only a short one.
    """
    seeds: list[str] = []
    for symbol in change.symbols:
        if symbol.qualified_name:
            seeds.append(symbol.qualified_name)
        if symbol.name:
            seeds.append(symbol.name)
    for route in getattr(routes, "routes", []) or []:
        handler = getattr(route, "handler", None)
        if handler:
            seeds.append(str(handler))
    return list(dict.fromkeys(item for item in seeds if item))


def _attach_bounded_graph_edges(
    *,
    code_intelligence: Any,
    structural: StructuralFacts,
    change: ChangeSet,
    routes: Any,
    result: ChangeVerificationResult,
) -> None:
    """Populate `structural.call_edges`/`usage_edges` from a bounded
    neighborhood, and record what that exploration could and could not see.

    A no-op for a backend that supplies no call graph or no bounded fetch
    (the native backend), which keeps its existing behavior exactly.
    """
    attach = getattr(code_intelligence, "attach_bounded_edges", None)
    if attach is None or not structural.provides_call_graph:
        return

    seeds = _graph_slice_seeds(change, routes)
    outcome = attach(structural, seed_symbols=seeds)

    result.diagnostics.append(
        f"graph_slice: used={outcome.used_slice} seeds={outcome.seed_count} "
        f"graph_calls={outcome.graph_calls} nodes={outcome.node_count} "
        f"call_edges={outcome.call_edge_count} usage_edges={outcome.usage_edge_count} "
        f"truncated={outcome.truncated} fell_back={outcome.fell_back}"
    )
    if outcome.reason:
        result.diagnostics.append(f"graph_slice_reason: {outcome.reason}")

    if not outcome.truncated:
        return

    # A bounded exploration means "nothing further was ESTABLISHED within
    # what was explored" — never "nothing further exists". Everything
    # already discovered stands unchanged; this only records that absence
    # of a further finding is not evidence of absence here.
    note = (
        f"{_BOUNDED_EXPLORATION_NOTE_PREFIX} for this change "
        f"({outcome.truncation_reason or 'a structural limit was reached'}); "
        "boundaries beyond the explored neighborhood are not established "
        "either way."
    )
    if not any(
        item.startswith(_BOUNDED_EXPLORATION_NOTE_PREFIX) for item in result.analysis_notes
    ):
        result.analysis_notes.append(note)


def _impact_diagnostics(impact_result: ImpactResult) -> list[str]:
    """Compact diagnostic lines summarising one impact-interpreter run."""
    metrics = impact_result.metrics
    lines = [
        "impact_interpreter: "
        + ", ".join(f"{key}={value}" for key, value in sorted(metrics.items())
                     if key not in {"max_depth", "max_visited"}),
    ]
    lines.extend(f"impact_note: {note}" for note in impact_result.notes)
    return lines


def _trace_impact_decisions(impact_result: ImpactResult) -> None:
    """Serialize every structural (PROVEN) discovery and every LLM candidate
    decision into `impact_decisions.jsonl` — no new judgment, only a
    reshaping of what `ImpactInterpreter` already recorded on
    `impact_result.affected`/`impact_result.llm_candidate_log`."""
    if not _trace.is_enabled():
        return
    for entry in impact_result.affected:
        if entry.status != IMPACT_STATUS_PROVEN:
            continue
        for changed_symbol in entry.changed_symbols or [""]:
            _trace.record_impact_decision(
                changed_symbol=changed_symbol,
                candidate_label=entry.label,
                kind=entry.kind,
                source="deterministic",
                status=entry.status,
                accepted=True,
                rejection_reason="",
                corroborated=None,
                confidence=None,
                reason="",
                evidence=entry.to_dict(),
            )
    for log_entry in impact_result.llm_candidate_log:
        accepted = bool(log_entry.get("accepted"))
        _trace.record_impact_decision(
            changed_symbol=log_entry.get("changed_symbol", ""),
            candidate_label=log_entry.get("candidate_entrypoint", ""),
            kind="",
            source="llm",
            status=IMPACT_STATUS_INFERRED if accepted else "rejected",
            accepted=accepted,
            rejection_reason=log_entry.get("rejection_reason", ""),
            corroborated=log_entry.get("corroborated"),
            confidence=log_entry.get("confidence"),
            reason=log_entry.get("rationale", ""),
            evidence=log_entry,
        )


def _to_affected_boundary(boundary: Any) -> AffectedBoundary:
    """Reshape one `impact.DiscoveredBoundary` into the product-facing
    `AffectedBoundary` — no re-derivation, only a smaller/JSON-simple view
    (the full `ImpactPath` becomes one human-readable evidence line)."""
    return AffectedBoundary(
        id=boundary.id,
        kind=boundary.kind,
        subtype=boundary.subtype,
        repo=boundary.repo,
        file=boundary.file,
        symbol=boundary.symbol,
        label=boundary.label,
        changed_symbols=sorted(set(boundary.changed_symbols)),
        evidence=[boundary.path.describe()] if boundary.path is not None else [],
        distance=boundary.distance,
        evidence_strength=boundary.evidence_strength,
        status=boundary.status,
    )


def _trace_boundary_decisions(impact_result: ImpactResult) -> None:
    """Serialize the bounded, decision-relevant boundary-discovery log
    (`ImpactResult.boundary_decisions`, already capped by
    `boundary_discovery.discover_boundaries`) into `impact_decisions.jsonl`
    — reuses the exact same trace category as structural/LLM impact
    decisions rather than inventing a second one."""
    if not _trace.is_enabled():
        return
    for decision in impact_result.boundary_decisions:
        accepted = decision.get("decision") == "emitted"
        _trace.record_impact_decision(
            changed_symbol=decision.get("changed_symbol", ""),
            candidate_label=decision.get("candidate", ""),
            kind=decision.get("kind") or "",
            source="boundary_discovery",
            status=IMPACT_STATUS_PROVEN if accepted else "rejected",
            accepted=accepted,
            rejection_reason="" if accepted else (decision.get("reason") or decision.get("decision") or ""),
            corroborated=None,
            confidence=None,
            reason=decision.get("reason", ""),
            evidence=decision,
        )


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

    trace_run_id = _trace.new_call_id("run")
    _trace.start_run(
        run_id=trace_run_id,
        options={
            "base": options.base,
            "include_working_tree": options.include_working_tree,
            "code_review": options.code_review,
            "llm_policy": options.llm_policy,
            "model_spec": options.model_spec,
            "run_tests": options.run_tests,
            "test_timeout_seconds": options.test_timeout_seconds,
            "impact_guide": options.impact_guide,
        },
        repos=[{"name": item.name, "root": item.root} for item in normalized_repos],
    )

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
    # Edges are deliberately NOT materialized here. A repository-wide
    # CALLS/USAGE sweep costs in proportion to total repository edge count
    # rather than change size, and every consumer of those edges starts from
    # a symbol this call has not resolved yet — `change.symbols` below needs
    # this call's own symbol index first. They are fetched as a bounded
    # neighborhood once the seeds are known (`_attach_bounded_graph_edges`).
    code_intelligence = get_code_intelligence()
    structural = code_intelligence.build_or_update(
        normalized_repos, workspace_id=workspace_id, defer_edges=True
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
    # --- PR-level semantic analysis (Increment A) -------------------------
    # A separate, complementary read of the change as a whole — never a
    # replacement for the structural analysis below, and never able to
    # affect it: `pr_semantic_analysis` is not read by anything past this
    # point. Runs whenever general LLM use is enabled (`--llm-policy`, the
    # same flag route discovery already checks) and works even when
    # `change.symbols` is empty (a language/indexing gap) — it reasons from
    # `change.files`/the diff either way.
    if options.llm_policy != "never":
        semantic_analysis, semantic_notes = generate_pr_semantic_analysis(
            change=change, repo_root=primary_root, model_spec=options.model_spec,
            llm_client=options.llm_client,
        )
        result.pr_semantic_analysis = semantic_analysis
        result.diagnostics.extend(semantic_notes)
        if semantic_analysis is None:
            for note in semantic_notes:
                if "unavailable" in note:
                    result.analysis_notes.append(
                        "PR semantic analysis unavailable: " + note.split(": ", 1)[-1]
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

    # --- bounded structural neighborhood ---------------------------------
    # Both seed sets are now known: the changed symbols (resolved above from
    # the symbol index) and the route handlers (just discovered). Fetch only
    # their neighborhood rather than the whole repository's edge tables.
    _attach_bounded_graph_edges(
        code_intelligence=code_intelligence, structural=structural,
        change=change, routes=routes, result=result,
    )

    # Which entrypoints the change reaches: the impact interpreter is the
    # primary source when CBM supplied a call graph, since it resolves
    # symbol identity exactly rather than through the native file-level
    # reverse-reach heuristic below. Native backend behavior is unchanged —
    # it never reaches this branch, and no fallback runs silently between
    # the two: the branch is decided once, by which backend answered.
    if structural.backend == CBM_BACKEND and structural.provides_call_graph:
        selected, impact_result, guide_notes = _select_via_impact_interpreter(
            change=change, routes=routes, structural=structural, repo_name=primary.name,
            options=options, repo_root=primary_root,
            semantic_analysis=result.pr_semantic_analysis,
        )
        result.diagnostics.extend(guide_notes)
        result.diagnostics.extend(_impact_diagnostics(impact_result))
        result.affected_boundaries = [
            _to_affected_boundary(item) for item in impact_result.boundaries
        ]
        _trace_impact_decisions(impact_result)
        _trace_boundary_decisions(impact_result)

        # --- Increment D: evidence-backed boundary inference ------------
        # One bounded LLM call over a compact packet of evidence already
        # gathered above, only for what deterministic discovery could not
        # ESTABLISH. Appended to the same `affected_boundaries` list under
        # `status="inferred"` — never fed into `accepted_impacts`,
        # `affected_flows`, obligations, or the verdict. Gated on the same
        # `--llm-policy` flag every other optional LLM pass already honors.
        if options.llm_policy != "never":
            # Increment B: a persisted, deterministic repository profile.
            # Built from manifests `repo_map` already located (no second
            # walk, no LLM call) and reused across runs; only a handful of
            # *retrieved* facts reach the packet below. Never load-bearing —
            # `None` here simply means boundary reasoning behaves exactly as
            # it did before profiles existed.
            repo_profile, profile_notes = get_or_build_repo_profile(
                repo_root=primary_root, repo_identity=primary.name,
                workspace_id=workspace_id, observed_commit=change.head,
                repo_map=structural.repo_map,
            )
            result.diagnostics.extend(profile_notes)
            inferred_boundaries, boundary_notes = infer_boundaries(
                change=change, impact_result=impact_result,
                deterministic_boundaries=result.affected_boundaries,
                semantic_analysis=result.pr_semantic_analysis,
                facts=structural, repo=primary.name, repo_root=primary_root,
                repo_profile=repo_profile,
                model_spec=options.model_spec, llm_client=options.llm_client,
            )
            result.affected_boundaries.extend(inferred_boundaries)
            result.diagnostics.extend(boundary_notes)
            for note in boundary_notes:
                if "unavailable" in note:
                    result.analysis_notes.append(
                        "AI boundary inference unavailable: " + note.split(": ", 1)[-1]
                    )
        # Provider/guide failures must be visible in the human-readable
        # report, not only countable in diagnostics — `analysis_notes` is
        # the section every renderer already shows by default, unlike
        # `diagnostics` (verbose-only). Deterministic analysis has already
        # run by this point regardless, so the report can say both things
        # are true at once: it ran, and AI inference may be incomplete.
        for note in guide_notes:
            if "impact_guide unavailable" in note:
                result.analysis_notes.append(f"AI impact inference unavailable: {note.split(': ', 1)[-1]}")
        guide_error_details = impact_result.metrics.get("guide_error_details") or []
        if guide_error_details:
            result.analysis_notes.append(
                f"AI impact inference: {len(guide_error_details)} guide call(s) failed "
                f"mid-run — {guide_error_details[-1]}"
            )
        if guide_notes or guide_error_details:
            result.analysis_notes.append(
                "Deterministic impact analysis still ran and is reflected below; "
                "AI-inferred impact coverage may be incomplete."
            )
        if impact_result.completeness != COMPLETENESS_COMPLETE:
            # A truncated traversal may hide a real entrypoint; the existing
            # verdict logic already refuses VERIFIED whenever analysis_status
            # is not complete, so this alone is enough to keep an incomplete
            # impact result from ever reading as VERIFIED.
            result.analysis_status = ANALYSIS_PARTIAL
            result.analysis_notes.append(
                "Impact analysis traversal was truncated by its bounds; some "
                "reachable entrypoints may not be listed."
            )
        if impact_result.unresolved:
            # `completeness` only reflects traversal truncation — a
            # *complete* traversal can still leave changed symbols with no
            # entrypoint path at all (nothing truncated the search; there
            # was simply nothing to find, or the guide only managed an
            # INFERRED candidate rather than a proven path). That gap must
            # not silently read as full impact coverage, so it is recorded
            # here on the canonical result (`_compute_summary` reads
            # `result.unresolved_changed_symbols` directly) rather than only
            # ever appearing in a diagnostics line.
            result.unresolved_changed_symbols = len(impact_result.unresolved)
            result.analysis_status = ANALYSIS_PARTIAL
            result.analysis_notes.append(
                f"{len(impact_result.unresolved)} changed symbol(s) have no established "
                "impact path to any entrypoint (an AI-inferred candidate may still exist "
                "for one — see AFFECTED BEHAVIOR — but that is not the same as resolved "
                "impact coverage)."
            )
    else:
        impact_result = None
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
    if structural.backend == CBM_BACKEND and structural.provides_call_graph:
        # The reachability check below (`reached = ...`) predates the impact
        # interpreter and only trusts a handler that IS the changed symbol, a
        # handler whose declaration line the diff touched, or a call the
        # native follower resolved. A route reached through
        # DECORATOR_REFERENCE or USAGE_REFERENCE satisfies none of those —
        # the dependency was never called, only named in a decorator
        # argument — so without this the impact interpreter's own findings
        # would be silently discarded by a gate that cannot see its evidence.
        # This does not relax the gate or duplicate it: it feeds the same
        # (file, symbol) vocabulary the gate already checks, using paths the
        # interpreter already proved rather than re-deriving reachability.
        for entrypoint in impact_result.affected:
            changed_symbol_keys.add((entrypoint.file, entrypoint.symbol))
            if entrypoint.qualified_name:
                changed_symbol_keys.add((entrypoint.file, entrypoint.qualified_name))
    changed_hunks = {
        item.path: [(hunk.start_line, hunk.end_line) for hunk in item.hunks]
        for item in change.files
    }

    candidate_endpoints = selected[:MAX_FLOWS * 3]
    max_flows_cap_hit = False
    max_flows_cap_remaining = 0
    for _cap_index, endpoint in enumerate(candidate_endpoints):
        if len(result.affected_flows) >= MAX_FLOWS:
            max_flows_cap_hit = True
            max_flows_cap_remaining = len(candidate_endpoints) - _cap_index
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
            # `endpoint`, not `resolved_endpoint`: `resolve_trace_target` may
            # substitute a same-route object from `routes.routes` that never
            # carried the tag `_select_via_impact_interpreter` set — the
            # provenance belongs to the candidate M4 actually selected.
            impact_status=endpoint.impact_status or "proven",
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

    if max_flows_cap_hit:
        # `selected` had more route candidates than `MAX_FLOWS` could model
        # this run — the ones left over stay recorded in `accepted_impacts`
        # (via `verification_model_status="unsupported_or_partial"`, which
        # already keeps VERIFIED out of reach) but never became a verification
        # flow at all. That must not read as silently-complete coverage.
        result.diagnostics.append(
            f"max_flows_cap: modeled {len(result.affected_flows)} candidate route(s) "
            f"(cap={MAX_FLOWS}); {max_flows_cap_remaining} left unmodeled by the cap"
        )
        result.analysis_notes.append(
            f"Verification-flow modeling is capped at {MAX_FLOWS} per run; "
            f"{max_flows_cap_remaining} additional candidate route(s) were not modeled "
            "and remain recorded as accepted impact only, without obligations."
        )

    if any(flow.analysis_status != ANALYSIS_COMPLETE for flow in result.affected_flows):
        result.analysis_status = ANALYSIS_PARTIAL
    if selected and not result.affected_flows:
        result.analysis_status = ANALYSIS_UNKNOWN

    # --- runtime dependencies, then execution ----------------------------
    result.runtime_dependencies = infer_runtime_dependencies(
        files=repo_files, flows=result.affected_flows, changed_files=changed_files
    )
    _run_test_execution(result, options, repo_files, primary_root, changed_files)

    for flow in result.affected_flows:
        resolve_flow_status(flow)
    result.accepted_impacts = _build_accepted_impacts(impact_result, result.affected_flows)
    result.summary = _compute_summary(result)
    _trace.record_final_decision(
        run_id=trace_run_id,
        risk=result.summary.risk,
        verdict=result.summary.verdict,
        headline=result.summary.headline,
        counts=result.summary.counts,
        reasons=result.summary.risk_reasons,
    )
    return result


def _trace_test_decision(flow: AffectedFlow, obligation: VerificationObligation) -> None:
    """Serialize one obligation's test-mapping/execution outcome — no new
    logic, just the `mapped_tests`/`supporting_tests`/`status`/`reason`
    `resolve_obligation_status` (or the `--no-run-tests` short-circuit just
    above it) already set."""
    if not _trace.is_enabled():
        return
    _trace.record_test_decision(
        flow_id=flow.id,
        obligation_id=obligation.id,
        obligation_description=obligation.statement,
        mapped_tests=[test.name for test in obligation.mapped_tests],
        supporting_tests=[test.name for test in obligation.supporting_tests],
        status=obligation.status,
        reason=obligation.reason,
    )


def _run_test_execution(
    result: ChangeVerificationResult,
    options: VerifyChangeOptions,
    repo_files,
    repo_root: Path,
    changed_files: set[str],
) -> None:
    """Run the repository's own test suite once and resolve obligations from it."""
    settings = ExecutionSettings(
        enabled=options.run_tests, timeout_seconds=options.test_timeout_seconds
    )
    ci_suite, notes = run_ci_suite(
        files=repo_files, repo_root=repo_root, settings=settings,
        changed_files=frozenset(changed_files),
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
                _trace_test_decision(flow, obligation)
                continue
            resolve_obligation_status(obligation, ci_suite)
            _trace_test_decision(flow, obligation)
