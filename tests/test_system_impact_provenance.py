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


def test_code_review_findings_are_shown_in_the_default_report() -> None:
    from sydes.report.verify_terminal import render_verify_change_terminal
    from sydes.verify.models import CodeFinding

    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    result.code_findings = [CodeFinding(id="finding-1", severity="P2", title="Cleanup skipped when the stream is closed",
                                        file="app/routes.py", line=42, explanation="The wrapper does not close it.",
                                        impact="Resources stay open.", suggested_fix="Close the inner iterator.")]
    text = render_verify_change_terminal(result)
    assert "Code review (advisory; excluded from the verdict)" in text
    assert "[P2] Cleanup skipped when the stream is closed" in text and "app/routes.py:42" in text
    assert "Fix: Close the inner iterator." in text
    plain = ChangeVerificationResult(change=ChangeSet(base="main"))
    assert "Code review" not in render_verify_change_terminal(plain)
