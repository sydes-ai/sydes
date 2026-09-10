"""`sydes.recovery.agent` — the discover -> prove -> verify -> compose
pipeline orchestration.

Uses a scripted fake `LLMClient` returning one response per call, in the
exact order the pipeline makes calls: one discovery call (possibly with
tool-call turns first), then one atomic-evidence-completion call per edge
(and per candidate test), then one verifier call. This is about the
pipeline's control flow and fail-closed behavior, not model quality — see
`scripts/recovery_eval.py` for the real-repo, real-model evaluation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryBudget, recover
from sydes.recovery.context import RecoveryContext
from sydes.recovery.schema import RecoveryError, STATUS_ESTABLISHED, STATUS_PARTIAL, STATUS_UNRESOLVED


def _context() -> RecoveryContext:
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


def _discovery_final(nodes: list[str], target_node: str, tests: list[dict] | None = None) -> dict:
    return {
        "final": {
            "candidate_path": {"entrypoint": "GET /users", "target_node": target_node, "nodes": nodes},
            "candidate_tests": tests or [],
        }
    }


def _no_path_discovery(tests: list[dict] | None = None) -> dict:
    return {"final": {"candidate_path": None, "candidate_tests": tests or []}}


def _atomic_final(relationship: str, file: str = "handler.ts") -> dict:
    return {
        "final": {
            "relationship": relationship,
            "evidence": [{"file": file, "line_start": 1, "line_end": 3, "fact": relationship}] if relationship else [],
        }
    }


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
    assert outcome.result.status == STATUS_ESTABLISHED
    assert outcome.result.recovered_paths[0].edges[0].provenance == "ai_recovery"


def test_three_node_path_runs_two_independent_atomic_completions(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),   # route -> mid
        _atomic_final("dispatches to"),  # mid -> handler
        _verdicts(True, True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_ESTABLISHED
    assert len(outcome.result.recovered_paths[0].edges) == 2


def test_one_unproven_edge_out_of_two_yields_partial_prefix(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),        # route -> mid: found evidence
        _atomic_final(""),                 # mid -> handler: nothing found
        _verdicts(True),                   # only the first (Layer-1 survivor) edge goes to the verifier
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.result.status == STATUS_PARTIAL
    assert [n.symbol for n in outcome.result.recovered_paths[0].nodes] == ["route", "mid"]


def test_edge_with_no_evidence_found_never_reaches_the_verifier(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final(""),  # evidence completion found nothing at all
        # no verdicts response queued -- if the verifier were called this would raise AssertionError
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.result.status == STATUS_UNRESOLVED


def test_discovery_with_no_candidate_path_but_a_candidate_test(repo: Path):
    (repo / "spec.ts").write_text("it('t', () => { expect(handler()).toBe(1); });\n")
    client = SequencedClient([
        _no_path_discovery(tests=[{"file": "spec.ts", "test": "t", "covers": "handler behavior"}]),
        _atomic_final("directly calls handler() and asserts", file="spec.ts"),
        _verdicts(True),
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="missing test mapping", budget=budget)
    assert outcome.result.recovered_paths == []
    assert outcome.result.recovered_tests[0].status == "accepted"


def test_discovery_tool_call_turn_before_final_answer(repo: Path):
    client = SequencedClient([
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_ESTABLISHED
    assert any(c.tool == "read_file" for c in outcome.stats.tool_calls)


def test_atomic_completion_tool_call_turn_before_final_answer(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        _atomic_final("registers and dispatches to"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_ESTABLISHED


def test_verifier_rejection_triggers_one_full_pipeline_retry(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("registers and dispatches to"),
        _verdicts(False),
        # retry: fresh discovery finds nothing this time
        _no_path_discovery(),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_UNRESOLVED
    assert outcome.stats.pipeline_retries == 1


def test_retry_only_replaces_result_when_strictly_better(repo: Path):
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),
        _atomic_final(""),  # partial: mid -> handler unproven
        _verdicts(True),
        # retry produces nothing -- strictly worse than the existing partial result
        _no_path_discovery(),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_PARTIAL
    assert len(outcome.result.recovered_paths[0].nodes) == 2  # kept the verified prefix, not discarded


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
    assert outcome.result.status == STATUS_UNRESOLVED


def test_independent_recover_calls_do_not_share_state(repo: Path):
    """No prior-run evidence leaks into an independent later run: each
    `recover()` call builds its own `RepoTools`/stats from scratch."""
    client1 = SequencedClient([_discovery_final(["route", "handler"], "handler"), _atomic_final("registers and dispatches to"), _verdicts(True)])
    outcome1 = recover(_context(), repo_root=repo, client=client1, trigger_reason="no established path")

    client2 = SequencedClient([_no_path_discovery()])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome2 = recover(_context(), repo_root=repo, client=client2, trigger_reason="no established path", budget=budget)

    assert outcome1.result.status == STATUS_ESTABLISHED
    assert outcome2.result.status == STATUS_UNRESOLVED
    assert outcome2.stats.llm_calls == 1  # only its own single discovery call, nothing carried over
    assert outcome2.stats.tool_calls == []


def test_verifier_rejects_found_evidence_yields_partial_prefix(repo: Path):
    """Distinct from 'no evidence found' (test above): here evidence
    completion DOES find something for the second edge, but the
    adversarial verifier (Stage C) rejects it -- the prefix up to that
    point must still be kept, not discarded."""
    client = SequencedClient([
        _discovery_final(["route", "mid", "handler"], "handler"),
        _atomic_final("registers"),           # route -> mid: evidence found
        _atomic_final("declared nearby"),     # mid -> handler: evidence found but weak
        _verdicts(True, False),               # both go to the verifier; second is rejected
    ])
    budget = RecoveryBudget(max_pipeline_retries=0)
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.result.status == STATUS_PARTIAL
    assert [n.symbol for n in outcome.result.recovered_paths[0].nodes] == ["route", "mid"]
    assert outcome.result.recovered_paths[0].unresolved_suffix[0].to_symbol == "handler"


def test_edge_relationship_text_comes_entirely_from_evidence_completion(repo: Path):
    """Discovery never proposes relationship wording at all in this
    schema -- only node names. The final edge's `relationship` is
    whatever Stage B (evidence completion) found, confirming Stage A's
    hypothesis carries no unverified narrative into the final result."""
    client = SequencedClient([
        _discovery_final(["route", "handler"], "handler"),
        _atomic_final("a very specific, source-grounded description of the real wiring"),
        _verdicts(True),
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.recovered_paths[0].edges[0].relationship == "a very specific, source-grounded description of the real wiring"


def test_full_pipeline_drops_node_beyond_target_node(repo: Path):
    """Shortest-sufficient-path, end to end: if discovery proposes a node
    past target_node, it never appears in the final result at all -- not
    even as unresolved_suffix -- regardless of whether its edge would
    have been accepted."""
    client = SequencedClient([
        _discovery_final(["route", "handler", "unrelated_extra"], "handler"),
        _atomic_final("registers and dispatches to"),  # route -> handler (the only edge up to target)
        _verdicts(True),
        # No atomic-completion or verdict call for handler -> unrelated_extra:
        # it must never be attempted since it is beyond target_node.
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_ESTABLISHED
    path = outcome.result.recovered_paths[0]
    assert [n.symbol for n in path.nodes] == ["route", "handler"]
    assert path.unresolved_suffix == []
