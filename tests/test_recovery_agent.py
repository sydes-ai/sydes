"""`sydes.recovery.agent` — the tool-use loop and `recover()` entry point.

Uses a scripted fake `LLMClient` (same pattern as
`tests/test_boundary_reasoning.py`'s `CountingClient`) rather than a real
provider — these tests are about the loop's control flow and fail-closed
behavior, not about model quality (see `scripts/recovery_eval.py` for the
real-repo, real-model evaluation).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryBudget, recover
from sydes.recovery.context import RecoveryContext
from sydes.recovery.schema import RecoveryError, STATUS_ESTABLISHED, STATUS_UNRESOLVED


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


def _established_final(entrypoint: str = "GET /users", file: str = "handler.ts") -> dict:
    return {
        "final": {
            "recovered_paths": [
                {
                    "entrypoint": entrypoint,
                    "steps": [{"symbol": "handler", "file": file, "relationship": "registers and dispatches"}],
                    "evidence": [{"file": file, "line_start": 1, "line_end": 3, "fact": "registers handler for this route"}],
                    "status": "established",
                }
            ],
            "recovered_tests": [],
            "corrected_first_pass_claims": [],
            "unresolved": [],
        }
    }


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "handler.ts").write_text("line1\nline2\nline3\n")
    return tmp_path


def test_recover_accepts_a_verified_established_path(repo: Path):
    # Turn 1: agent's initial answer; verifier's one call accepts it.
    client = SequencedClient([
        _established_final(),
        {"verdicts": [{"index": 0, "accept": True, "reason": "evidence supports every bridge"}]},
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_ESTABLISHED
    assert outcome.result.recovered_paths[0].provenance == "ai_recovery"


def test_recover_runs_a_tool_call_before_the_final_answer(repo: Path):
    client = SequencedClient([
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        _established_final(),
        {"verdicts": [{"index": 0, "accept": True, "reason": "ok"}]},
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.stats.turns == 2
    assert any(c.tool == "read_file" for c in outcome.stats.tool_calls)


def test_verifier_rejection_downgrades_the_path_and_is_not_lost(repo: Path):
    client = SequencedClient([
        _established_final(),
        {"verdicts": [{"index": 0, "accept": False, "reason": "the cited line does not show a real registration"}]},
        # retry attempt:
        {"final": {"recovered_paths": [], "recovered_tests": [], "corrected_first_pass_claims": [],
                    "unresolved": [{"question": "does anything register this handler?",
                                    "missing_evidence": "no registration point found anywhere in the repo"}]}},
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")
    assert outcome.result.status == STATUS_UNRESOLVED
    assert outcome.stats.verify_retries == 1


def test_provider_failure_raises_recovery_error(repo: Path):
    with pytest.raises(RecoveryError):
        recover(_context(), repo_root=repo, client=FailingClient(), trigger_reason="no established path")


def test_malformed_final_answer_raises_recovery_error(repo: Path):
    client = SequencedClient(["not json and not a tool call"])
    with pytest.raises(RecoveryError):
        recover(_context(), repo_root=repo, client=client, trigger_reason="no established path")


def test_recovery_budget_forces_a_final_answer_eventually(repo: Path):
    budget = RecoveryBudget(max_turns=2)
    client = SequencedClient([
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        {"tool": "read_file", "args": {"path": "handler.ts"}},
        {"final": {"recovered_paths": [], "recovered_tests": [], "corrected_first_pass_claims": [], "unresolved": []}},
    ])
    outcome = recover(_context(), repo_root=repo, client=client, trigger_reason="no established path", budget=budget)
    assert outcome.result.status == STATUS_UNRESOLVED
    assert outcome.stats.turns == 3
