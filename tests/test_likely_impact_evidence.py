"""`sydes.recovery.likely_impact_evidence` — the experimental evidence
checker for "likely, not fully established" impact candidates. These
tests exercise the tool-use loop wrapper and the fail-safe guarantees
with a scripted/mocked LLM client — no real provider calls (see
`scripts/evaluate_likely_impact_evidence.py` for the live-repo, real-LLM
evaluation harness this module was built for).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMRequest, LLMResponse
from sydes.recovery.likely_impact_evidence import (
    PROMOTE,
    RETAIN,
    SUPPRESS,
    EvidenceCheckStats,
    LikelyImpactCandidate,
    check_candidate_evidence,
)
from sydes.recovery.schema import RecoveryError


class SequencedClient:
    def __init__(self, responses: list[dict | str]) -> None:
        self._responses = list(responses)

    def generate(self, request: LLMRequest) -> LLMResponse:
        payload = self._responses.pop(0)
        return LLMResponse(text=payload if isinstance(payload, str) else json.dumps(payload))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def handler():\n    pass\n")
    return tmp_path


def _tools(repo: Path):
    from sydes.recovery.tools import RepoTools

    return RepoTools(repo)


def _candidate(**overrides) -> LikelyImpactCandidate:
    base = dict(
        entry_label="GET /dev/model",
        candidate_label="model_status",
        proposed_target="generate_audio_stream",
        changed_files=("api/src/services/tts_service.py",),
    )
    base.update(overrides)
    return LikelyImpactCandidate(**base)


def test_promote_when_agent_finds_real_evidence(repo: Path):
    client = SequencedClient([
        {"final": {
            "decision": "promote",
            "reason": "model_status calls tts_service.generate_audio_stream directly, see app.py:12",
            "evidence_files": ["app.py"],
        }}
    ])
    result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=EvidenceCheckStats())
    assert result.decision == PROMOTE
    assert result.evidence_files == ("app.py",)


def test_suppress_when_agent_finds_contradicting_evidence(repo: Path):
    client = SequencedClient([
        {"final": {
            "decision": "suppress",
            "reason": "model_status only calls model_manager.status(), never generate_audio_stream",
            "evidence_files": ["app.py"],
        }}
    ])
    result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=EvidenceCheckStats())
    assert result.decision == SUPPRESS


def test_retain_when_agent_is_genuinely_uncertain(repo: Path):
    client = SequencedClient([
        {"final": {"decision": "retain", "reason": "no clear evidence either way after searching", "evidence_files": []}}
    ])
    result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=EvidenceCheckStats())
    assert result.decision == RETAIN


def test_malformed_final_answer_fails_safe_to_retain_not_an_exception(repo: Path):
    """A parse failure must never propagate as an uncaught error, and must
    never silently read as a positive suppression -- it degrades to the
    same conservative default as genuine model uncertainty."""
    client = SequencedClient(["not json at all"])
    result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=EvidenceCheckStats())
    assert result.decision == RETAIN
    assert "could not complete" in result.reason


def test_invalid_decision_value_fails_safe_to_retain(repo: Path):
    client = SequencedClient([{"final": {"decision": "maybe", "reason": "unsure"}}])
    result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=EvidenceCheckStats())
    assert result.decision == RETAIN


def test_provider_failure_fails_safe_to_retain(repo: Path):
    class FailingClient:
        def generate(self, request: LLMRequest) -> LLMResponse:
            from sydes.llm.client import LLMClientError

            raise LLMClientError("provider unavailable")

    result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=FailingClient(), stats=EvidenceCheckStats())
    assert result.decision == RETAIN


def test_never_raises_recovery_error_out_of_the_function(repo: Path):
    """The whole point of the fail-safe wrapper: callers integrating this
    into a pipeline must never need their own try/except around it."""
    client = SequencedClient(["garbage"])
    try:
        result = check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=EvidenceCheckStats())
    except RecoveryError:
        pytest.fail("check_candidate_evidence must catch RecoveryError internally, not raise it")
    assert result.decision == RETAIN


def test_stats_accumulate_across_the_tool_loop(repo: Path):
    stats = EvidenceCheckStats()
    client = SequencedClient([
        {"tool": "read_file", "args": {"path": "app.py"}},
        {"final": {"decision": "retain", "reason": "still unclear", "evidence_files": []}},
    ])
    check_candidate_evidence(_candidate(), tools=_tools(repo), client=client, stats=stats)
    assert stats.llm_calls == 2
    assert stats.turns == 2
