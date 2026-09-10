"""`sydes.recovery.agent` — the discover -> prove (with recursion) ->
verify -> compose pipeline orchestration, and independent path/test
outcomes.

Uses a scripted fake `LLMClient` returning one response per call, in the
exact order the pipeline makes calls: one discovery call (possibly with
tool-call turns first), then one atomic-evidence-completion call per edge
(and per candidate test) -- with a decomposition call and its own set of
atomic-completion calls inserted whenever a direct proof finds nothing --
then one verifier call for paths and, separately, one for tests. This is
about the pipeline's control flow and fail-closed behavior, not model
quality — see `scripts/recovery_eval.py` for the real-repo, real-model
evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryBudget, recover
from sydes.recovery.context import RecoveryContext
from sydes.recovery.schema import RecoveryError, STATUS_ESTABLISHED, STATUS_PARTIAL, STATUS_UNRESOLVED


def _context(changed_files: tuple[str, ...] = ("handler.ts",)) -> RecoveryContext:
    return RecoveryContext(
        reason_first_pass_stopped="no established path",
        gap_kinds=("no_established_flow",),
        diff_summary="- modified app/handler.ts",
        changed_symbols=("handler (app.handler) in app/handler.ts:10",),
        current_result_summary="verdict=VERIFICATION INCOMPLETE",
        cbm_fragments="(none)",
        known_entrypoints=(),
        test_candidates=(),
        unresolved_gaps=(),
        changed_files=changed_files,
    )


class SequencedClient:
    """Returns one scripted `LLMResponse` per call, in order."""

    def __init__(self, responses: list[dict | str]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("SequencedClient exhausted its scripted responses")
        payload = self._responses.pop(0)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return LLMResponse(text=text)


class FailingClient:
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("provider unreachable")


def _node(symbol: str, file: str = "handler.ts") -> dict:
    return {"symbol": symbol, "file": file}


def _discovery_final(nodes: list[str], target_node: str, tests: list[dict] | None = None, file: str = "handler.ts") -> dict:
    return {
        "final": {
            "candidate_path": {"entrypoint": "GET /users", "target_node": target_node, "nodes": [_node(n, file) for n in nodes]},
            "candidate_tests": tests or [],
        }
    }


def _no_path_discovery(tests: list[dict] | None = None) -> dict:
    return {"final": {"candidate_path": None, "candidate_tests": tests or []}}


def _atomic_final(relationship: str, file: str = "handler.ts", to_file: str | None = None) -> dict:
    return {
        "final": {
            "relationship": relationship,
            "to_file": (to_file if to_file is not None else file) if relationship else "",
            "from_file": file if relationship else "",
            "evidence": [{"file": file, "line_start": 1, "line_end": 3, "fact": relationship}] if relationship else [],
        }
    }


def _decompose_final(intermediates: list[str], file: str = "handler.ts") -> dict:
    return {"final": {"intermediates": [_node(s, file) for s in intermediates]}}


def _verdicts(*accepts: bool) -> dict:
    return {"verdicts": [{"index": i, "accept": a, "reason": "ok" if a else "not proven"} for i, a in enumerate(accepts)]}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "handler.ts").write_text("route registers handler\nline2\nline3\n")
    return tmp_path


def test_recover_accepts_a_verified_established_two_node_path(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_ESTABLISHED
    assert outcome.path_recovery.paths[0].edges[0].provenance == "ai_recovery"
    assert outcome.test_recovery.status == STATUS_UNRESOLVED  # no candidate tests proposed


def test_three_node_path_runs_two_independent_atomic_completions(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),
        _atomic_final("dispatches to"),
        _verdicts(True, True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_ESTABLISHED
    assert len(outcome.path_recovery.paths[0].edges) == 2


def test_one_unproven_edge_out_of_two_yields_partial_prefix(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),
        _atomic_final(""),               # nothing found directly
        _decompose_final([]),            # decomposition also finds nothing
        _verdicts(True),                 # only the proven edge reaches the verifier
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.path_recovery.status == STATUS_PARTIAL
    assert [n.symbol for n in outcome.path_recovery.paths[0].nodes] == ["route", "mid"]


def test_discovery_with_no_candidate_path_but_a_candidate_test(repo: Path):
    (repo / "spec.ts").write_text("it('t', () => { expect(handler()).toBe(1); });\n")
    client = SequencedClient([
        _no_path_discovery(tests=[{"file": "spec.ts", "test": "t", "covers": "handler behavior", "target": _node("handler")}]),
        _atomic_final("directly calls handler() and asserts", file="spec.ts", to_file="handler.ts"),
        _verdicts(True),
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="missing test mapping", budget=budget)
    assert outcome.path_recovery.paths == []
    assert outcome.test_recovery.status == STATUS_ESTABLISHED
    assert outcome.test_recovery.tests[0].status == "accepted"


def test_discovery_tool_call_turn_before_final_answer(repo: Path):
    client = SequencedClient([
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_ESTABLISHED
    assert any(c.tool == "read_file" for c in outcome.stats.tool_calls)


def test_verifier_rejection_triggers_one_full_pipeline_retry(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(False),
        # retry: fresh discovery finds nothing this time
        _no_path_discovery(),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_UNRESOLVED
    assert outcome.stats.pipeline_retries == 1


def test_retry_only_replaces_result_when_strictly_better(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),
        _atomic_final(""),
        _decompose_final([]),
        _verdicts(True),
        # retry produces nothing at all -- strictly worse than the existing partial result
        _no_path_discovery(),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_PARTIAL
    assert len(outcome.path_recovery.paths[0].nodes) == 2


def test_provider_failure_during_discovery_raises_recovery_error(repo: Path):
    with pytest.raises(RecoveryError):
        recover(_context(), repo_root=repo, client=FailingClient(), trigger_reason="no established path")


def test_malformed_discovery_final_raises_recovery_error(repo: Path):
    client = SequencedClient(["not json and not a tool call"])
    with pytest.raises(RecoveryError):
        recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")


def test_discovery_budget_forces_a_final_answer_eventually(repo: Path):
    budget = RecoveryBudget(max_discovery_turns=2, max_pipeline_retries=0)
    client = SequencedClient([
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        _no_path_discovery(),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.path_recovery.status == STATUS_UNRESOLVED


def test_independent_recover_calls_do_not_share_state(repo: Path):
    client1 = SequencedClient([_discovery_final(["route", "handler"], "handler"), _atomic_final("registers and dispatches to"), _verdicts(True)])
    outcome1 = recover(_context(), repo_root=repo, client=client1, trigger_reason="no established path")

    client2 = SequencedClient([_no_path_discovery()])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome2 = recover(_context(), repo_root=repo, client=client2, trigger_reason="no established path", budget=budget)

    assert outcome1.path_recovery.status == STATUS_ESTABLISHED
    assert outcome2.path_recovery.status == STATUS_UNRESOLVED
    assert outcome2.stats.llm_calls == 1
    assert outcome2.stats.tool_calls == []


def test_full_pipeline_drops_node_beyond_target_node(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler", "unrelated_extra"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_ESTABLISHED
    path = outcome.path_recovery.paths[0]
    assert [n.symbol for n in path.nodes] == ["route", "handler"]
    assert path.unresolved_suffix == []


# ---------------------------------------------------------------------------
# Recursive missing-link resolution.
# ---------------------------------------------------------------------------


def test_recursive_decomposition_used_when_direct_proof_finds_nothing(repo: Path):
    (repo / "handler.ts").write_text("mid registers handler\nline2\n")
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final(""),                          # direct route -> handler: nothing found
        _decompose_final(["mid"]),                  # decompose into one intermediate
        _atomic_final("route registers mid"),        # route -> mid
        _atomic_final("mid registers handler"),      # mid -> handler
        _verdicts(True, True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.path_recovery.status == STATUS_ESTABLISHED
    path = outcome.path_recovery.paths[0]
    assert [n.symbol for n in path.nodes] == ["route", "mid", "handler"]
    assert all(e.from_decomposition for e in path.edges)
    assert outcome.stats.recursive_decompositions_used == 1


def test_recursive_decomposition_not_attempted_when_direct_proof_succeeds(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.stats.recursive_decompositions_attempted == 0


def test_decomposition_chain_with_one_bad_edge_yields_partial(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final(""),
        _decompose_final(["mid"]),
        _atomic_final("route registers mid"),
        _atomic_final(""),  # mid -> handler still unproven even after decomposition
        _verdicts(True),
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.path_recovery.status == STATUS_PARTIAL
    assert [n.symbol for n in outcome.path_recovery.paths[0].nodes] == ["route", "mid"]


def test_no_second_decomposition_after_first_fails(repo: Path):
    """Recursion happens at most once per hop -- if decomposition itself
    produced a chain but one of ITS edges is still unproven, there is no
    further decomposition of that sub-edge; it is simply reported
    unresolved."""
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final(""),
        _decompose_final(["mid"]),
        _atomic_final(""),  # route -> mid also unproven; no further decomposition attempted
        _atomic_final(""),  # mid -> handler unproven
        # no further decompose_final call is scripted -- if the code tried
        # one, SequencedClient would raise AssertionError
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.path_recovery.status == STATUS_UNRESOLVED


def test_decomposition_chain_respects_max_bridge_nodes(repo: Path):
    """More intermediates than the budget allows are truncated, not used
    wholesale -- confirmed by the exact number of atomic-completion calls
    scripted (one per hop in the truncated chain, not the full proposal)."""
    budget = RecoveryBudget(max_bridge_nodes=1, max_pipeline_retries=0)
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final(""),
        _decompose_final(["a", "b", "c"]),  # proposes 3, budget allows 1
        _atomic_final("route to a"),        # route -> a
        _atomic_final("a to handler"),      # a -> handler (chain truncated to just "a")
        _verdicts(True, True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert [n.symbol for n in outcome.path_recovery.paths[0].nodes] == ["route", "a", "handler"]
