"""A run that hit LLM/provider failures is never counted as benchmark data."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapter"))

import aacr  # noqa: E402

CASE = {"case_id": "o__r-1", "instance_id": "o__r@abcdef1", "repo": "o/r", "pr": 1, "pr_url": "https://github.com/o/r/pull/1",
        "language": "Python", "base_commit": "b" * 40, "head_commit": "a" * 40, "change_line_count": 3}
QUOTA = "OpenAI request failed: Error code: 429 - {'error': {'type': 'insufficient_quota'}}"


def _review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sydes: dict, investigation: dict | None) -> Path:
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({"cases": [CASE]}))
    (tmp_path / "checkout").mkdir()
    (tmp_path / "bench" / "aacr").mkdir(parents=True)
    monkeypatch.setattr(aacr, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(aacr, "ROOT", tmp_path / "bench" / "aacr")
    monkeypatch.setattr(aacr, "repo_checkout", lambda c: tmp_path / "checkout")
    monkeypatch.setattr(aacr, "review_base", lambda path, b, h: (b, {"rule": "stub"}))
    monkeypatch.setattr(aacr, "clean", lambda path: None)
    monkeypatch.setattr(aacr, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_sydes(command, **kwargs):
        out = Path(command[command.index("--json") + 1])
        out.write_text(json.dumps(sydes))
        if investigation is not None:
            out.with_name(out.stem + ".investigation.json").write_text(json.dumps(investigation))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(aacr.subprocess, "run", fake_sydes)
    aacr.review(cases, "r")
    return tmp_path / "results" / "r"


FINDING = {"title": "t", "file": "f.py", "line": 2}


@pytest.mark.parametrize(("sydes", "investigation"), [
    ({"code_findings": [], "run_metrics": {"llm_totals": {"calls": 9, "errors": 2}}}, None),
    ({"code_findings": [], "run_metrics": {"llm_totals": {"calls": 9, "errors": 0}}, "diagnostics": [f"pr_semantic_analysis unavailable: {QUOTA}"]}, None),
    ({"code_findings": [FINDING], "run_metrics": {"llm_totals": {"calls": 9, "errors": 0}}}, {"errors": [f"finder: {QUOTA}"]}),
])
def test_a_case_with_provider_errors_is_invalid_and_never_scored(tmp_path, monkeypatch, sydes, investigation) -> None:
    run = _review(tmp_path, monkeypatch, sydes, investigation)
    assert not (run / "aacr" / "o__r@abcdef1.json").exists()          # the evaluator never sees it
    assert (run / "invalid" / "o__r@abcdef1.json").exists()           # kept for debugging
    assert (run / "sydes" / "o__r@abcdef1.json").exists()
    tele = json.loads((run / "telemetry" / "o__r@abcdef1.json").read_text())
    assert tele["status"].startswith("INVALID") and tele["invalid_reasons"]
    summary = aacr.summarize("r")
    assert summary["invalid_cases"][0]["case_id"] == "o__r-1" and summary["cases"] == []


def test_a_clean_case_is_scored(tmp_path, monkeypatch) -> None:
    run = _review(tmp_path, monkeypatch, {"code_findings": [FINDING], "run_metrics": {"llm_totals": {"calls": 9, "errors": 0}}},
                  {"errors": []})
    assert (run / "aacr" / "o__r@abcdef1.json").exists() and not (run / "invalid").exists()
