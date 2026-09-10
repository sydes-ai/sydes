"""`sydes.recovery.verify` — the deterministic evidence-existence gate and
the adversarial LLM verification pass."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sydes.llm.client import LLMRequest, LLMResponse
from sydes.recovery.agent import RecoveryRunStats
from sydes.recovery.schema import (
    RecoveredEvidence,
    RecoveredPath,
    RecoveredStep,
    RecoveryResult,
    STATUS_ESTABLISHED,
    STATUS_UNRESOLVED,
)
from sydes.recovery.tools import RepoTools
from sydes.recovery.verify import verify_recovery_result


class ScriptedVerifier:
    def __init__(self, payload: dict) -> None:
        self._text = json.dumps(payload)
        self.calls = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text=self._text)


def _draft_with_one_established_path(*, file: str, line_start: int, line_end: int) -> RecoveryResult:
    path = RecoveredPath(
        entrypoint="GET /users",
        steps=[RecoveredStep(symbol="handler", file=file, relationship="registers")],
        evidence=[RecoveredEvidence(file=file, line_start=line_start, line_end=line_end, fact="registers the handler")],
        status=STATUS_ESTABLISHED,
        provenance="ai_recovery",
    )
    return RecoveryResult(status=STATUS_ESTABLISHED, recovered_paths=[path])


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "handler.ts").write_text("line1\nline2\nline3\n")
    return tmp_path


def test_verifier_accepts_a_well_evidenced_path(repo: Path):
    draft = _draft_with_one_established_path(file="handler.ts", line_start=1, line_end=2)
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "solid"}]})
    stats = RecoveryRunStats()
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=stats)
    assert verified.status == STATUS_ESTABLISHED
    assert client.calls == 1


def test_verifier_can_reject_a_claimed_path(repo: Path):
    draft = _draft_with_one_established_path(file="handler.ts", line_start=1, line_end=2)
    client = ScriptedVerifier(
        {"verdicts": [{"index": 0, "accept": False, "reason": "the runtime connection is only assumed"}]}
    )
    stats = RecoveryRunStats()
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=stats)
    assert verified.status == STATUS_UNRESOLVED
    assert verified.recovered_paths[0].rejection_reason == "the runtime connection is only assumed"
    assert verified.recovered_paths[0].provenance == "ai_recovery_exhausted"


def test_deterministic_check_rejects_fabricated_line_range_without_an_llm_call(repo: Path):
    # handler.ts only has 3 lines; this path cites lines 40-42.
    draft = _draft_with_one_established_path(file="handler.ts", line_start=40, line_end=42)
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    stats = RecoveryRunStats()
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=stats)
    assert verified.status == STATUS_UNRESOLVED
    assert client.calls == 0  # rejected before ever asking the verifier


def test_deterministic_check_rejects_a_nonexistent_file(repo: Path):
    draft = _draft_with_one_established_path(file="does_not_exist.ts", line_start=1, line_end=2)
    client = ScriptedVerifier({"verdicts": [{"index": 0, "accept": True, "reason": "n/a"}]})
    stats = RecoveryRunStats()
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=stats)
    assert verified.status == STATUS_UNRESOLVED
    assert client.calls == 0


def test_verifier_giving_no_verdict_for_a_path_is_treated_as_rejection(repo: Path):
    draft = _draft_with_one_established_path(file="handler.ts", line_start=1, line_end=2)
    client = ScriptedVerifier({"verdicts": []})  # no verdict at all for index 0
    stats = RecoveryRunStats()
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=stats)
    assert verified.status == STATUS_UNRESOLVED


def test_unresolved_paths_are_left_alone_no_verifier_call_needed(repo: Path):
    path = RecoveredPath(entrypoint="GET /users", status=STATUS_UNRESOLVED, provenance="ai_recovery_exhausted")
    draft = RecoveryResult(status=STATUS_UNRESOLVED, recovered_paths=[path])
    client = ScriptedVerifier({"verdicts": []})
    stats = RecoveryRunStats()
    verified = verify_recovery_result(draft, tools=RepoTools(repo), client=client, stats=stats)
    assert verified.status == STATUS_UNRESOLVED
    assert client.calls == 0
