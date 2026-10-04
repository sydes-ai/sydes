"""The default System impact view never presents an AI-inferred flow like a proven one."""

from __future__ import annotations

from sydes.report.verify_terminal import _render_system_impact_default
from sydes.verify.models import AffectedFlow, ChangeSet, ChangeVerificationResult


def _render(*flows: AffectedFlow) -> list[str]:
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    result.affected_flows = list(flows)
    lines: list[str] = []
    _render_system_impact_default(result, lines)
    return lines


def test_an_inferred_flow_is_marked_and_a_proven_one_is_not() -> None:
    proven = AffectedFlow(id="f1", entry_label="POST /v1/audio/speech", method="POST", path="/v1/audio/speech")
    inferred = AffectedFlow(id="f2", entry_label="GET /v1/download/{filename}", method="GET",
                            path="/v1/download/{filename}", impact_status="inferred")
    lines = _render(proven, inferred)
    assert "POST /v1/audio/speech" in lines
    assert "GET /v1/download/{filename}   [AI-inferred]" in lines
    assert any("proposed by AI inference" in line for line in lines)
    assert not any(line.startswith("POST /v1/audio/speech ") and "[" in line for line in lines)


def test_an_ai_recovery_flow_is_marked() -> None:
    recovered = AffectedFlow(id="f3", entry_label="GET /items", provenance="ai_recovery")
    assert "GET /items   [AI recovery]" in _render(recovered)
