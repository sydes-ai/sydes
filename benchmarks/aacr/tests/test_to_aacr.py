"""The AACR submission carries Code Review findings only, in AACR's generic-comment shape."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

from to_aacr import to_aacr_result, to_comment  # noqa: E402

FINDING = {"id": "f1", "severity": "P1", "title": "Captioned streams stay open", "file": "api/src/routers/development.py",
           "line": 374, "explanation": "The wrapper does not close its inner iterator.",
           "impact": "Writers stay open until GC.", "suggested_fix": "Use aclosing()."}


def test_a_finding_becomes_one_comment_with_its_single_line() -> None:
    comment = to_comment(FINDING)
    assert comment == {
        "path": "api/src/routers/development.py", "start_line": 374, "end_line": 374,
        "content": ("Captioned streams stay open\nThe wrapper does not close its inner iterator.\n"
                    "Impact: Writers stay open until GC.\nFix: Use aclosing()."),
        "severity": "P1",
    }


def test_a_finding_without_a_line_keeps_null_lines() -> None:
    comment = to_comment({**FINDING, "line": None})
    assert comment["start_line"] is None and comment["end_line"] is None


def test_only_code_findings_are_submitted() -> None:
    sydes = {
        "code_findings": [FINDING],
        "pr_semantic_analysis": {"local_risks": [{"description": "a risk", "severity": "P1"}]},
        "affected_flows": [{"method": "POST", "path": "/x"}],
        "run_metrics": {"llm_totals": {"input_tokens": 100, "output_tokens": 20}},
    }
    result = to_aacr_result("o__r@abc1234", sydes, duration_seconds=12.34, status="completed")
    assert [c["content"].splitlines()[0] for c in result["review"]["comments"]] == ["Captioned streams stay open"]
    assert result["review"]["summary"] == {"input_tokens": 100, "output_tokens": 20}
    assert result["boundary"] == "code_findings" and result["duration_seconds"] == 12.3


def test_a_failed_run_still_yields_an_empty_result_file() -> None:
    result = to_aacr_result("o__r@abc1234", None, duration_seconds=0, status="failed")
    assert result["review"]["comments"] == [] and result["status"] == "failed"
