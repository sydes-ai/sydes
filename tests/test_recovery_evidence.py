"""`sydes.recovery.evidence` — Stage B in isolation: proves or disproves
ONE relationship/test claim at a time, never a whole path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryRunStats
from sydes.recovery.evidence import prove_relationship, prove_test_claim
from sydes.recovery.schema import CandidateTestClaim
from sydes.recovery.tools import RepoTools


class SequencedClient:
    def __init__(self, responses: list[dict | str]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        if not self._responses:
            raise AssertionError("exhausted scripted responses")
        payload = self._responses.pop(0)
        return LLMResponse(text=payload if isinstance(payload, str) else json.dumps(payload))


class AlwaysToolCallClient:
    """Never gives a final answer -- used to confirm the atomic prover's
    own turn budget bounds it and does not retry beyond that budget."""

    def __init__(self) -> None:
        self.call_count = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(text=json.dumps({"tool": "read_file", "args": {"path": "handler.ts"}}))


class FailingClient:
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError("provider unreachable")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "handler.ts").write_text("route registers handler\n")
    return tmp_path


def test_prove_relationship_returns_evidence_when_found(repo: Path):
    client = SequencedClient([
        {"final": {"relationship": "registers and dispatches", "evidence": [{"file": "handler.ts", "line_start": 1, "line_end": 1, "fact": "registers handler"}]}}
    ])
    stats = RecoveryRunStats()
    edge = prove_relationship("route", "handler", "test context", tools=RepoTools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert edge.from_symbol == "route"
    assert edge.to_symbol == "handler"
    assert len(edge.evidence) == 1


def test_prove_relationship_returns_empty_evidence_when_nothing_found(repo: Path):
    client = SequencedClient([{"final": {"relationship": "", "evidence": []}}])
    stats = RecoveryRunStats()
    edge = prove_relationship("a", "b", "ctx", tools=RepoTools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert edge.evidence == []


def test_prove_relationship_never_raises_on_provider_failure(repo: Path):
    """A failed atomic proof attempt is 'no evidence found' for this one
    relationship, not a fatal error for the whole recovery run."""
    stats = RecoveryRunStats()
    edge = prove_relationship("a", "b", "ctx", tools=RepoTools(repo), client=FailingClient(), max_turns=3, max_response_chars=4000, stats=stats)
    assert edge.evidence == []


def test_prove_relationship_does_not_retry_beyond_its_own_turn_budget(repo: Path):
    """Evidence completion cannot find proof -> no repeated retries beyond
    the configured budget: a client that never answers 'final' must be
    bounded by max_turns, not looped on indefinitely."""
    client = AlwaysToolCallClient()
    stats = RecoveryRunStats()
    edge = prove_relationship("a", "b", "ctx", tools=RepoTools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert edge.evidence == []
    # max_turns=3 -> at most 4 calls (3 tool-call turns + 1 forced-final turn)
    assert client.call_count <= 4


def test_prove_test_claim_returns_evidence_when_found(repo: Path):
    (repo / "spec.ts").write_text("it('t', () => { expect(handler()).toBe(1); });\n")
    candidate = CandidateTestClaim(file="spec.ts", test="t", covers="handler behavior")
    client = SequencedClient([
        {"final": {"relationship": "directly calls handler() and asserts", "evidence": [{"file": "spec.ts", "line_start": 1, "line_end": 1, "fact": "calls handler()"}]}}
    ])
    stats = RecoveryRunStats()
    result = prove_test_claim(candidate, "ctx", tools=RepoTools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert result.file == "spec.ts"
    assert len(result.evidence) == 1


def test_prove_test_claim_returns_empty_evidence_when_nothing_found(repo: Path):
    (repo / "spec.ts").write_text("import { handler } from './handler';\n")
    candidate = CandidateTestClaim(file="spec.ts", test="unrelated", covers="handler behavior")
    client = SequencedClient([{"final": {"relationship": "", "evidence": []}}])
    stats = RecoveryRunStats()
    result = prove_test_claim(candidate, "ctx", tools=RepoTools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats)
    assert result.evidence == []
