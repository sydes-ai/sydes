"""Orchestrates the recovery pipeline: discover a candidate, prove each
atomic relationship independently (recursing into intermediate entities
when a direct proof fails), verify, compose -- path recovery and test
recovery as two fully independent outcomes.

CBM is a helper here, not the authority: the pipeline starts from the first
pass's own hypothesis (the CBM/structural fragments in `RecoveryContext`)
but is explicitly told it may inspect any file in the repository, not only
ones CBM already surfaced, and that it may correct — not merely extend —
the first pass's hypothesis if source evidence contradicts it.

Four stages, each independently testable:
  A. `sydes.recovery.discovery` proposes a candidate path/tests — a
     hypothesis only, no evidence-citation responsibility.
  B. `sydes.recovery.evidence` proves (or fails to prove) each adjacent
     pair in the candidate path, and each candidate test, ONE AT A TIME —
     never the whole path in one call. When a direct proof finds nothing,
     exactly one recursive decomposition attempt asks for a small,
     source-backed chain of intermediate entities, and each resulting hop
     is then proven directly (no further recursion).
  C. `sydes.recovery.verify` independently, adversarially judges each
     atomic claim's evidence AND canonical identity — path and test
     verification never share state or outcome.
  D. This module composes the verified atomic facts into a final
     `RecoveryResult` (`path_recovery` + `test_recovery`, each
     independently `established`/`partial`/`unresolved`) via the
     shortest-sufficient-path truncation `sydes.recovery.verify` already
     implements. The full internal proof chain (including any nodes a
     recursive decomposition inserted) is retained; nothing is collapsed
     before or during verification.

A failure to establish or even to complete path recovery must never
discard test recovery, and vice versa — see `recover`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sydes.llm.client import LLMClient
from sydes.recovery.context import RecoveryContext
from sydes.recovery.discovery import discover_candidate
from sydes.recovery.entrypoint_heuristic import find_declarative_entrypoints
from sydes.recovery.evidence import decompose_relationship, prove_relationship, prove_test_claim
from sydes.recovery.graph_path import propose_graph_path
from sydes.recovery.graph_tools import CBMGraphTools
from sydes.recovery.schema import (
    EntityRef,
    MAX_BRIDGE_NODES,
    PathRecoveryResult,
    ROOT_VERIFIED_BOUNDARY,
    RecoveredEdge,
    RecoveredEvidence,
    RecoveredPath,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    TestRecoveryResult,
    recompute_test_status,
)
from sydes.recovery.tools import RepoTools, ToolCallRecord
from sydes.recovery.verify import verify_paths, verify_tests


@dataclass(frozen=True)
class RecoveryBudget:
    """A hard ceiling on the recovery run — "start simple", not a routing
    policy.

    `max_discovery_turns` bounds Stage A's tool-call turns. `max_edge_turns`
    bounds EACH Stage B atomic-proof/decompose call (deliberately smaller
    than discovery's budget — a focused single-relationship search needs
    fewer turns than ranging over the whole repository).
    `max_bridge_nodes` caps how many intermediate entities one recursive
    decomposition may insert. `max_pipeline_retries` bounds how many times
    the WHOLE pipeline (fresh discovery + fresh evidence completion) may be
    retried after a non-established outcome — see `recover`. Recursive
    decomposition itself is capped at exactly one attempt per originally-
    unproven relationship, enforced in `_prove_hop_with_recursion` (not
    configurable — "one attempt" is a correctness property, not a tuning
    knob).

    `max_edge_retries` bounds how many additional, fresh attempts a SINGLE
    edge gets after both the direct proof and recursive decomposition
    still leave it with no evidence at all — same `prove_relationship`
    call, same prompt, no new reasoning strategy, just another try (an
    LLM call is not guaranteed to search the same way twice). Zero by
    default (no behavior change for existing callers). This is a search-
    capacity knob, not a correctness property like decomposition's cap:
    raising it never changes what counts as evidence or how an edge is
    judged — `sydes.recovery.verify`'s Layer 0/1/2 pipeline is identical
    either way, and a retry that still finds nothing is reported exactly
    like a single unresolved attempt, never treated as weaker or
    stronger evidence for having been retried.
    """

    max_discovery_turns: int = 6
    max_edge_turns: int = 4
    max_bridge_nodes: int = MAX_BRIDGE_NODES
    max_pipeline_retries: int = 1
    max_edge_retries: int = 0
    max_response_chars: int = 4000


@dataclass
class RecoveryRunStats:
    """Cost/latency/tool-usage accounting for one `recover()` call,
    accumulated across every stage (discovery + every atomic proof,
    including any recursive decomposition + verification) — the
    evaluation harness's primary output alongside the `RecoveryResult`
    itself."""

    turns: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    llm_calls: int = 0
    verify_retries: int = 0
    pipeline_retries: int = 0
    files_read: list[str] = field(default_factory=list)
    recursive_decompositions_attempted: int = 0
    recursive_decompositions_used: int = 0
    #: How many candidate paths `sydes.recovery.graph_path.propose_graph_path`
    #: proposed (one attempt per changed symbol, see `recover`), and how
    #: many of those the SAME `verify_paths` call every other candidate
    #: path goes through actually established -- zero LLM calls spent on
    #: either number beyond the one shared Layer-2 batch judging every
    #: candidate edge (graph-proposed and LLM-proposed alike) together.
    graph_paths_proposed: int = 0
    graph_paths_established: int = 0
    #: Of `graph_paths_established` above, how many have a root that is
    #: only `ROOT_CANDIDATE_BOUNDARY` (see `sydes.recovery.graph_path`'s
    #: topology fallback and `sydes.recovery.schema`) -- a real, fully
    #: edge-proven chain, but to an upstream root CBM's graph shape
    #: suggested rather than one already known (an HTTP route, an
    #: established flow). Never fold this into `graph_paths_established`
    #: without also surfacing this count: an established chain with an
    #: unverified root is not the same claim as reaching a real,
    #: already-known entrypoint.
    graph_paths_established_unverified_boundary: int = 0
    #: How many single-edge retries (see `RecoveryBudget.max_edge_retries`)
    #: were actually attempted, and how many of those turned a no-evidence
    #: edge into one with evidence -- `attempted - succeeded` is exactly
    #: how many retries changed nothing.
    edge_retries_attempted: int = 0
    edge_retries_succeeded: int = 0


@dataclass
class RecoveryOutcome:
    """What `recover()` returns: path recovery and test recovery as two
    fully independent results, plus the stats needed to judge whether this
    prototype is worth integrating. Neither result's status or content
    depends on the other's."""

    path_recovery: PathRecoveryResult
    test_recovery: TestRecoveryResult
    stats: RecoveryRunStats
    trigger_reason: str


def _status_rank(status: str) -> int:
    return {STATUS_ESTABLISHED: 2, STATUS_PARTIAL: 1, STATUS_UNRESOLVED: 0}.get(status, 0)


def _prove_once_more_if_still_unresolved(
    edge: RecoveredEdge, from_entity: EntityRef, to_entity: EntityRef, path_context: str, *,
    tools: RepoTools, client: LLMClient, budget: RecoveryBudget, stats: RecoveryRunStats,
) -> RecoveredEdge:
    """`RecoveryBudget.max_edge_retries` applied to one already-attempted
    edge: if it still has no evidence at all, try `prove_relationship`
    again, up to that many additional times, the exact same call every
    time — no new strategy, just another roll. Stops at the first attempt
    that finds evidence; returns the last attempt (still empty) if none
    do. A no-op (returns `edge` unchanged) when the edge already has
    evidence or the budget is zero."""
    current = edge
    for _ in range(max(0, budget.max_edge_retries)):
        if current.evidence:
            break
        stats.edge_retries_attempted += 1
        retry = prove_relationship(
            from_entity, to_entity, path_context, tools=tools, client=client,
            max_turns=budget.max_edge_turns, max_response_chars=budget.max_response_chars, stats=stats,
        )
        if retry.evidence:
            stats.edge_retries_succeeded += 1
        current = retry
    return current


def _prove_hop_with_recursion(
    from_entity: EntityRef, to_entity: EntityRef, path_context: str, *, tools: RepoTools, client: LLMClient,
    budget: RecoveryBudget, stats: RecoveryRunStats,
) -> list[RecoveredEdge]:
    """One candidate-path hop, possibly expanded into several proven
    sub-edges. Recursion happens at most once: if the direct
    `prove_relationship(from_entity, to_entity, ...)` finds no evidence at
    all, one `decompose_relationship` call is tried; each hop in whatever
    chain it returns is then proven directly (never decomposed further).
    If decomposition finds nothing (or is itself unproductive), the
    original, unresolved direct edge is returned as-is — never fabricated,
    never silently dropped.

    `budget.max_edge_retries` (see that field's docstring) applies AFTER
    this — to the direct edge when decomposition never runs or finds
    nothing, and to each decomposed sub-edge that still comes back with no
    evidence — never in place of decomposition, only after it has already
    had its one attempt.
    """
    direct = prove_relationship(
        from_entity, to_entity, path_context, tools=tools, client=client,
        max_turns=budget.max_edge_turns, max_response_chars=budget.max_response_chars, stats=stats,
    )
    if direct.evidence:
        return [direct]

    stats.recursive_decompositions_attempted += 1
    intermediates = decompose_relationship(
        from_entity, to_entity, path_context, tools=tools, client=client,
        max_turns=budget.max_edge_turns, max_response_chars=budget.max_response_chars, stats=stats,
    )
    intermediates = intermediates[: budget.max_bridge_nodes]
    if not intermediates:
        direct = _prove_once_more_if_still_unresolved(
            direct, from_entity, to_entity, path_context,
            tools=tools, client=client, budget=budget, stats=stats,
        )
        return [direct]

    stats.recursive_decompositions_used += 1
    chain = [from_entity, *intermediates, to_entity]
    edges: list[RecoveredEdge] = []
    for a, b in zip(chain, chain[1:]):
        sub_context = f"{path_context} (intermediate hop found via recursive decomposition)"
        edge = prove_relationship(
            a, b, sub_context, tools=tools, client=client,
            max_turns=budget.max_edge_turns, max_response_chars=budget.max_response_chars, stats=stats,
        )
        edge = _prove_once_more_if_still_unresolved(
            edge, a, b, sub_context, tools=tools, client=client, budget=budget, stats=stats,
        )
        edges.append(edge.model_copy(update={"from_decomposition": True}))
    return edges


def _direct_entrypoint_edge(node: EntityRef, repo_root: Path) -> RecoveredEdge | None:
    """When discovery collapses a candidate path to a single node (its
    honest way of saying the entrypoint's own decorator sits directly on
    the changed symbol -- no intermediate hop exists at all, see
    `sydes.recovery.schema._parse_candidate_path`), don't ask Stage B to
    "prove" a relationship between a symbol and itself. Confirm it the
    same deterministic way `sydes.recovery.context` found it in the first
    place, or return None (never fabricate) if it can't be confirmed here.

    This still goes through the ordinary `sydes.recovery.verify` Layer
    0/1/2 pipeline like any other edge -- it is a normal `RecoveredEdge`
    with real, inspectable evidence, not a special-cased bypass.
    """
    if not node.file:
        return None
    try:
        found = find_declarative_entrypoints(repo_root, [node.file])
    except OSError:
        return None
    # `node.symbol` may be class-qualified (e.g. `SomeClass.someMethod`, the
    # convention discovery otherwise uses throughout this pipeline) while
    # the heuristic's regex-based extraction only ever captures the bare
    # method name -- match on the last segment, not exact equality.
    bare_symbol = node.symbol.rsplit(".", 1)[-1]
    match = next((ep for ep in found if ep.symbol == bare_symbol), None)
    if match is None:
        return None
    evidence = [RecoveredEvidence(
        file=node.file, line_start=match.line, line_end=match.line,
        fact=(
            f"{node.symbol} carries a {match.http_method}-shaped declarative-entrypoint "
            f"decorator (path={match.path!r}) directly on its own declaration."
        ),
    )]
    return RecoveredEdge(
        **{"from": node, "to": node},
        relationship=(
            "the entrypoint's own decorator is declared directly on this symbol; "
            "no intermediate hop connects them because none exists"
        ),
        evidence=evidence,
    )


def _run_pipeline_once(
    context: RecoveryContext, *, tools: RepoTools, client: LLMClient, budget: RecoveryBudget,
    stats: RecoveryRunStats, repo_root: Path, graph: CBMGraphTools, use_graph_path_search: bool = False,
) -> tuple[list[RecoveredPath], list]:
    """Stages A + B (draft assembly). Stage C (verification) is applied
    once by the caller, separately for paths and for tests.

    `use_graph_path_search`, when on, tries a deterministic candidate path
    per changed symbol FIRST (see `sydes.recovery.graph_path`) -- built
    entirely from CBM's already-indexed graph, no LLM call spent proposing
    or proving it. Every graph-proposed path is added to `draft_paths`
    alongside whatever the LLM discovery loop below separately proposes;
    both go through the exact same `verify_paths` call downstream (in
    `recover`), batched into the same Layer-2 judging pass -- a graph
    proposal is never trusted more or less than an LLM one just because of
    where it came from, and it costs nothing extra if it turns out
    unhelpful (Layer 0/1 reject it before any LLM call, same as any other
    edge that fails those cheap checks).
    """
    draft_paths: list[RecoveredPath] = []
    if use_graph_path_search:
        entrypoints = list(context.entrypoint_entities)
        for target in context.changed_symbol_entities:
            graph_path = propose_graph_path(entrypoints, target, graph=graph, tools=tools)
            if graph_path is not None:
                stats.graph_paths_proposed += 1
                draft_paths.append(graph_path)

    candidate_path, candidate_tests = discover_candidate(
        context, tools=tools, client=client,
        max_turns=budget.max_discovery_turns, max_response_chars=budget.max_response_chars, stats=stats,
    )

    if candidate_path is not None:
        # Shortest-sufficient-path applied at evidence-completion time, not
        # only at final composition: nothing past target_node is proven at
        # all (verify would drop it anyway), so there is no reason to
        # spend an atomic-completion call on it.
        target_index = next(i for i, n in enumerate(candidate_path.nodes) if n.symbol == candidate_path.target_node)
        relevant_nodes = candidate_path.nodes[: target_index + 1]

        expanded_nodes: list[EntityRef] = [relevant_nodes[0]]
        all_edges: list[RecoveredEdge] = []
        if len(relevant_nodes) == 1:
            direct_edge = _direct_entrypoint_edge(relevant_nodes[0], repo_root)
            if direct_edge is not None:
                all_edges.append(direct_edge)
        else:
            path_ctx = f"hop in candidate path from entrypoint {candidate_path.entrypoint!r} to changed behavior {candidate_path.target_node!r}"
            for from_entity, to_entity in zip(relevant_nodes, relevant_nodes[1:]):
                hop_edges = _prove_hop_with_recursion(
                    from_entity, to_entity, path_ctx, tools=tools, client=client, budget=budget, stats=stats,
                )
                for edge in hop_edges:
                    expanded_nodes.append(edge.to_entity)
                    all_edges.append(edge)

        draft_paths.append(
            RecoveredPath(
                entrypoint=candidate_path.entrypoint, target_node=candidate_path.target_node,
                nodes=expanded_nodes, edges=all_edges,
            )
        )

    draft_tests = [
        prove_test_claim(
            candidate,
            candidate.target,
            "candidate test proposed during discovery for this change's missing test mapping",
            tools=tools, client=client, max_turns=budget.max_edge_turns,
            max_response_chars=budget.max_response_chars, stats=stats,
        )
        for candidate in candidate_tests
    ]

    return draft_paths, draft_tests


def recover(
    result_context: RecoveryContext,
    *,
    repo_root: Path,
    client: LLMClient,
    trigger_reason: str,
    budget: RecoveryBudget | None = None,
    use_graph_path_search: bool = False,
) -> RecoveryOutcome:
    """Run the full discover -> prove (with recursion) -> verify -> compose
    pipeline, with at most one full-pipeline retry if PATH recovery does
    not reach `established`. Test recovery has no separate retry of its
    own in this pass — it already reaches the correct answer reliably
    without one; see the module docstring.

    Path recovery and test recovery are verified completely independently
    (`verify_paths`/`verify_tests`) and neither result depends on or is
    discarded by the other's outcome.

    `use_graph_path_search` (see `sydes.recovery.graph_path`, off by
    default) is tried only on the FIRST pipeline attempt, never on a
    pipeline retry -- it is deterministic (same repo state, same inputs,
    same CBM graph), so retrying it would spend CBM calls to reproduce the
    identical result rather than find anything new.

    Never raises on a clean failure path: the CLI's AI-recovery hook (on by
    default, opt out with `--no-ai-recovery`) is responsible for catching
    `sydes.recovery.schema.RecoveryError`
    and leaving the first-pass result untouched. A failure partway through
    Stage B for one edge/test does not fail the run — see
    `sydes.recovery.evidence`, which treats its own failure as "no evidence
    found" for that one atomic claim.
    """
    active_budget = budget or RecoveryBudget()
    graph = CBMGraphTools(repo_root)
    tools = RepoTools(repo_root, graph=graph)
    stats = RecoveryRunStats()
    changed_files = frozenset(result_context.changed_files)

    try:
        draft_paths, draft_tests = _run_pipeline_once(
            result_context, tools=tools, client=client, budget=active_budget, stats=stats, repo_root=repo_root,
            graph=graph, use_graph_path_search=use_graph_path_search,
        )
        path_recovery = verify_paths(draft_paths, changed_files=changed_files, tools=tools, client=client, stats=stats)
        graph_established_paths = [
            p for p in path_recovery.paths[: stats.graph_paths_proposed] if p.status == STATUS_ESTABLISHED
        ]
        stats.graph_paths_established = len(graph_established_paths)
        stats.graph_paths_established_unverified_boundary = sum(
            1 for p in graph_established_paths if p.root_boundary_status != ROOT_VERIFIED_BOUNDARY
        )
        test_recovery = verify_tests(draft_tests, changed_files=changed_files, tools=tools, client=client, stats=stats)

        while (
            path_recovery.status != STATUS_ESTABLISHED
            and stats.pipeline_retries < active_budget.max_pipeline_retries
        ):
            stats.pipeline_retries += 1
            retry_paths, retry_tests = _run_pipeline_once(
                result_context, tools=tools, client=client, budget=active_budget, stats=stats, repo_root=repo_root,
                graph=graph,  # use_graph_path_search left False: deterministic, no point repeating it
            )
            retry_path_recovery = verify_paths(retry_paths, changed_files=changed_files, tools=tools, client=client, stats=stats)
            if _status_rank(retry_path_recovery.status) > _status_rank(path_recovery.status):
                path_recovery = retry_path_recovery
            # A retry's tests are additive evidence for the SAME independent
            # test-recovery outcome, never a replacement -- a path retry must
            # not discard tests already established on the first attempt.
            if retry_tests:
                retry_test_recovery = verify_tests(retry_tests, changed_files=changed_files, tools=tools, client=client, stats=stats)
                existing_keys = {(t.file, t.test) for t in test_recovery.tests}
                new_tests = [t for t in retry_test_recovery.tests if (t.file, t.test) not in existing_keys]
                if new_tests:
                    test_recovery = recompute_test_status(
                        TestRecoveryResult(tests=test_recovery.tests + new_tests)
                    )
    finally:
        graph.close()

    stats.tool_calls = list(tools.calls)
    return RecoveryOutcome(path_recovery=path_recovery, test_recovery=test_recovery, stats=stats, trigger_reason=trigger_reason)
