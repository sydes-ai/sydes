"""`sydes.recovery.evidence` — Stage B in isolation: proves or disproves
ONE relationship/test claim at a time, resolves canonical entity identity,
and (via `decompose_relationship`) proposes a recursive missing-link
chain. Never a whole path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMClientError, LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryRunStats
from sydes.recovery.evidence import decompose_relationship, prove_relationship, prove_test_claim
from sydes.recovery.schema import CandidateTestClaim, EntityRef
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


def _entity(symbol: str, file: str = "") -> EntityRef:
    return EntityRef(symbol=symbol, file=file)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "handler.ts").write_text("route registers handler\n")
    return tmp_path


def test_prove_relationship_returns_resolved_identity_and_evidence(repo: Path):
    client = SequencedClient([
        {"final": {
            "from_file": "handler.ts", "to_file": "handler.ts", "to_qualified_name": "pkg.handler",
            "relationship": "registers and dispatches",
            "evidence": [{"file": "handler.ts", "line_start": 1, "line_end": 1, "fact": "registers handler"}],
        }}
    ])
    stats = RecoveryRunStats()
    edge = prove_relationship(
        _entity("route"), _entity("handler"), "test context",
        tools=RepoTools(repo), client=client, max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert edge.from_entity.symbol == "route"
    assert edge.to_entity.symbol == "handler"
    assert edge.to_entity.file == "handler.ts"
    assert edge.to_entity.qualified_name == "pkg.handler"
    assert len(edge.evidence) == 1


def test_prove_relationship_returns_empty_evidence_when_nothing_found(repo: Path):
    client = SequencedClient([{"final": {"relationship": "", "evidence": [], "from_file": "", "to_file": ""}}])
    stats = RecoveryRunStats()
    edge = prove_relationship(
        _entity("a"), _entity("b"), "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert edge.evidence == []
    assert edge.to_entity.file == ""  # stayed unresolved, not silently guessed


def test_prove_relationship_preserves_original_file_when_not_re_resolved(repo: Path):
    """If discovery already knew a file and Stage B's response omits
    `to_file`, the original (already-known) file must not be wiped out."""
    client = SequencedClient([{"final": {"relationship": "calls", "evidence": [{"file": "handler.ts", "line_start": 1, "line_end": 1, "fact": "calls"}]}}])
    stats = RecoveryRunStats()
    edge = prove_relationship(
        _entity("a", file="a.ts"), _entity("b", file="handler.ts"), "ctx", tools=RepoTools(repo),
        client=client, max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert edge.to_entity.file == "handler.ts"


def test_prove_relationship_never_raises_on_provider_failure(repo: Path):
    stats = RecoveryRunStats()
    edge = prove_relationship(
        _entity("a"), _entity("b"), "ctx", tools=RepoTools(repo), client=FailingClient(),
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert edge.evidence == []


def test_prove_relationship_does_not_retry_beyond_its_own_turn_budget(repo: Path):
    client = AlwaysToolCallClient()
    stats = RecoveryRunStats()
    edge = prove_relationship(
        _entity("a"), _entity("b"), "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert edge.evidence == []
    assert client.call_count <= 4  # 3 tool-call turns + 1 forced-final turn


def test_prove_test_claim_returns_evidence_when_found(repo: Path):
    (repo / "spec.ts").write_text("it('t', () => { expect(handler()).toBe(1); });\n")
    candidate = CandidateTestClaim(file="spec.ts", test="t", covers="handler behavior")
    client = SequencedClient([
        {"final": {"to_file": "handler.ts", "relationship": "directly calls handler() and asserts",
                    "evidence": [{"file": "spec.ts", "line_start": 1, "line_end": 1, "fact": "calls handler()"}]}}
    ])
    stats = RecoveryRunStats()
    result = prove_test_claim(
        candidate, _entity("handler", "handler.ts"), "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert result.file == "spec.ts"
    assert result.target.file == "handler.ts"
    assert len(result.evidence) == 1


def test_prove_test_claim_returns_empty_evidence_when_nothing_found(repo: Path):
    (repo / "spec.ts").write_text("import { handler } from './handler';\n")
    candidate = CandidateTestClaim(file="spec.ts", test="unrelated", covers="handler behavior")
    client = SequencedClient([{"final": {"relationship": "", "evidence": []}}])
    stats = RecoveryRunStats()
    result = prove_test_claim(
        candidate, _entity("handler", "handler.ts"), "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert result.evidence == []


def test_prove_test_claim_with_no_proposed_target_resolves_from_agent_not_from_test_name(repo: Path):
    """A real reliability-experiment run showed the fallback for 'no target
    proposed by discovery' naming the target after the TEST ITSELF -- which
    then let a test's own file resolve as if it were the entity under test.
    `target=None` must ask the agent to determine the real entity, never
    silently substitute the test's own name/file."""
    (repo / "handler.ts").write_text("export function handler() { return 1; }\n")
    (repo / "spec.ts").write_text("it('handles it', () => { expect(handler()).toBe(1); });\n")
    candidate = CandidateTestClaim(file="spec.ts", test="handles it", covers="handler behavior")
    client = SequencedClient([
        {"final": {
            "to_file": "handler.ts", "to_symbol": "handler",
            "relationship": "directly calls handler() and asserts on its result",
            "evidence": [{"file": "spec.ts", "line_start": 1, "line_end": 1, "fact": "calls handler()"}],
        }}
    ])
    stats = RecoveryRunStats()
    result = prove_test_claim(
        candidate, None, "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert result.target.file == "handler.ts"
    assert result.target.symbol == "handler"
    assert result.target.file != candidate.file


def test_prove_test_claim_with_no_proposed_target_and_no_resolution_stays_unresolved(repo: Path):
    (repo / "spec.ts").write_text("it('t', () => {});\n")
    candidate = CandidateTestClaim(file="spec.ts", test="t", covers="unclear")
    client = SequencedClient([{"final": {"relationship": "", "evidence": []}}])
    stats = RecoveryRunStats()
    result = prove_test_claim(
        candidate, None, "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert result.target.file == ""
    assert result.evidence == []


def test_decompose_relationship_returns_intermediates(repo: Path):
    client = SequencedClient([{"final": {"intermediates": [{"symbol": "mid", "file": "handler.ts"}]}}])
    stats = RecoveryRunStats()
    intermediates = decompose_relationship(
        _entity("a"), _entity("b"), "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert [n.symbol for n in intermediates] == ["mid"]


def test_decompose_relationship_returns_empty_when_nothing_found(repo: Path):
    client = SequencedClient([{"final": {"intermediates": []}}])
    stats = RecoveryRunStats()
    intermediates = decompose_relationship(
        _entity("a"), _entity("b"), "ctx", tools=RepoTools(repo), client=client,
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert intermediates == []


def test_decompose_relationship_never_raises_on_provider_failure(repo: Path):
    stats = RecoveryRunStats()
    intermediates = decompose_relationship(
        _entity("a"), _entity("b"), "ctx", tools=RepoTools(repo), client=FailingClient(),
        max_turns=3, max_response_chars=4000, stats=stats,
    )
    assert intermediates == []
