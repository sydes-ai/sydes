"""Orchestrates the recovery pipeline: discover a candidate, prove each
atomic relationship independently, verify, compose.

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
     never the whole path in one call.
  C. `sydes.recovery.verify` (unchanged) independently, adversarially
     judges each atomic claim's evidence.
  D. This module composes the verified atomic facts into a final
     `RecoveryResult` via the same shortest-sufficient-path truncation
     `sydes.recovery.verify` already implements.

Splitting discovery from evidence completion is the fix for a real,
observed weakness: one agent asked to range broadly over a whole
repository AND perfectly cite evidence for every hop in the same pass
tended to cite the nearest plausible-looking lines rather than the ones
that actually prove a given hop. A focused, single-relationship prover has
one job and nothing else competing for its attention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sydes.llm.client import LLMClient
from sydes.recovery.context import RecoveryContext
from sydes.recovery.discovery import discover_candidate
from sydes.recovery.evidence import prove_relationship, prove_test_claim
from sydes.recovery.schema import (
    RecoveredNode,
    RecoveredPath,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
)
from sydes.recovery.tools import RepoTools, ToolCallRecord
from sydes.recovery.verify import verify_recovery_result


@dataclass(frozen=True)
class RecoveryBudget:
    """A hard ceiling on the recovery run — "start simple", not a routing
    policy.

    `max_discovery_turns` bounds Stage A's tool-call turns. `max_edge_turns`
    bounds EACH Stage B atomic-proof call (deliberately smaller than
    discovery's budget — a focused single-relationship search needs fewer
    turns than ranging over the whole repository). `max_pipeline_retries`
    bounds how many times the WHOLE pipeline (fresh discovery + fresh
    evidence completion) may be retried after a non-established outcome —
    see `recover`.
    """

    max_discovery_turns: int = 6
    max_edge_turns: int = 4
    max_pipeline_retries: int = 1
    max_response_chars: int = 4000


@dataclass
class RecoveryRunStats:
    """Cost/latency/tool-usage accounting for one `recover()` call,
    accumulated across every stage (discovery + every atomic proof +
    verification) — the evaluation harness's primary output alongside the
    `RecoveryResult` itself."""

    turns: int = 0
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    llm_calls: int = 0
    verify_retries: int = 0
    pipeline_retries: int = 0
    files_read: list[str] = field(default_factory=list)


@dataclass
class RecoveryOutcome:
    """What `recover()` returns: the (possibly verify-downgraded) result,
    plus the stats needed to judge whether this prototype is worth
    integrating."""

    result: RecoveryResult
    stats: RecoveryRunStats
    trigger_reason: str


def _status_rank(status: str) -> int:
    return {STATUS_ESTABLISHED: 2, STATUS_PARTIAL: 1, STATUS_UNRESOLVED: 0}.get(status, 0)


def _run_pipeline_once(
    context: RecoveryContext, *, tools: RepoTools, client: LLMClient, budget: RecoveryBudget, stats: RecoveryRunStats,
) -> RecoveryResult:
    """Stages A + B + (draft assembly). Stage C (verification) is applied
    once by the caller across everything this produces."""
    candidate_path, candidate_tests = discover_candidate(
        context, tools=tools, client=client,
        max_turns=budget.max_discovery_turns, max_response_chars=budget.max_response_chars, stats=stats,
    )

    draft_paths: list[RecoveredPath] = []
    if candidate_path is not None:
        # Shortest-sufficient-path applied at evidence-completion time, not
        # only at final composition: nothing past target_node is proven at
        # all (`sydes.recovery.verify` would drop it anyway), so there is
        # no reason to spend an atomic-completion call on it.
        target_index = candidate_path.nodes.index(candidate_path.target_node)
        relevant_nodes = candidate_path.nodes[: target_index + 1]

        edges = []
        for from_symbol, to_symbol in zip(relevant_nodes, relevant_nodes[1:]):
            edge = prove_relationship(
                from_symbol, to_symbol,
                f"hop in candidate path from entrypoint {candidate_path.entrypoint!r} to changed behavior {candidate_path.target_node!r}",
                tools=tools, client=client,
                max_turns=budget.max_edge_turns, max_response_chars=budget.max_response_chars, stats=stats,
            )
            edges.append(edge)
        nodes = [
            RecoveredNode(symbol=symbol, file=_file_for_node(symbol, edges))
            for symbol in relevant_nodes
        ]
        draft_paths.append(
            RecoveredPath(
                entrypoint=candidate_path.entrypoint, target_node=candidate_path.target_node,
                nodes=nodes, edges=edges,
            )
        )

    draft_tests = [
        prove_test_claim(
            candidate, "candidate test proposed during discovery for this change's missing test mapping",
            tools=tools, client=client, max_turns=budget.max_edge_turns,
            max_response_chars=budget.max_response_chars, stats=stats,
        )
        for candidate in candidate_tests
    ]

    return RecoveryResult(status=STATUS_UNRESOLVED, recovered_paths=draft_paths, recovered_tests=draft_tests)


def _file_for_node(symbol: str, edges: list) -> str:
    """Best-effort display file for a node — cosmetic only; correctness
    lives entirely in the edges' own evidence, never in this guess."""
    for edge in edges:
        if edge.from_symbol == symbol or edge.to_symbol == symbol:
            for item in edge.evidence:
                if item.file:
                    return item.file
    return ""


def recover(
    result_context: RecoveryContext,
    *,
    repo_root: Path,
    client: LLMClient,
    trigger_reason: str,
    budget: RecoveryBudget | None = None,
) -> RecoveryOutcome:
    """Run the full discover -> prove -> verify -> compose pipeline, with
    at most one full-pipeline retry if the first attempt does not reach
    `established`. Never more than that.

    Never raises on a clean failure path: the CLI's opt-in `--ai-recovery`
    hook is responsible for catching `sydes.recovery.schema.RecoveryError`
    and leaving the first-pass result untouched. A failure partway through
    Stage B for one edge/test does not fail the run — see
    `sydes.recovery.evidence`, which treats its own failure as "no evidence
    found" for that one atomic claim.
    """
    active_budget = budget or RecoveryBudget()
    tools = RepoTools(repo_root)
    stats = RecoveryRunStats()

    draft = _run_pipeline_once(result_context, tools=tools, client=client, budget=active_budget, stats=stats)
    verified = verify_recovery_result(draft, tools=tools, client=client, stats=stats)

    while (
        verified.status != STATUS_ESTABLISHED
        and stats.pipeline_retries < active_budget.max_pipeline_retries
    ):
        stats.pipeline_retries += 1
        retry_draft = _run_pipeline_once(result_context, tools=tools, client=client, budget=active_budget, stats=stats)
        retry_verified = verify_recovery_result(retry_draft, tools=tools, client=client, stats=stats)
        if _status_rank(retry_verified.status) > _status_rank(verified.status):
            verified = retry_verified

    stats.tool_calls = list(tools.calls)
    return RecoveryOutcome(result=verified, stats=stats, trigger_reason=trigger_reason)
