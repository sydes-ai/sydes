"""The review stage never sees AACR's ground truth.

Reference comments, the converted dataset and evaluator outputs may enter only
at `eval`, after every review exists. These tests run the real `review` stage
with git and Sydes stubbed, record every file it opens, and check the committed
case files carry nothing a reviewer must not see.
"""

from __future__ import annotations

import builtins
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ADAPTER = Path(__file__).resolve().parent.parent / "adapter"
sys.path.insert(0, str(ADAPTER))

import aacr  # noqa: E402

CASES_DIR = Path(__file__).resolve().parent.parent / "cases"


@pytest.mark.parametrize("name", ["dev-smoke-supported.json", "dev-laptop-supported.json"])
def test_case_files_carry_only_reviewer_visible_fields(name: str) -> None:
    data = json.loads((CASES_DIR / name).read_text())
    assert data["cases"], name
    for case in data["cases"]:
        assert set(case) <= aacr.REVIEW_CASE_FIELDS, case["case_id"]
        assert case["language"] in data["source"]["supported_languages"]
    assert "comment" not in json.dumps(data).lower()


def test_review_reads_only_the_case_file_and_its_own_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = tmp_path / "cases.json"
    case = {"case_id": "o__r-1", "instance_id": "o__r@abcdef1", "repo": "o/r", "pr": 1, "pr_url": "https://github.com/o/r/pull/1",
            "language": "Python", "base_commit": "b" * 40, "head_commit": "a" * 40, "change_line_count": 3}
    cases.write_text(json.dumps({"cases": [case]}))
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(aacr, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(aacr, "ROOT", tmp_path / "bench" / "aacr")
    (tmp_path / "bench" / "aacr").mkdir(parents=True)
    monkeypatch.setattr(aacr, "repo_checkout", lambda c: checkout)
    monkeypatch.setattr(aacr, "review_base", lambda path, b, h: (b, {"rule": "stub"}))
    monkeypatch.setattr(aacr, "clean", lambda path: None)
    monkeypatch.setattr(aacr, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_sydes(command, **kwargs):
        out = Path(command[command.index("--json") + 1])
        out.write_text(json.dumps({"code_findings": [{"title": "t", "file": "f.py", "line": 2}], "run_metrics": {}}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(aacr.subprocess, "run", fake_sydes)

    reads: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def recording(real):
        def opener(file, mode="r", *args, **kwargs):
            if "r" in mode and "+" not in mode:
                reads.append(str(file))
            return real(file, mode, *args, **kwargs)
        return opener

    monkeypatch.setattr(builtins, "open", recording(real_open))
    monkeypatch.setattr(io, "open", recording(real_io_open))
    aacr.review(cases, "leak-test")

    run_dir = str(tmp_path / "results" / "leak-test")
    for path in reads:
        assert path == str(cases) or path.startswith(run_dir), f"review read {path}"
        assert "aacr-bench" not in path and "dataset" not in path and "metrics_" not in path, path
    result = json.loads((tmp_path / "results" / "leak-test" / "aacr" / "o__r@abcdef1.json").read_text())
    assert [c["content"] for c in result["review"]["comments"]] == ["t"]


def test_variance_treats_one_place_as_one_finding_whatever_the_wording() -> None:
    a = {"path": "g/e.py", "start_line": 114, "content": "Bind each connected component before scheduling its merge"}
    b = {"path": "g/e.py", "start_line": 114, "content": "Late-bound loop variable makes merge tasks target the wrong component"}
    assert aacr._same(a, b)
    assert not aacr._same(a, {**b, "start_line": 140})
    assert not aacr._same(a, {**b, "path": "other.py"})
    assert aacr._same({"path": "x.py", "start_line": None, "content": "close the writer on abort"},
                      {"path": "x.py", "start_line": None, "content": "writer not closed on abort"})


def test_replay_reads_only_the_case_file_the_source_run_and_its_own_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = tmp_path / "cases.json"
    case = {"case_id": "o__r-1", "instance_id": "o__r@abcdef1", "repo": "o/r", "pr": 1, "pr_url": "https://github.com/o/r/pull/1",
            "language": "Python", "base_commit": "b" * 40, "head_commit": "a" * 40, "change_line_count": 3}
    cases.write_text(json.dumps({"cases": [case]}))
    results = tmp_path / "results"
    (results / "src" / "sydes").mkdir(parents=True)
    (results / "src" / "telemetry").mkdir()
    (results / "src" / "sydes" / "o__r@abcdef1.snapshot.json").write_text("{}")
    (results / "src" / "telemetry" / "o__r@abcdef1.json").write_text(json.dumps({"invalid_reasons": []}))
    (results / "src" / "dataset.jsonl").write_text("REFERENCE")  # must never be read
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(aacr, "RESULTS", results)
    monkeypatch.setattr(aacr, "ROOT", tmp_path / "bench" / "aacr")
    (tmp_path / "bench" / "aacr").mkdir(parents=True)
    monkeypatch.setattr(aacr, "repo_checkout", lambda c: checkout)
    monkeypatch.setattr(aacr, "review_base", lambda path, b, h: (b, {"rule": "stub"}))
    monkeypatch.setattr(aacr, "clean", lambda path: None)
    monkeypatch.setattr(aacr, "sh", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(aacr, "sydes_build", lambda ref: ("/build", "c" * 40))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    commands = []

    def fake_replay(command, **kwargs):
        commands.append(command)
        out = Path(command[command.index("--json") + 1])
        out.write_text(json.dumps({"code_findings": [{"title": "t", "file": "f.py", "line": 2}], "run_metrics": {}}))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(aacr.subprocess, "run", fake_replay)
    reads: list[str] = []
    real_open, real_io_open = builtins.open, io.open

    def recording(real):
        def opener(file, mode="r", *args, **kwargs):
            if "r" in mode and "+" not in mode:
                reads.append(str(file))
            return real(file, mode, *args, **kwargs)
        return opener

    monkeypatch.setattr(builtins, "open", recording(real_open))
    monkeypatch.setattr(io, "open", recording(real_io_open))
    aacr.replay(cases, "rep", "src", "two_round", "HEAD")

    allowed = (str(results / "src" / "sydes"), str(results / "src" / "telemetry"), str(results / "rep"))
    for path in reads:
        assert path == str(cases) or path.startswith(allowed), f"replay read {path}"
    assert commands[0][commands[0].index("--allocation") + 1] == "two_round"
    assert "sydes.verify.investigation_replay" in commands[0]
    assert (results / "rep" / "aacr" / "o__r@abcdef1.json").exists()
