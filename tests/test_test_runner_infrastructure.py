"""A test runner that cannot start is an environment fact, never a test failure.

`python3 -m pytest` on an interpreter without pytest prints `…/python3: No module named
pytest` and exits 1 -- the code pytest itself uses for failing tests. Such a run must be
reported as test execution unavailable, must not name any test as failed, and cannot by
itself produce ACTION REQUIRED. Genuine pytest failures still fail.
"""

from __future__ import annotations

import stat
from pathlib import Path

from sydes.verify.analyzer import (
    _compute_summary,
    resolve_obligation_status_from_executions,
)
from sydes.verify.models import (
    BLOCKER_COLLECTION_ERROR,
    BLOCKER_RUNNER_MISSING,
    VERDICT_ACTION_REQUIRED,
    VERIFICATION_FAILED,
    VERIFICATION_UNKNOWN,
    AffectedFlow,
    ChangeSet,
    ChangeVerificationResult,
    MappedTest,
    VerificationObligation,
)
from sydes.verify.source_files import load_repo_files
from sydes.verify.test_execution import (
    ExecutionSettings,
    _interpret_exit,
    execute_mapped_tests,
)

NO_PYTEST = "/usr/local/bin/python3: No module named pytest\n"
REAL_FAILURE = "F.\n=================================== FAILURES ===\nE   assert 1 == 2\nFAILED t.py::test_a - assert 1 == 2\n1 failed, 1 passed in 0.02s\n"


def test_a_missing_pytest_module_is_runner_missing_not_failed() -> None:
    status, blocker, detail = _interpret_exit("pytest", 1, NO_PYTEST)
    assert (status, blocker) == (VERIFICATION_UNKNOWN, BLOCKER_RUNNER_MISSING)
    assert detail and detail.startswith("pytest")
    status, blocker, _ = _interpret_exit("pytest", 1, "ModuleNotFoundError: No module named 'pytest'\n")
    assert status == VERIFICATION_UNKNOWN and blocker is not None


def test_a_missing_executable_is_runner_missing() -> None:
    status, blocker, _ = _interpret_exit("pytest", 1, "env: python3: No such file or directory\n")
    assert (status, blocker) == (VERIFICATION_UNKNOWN, BLOCKER_RUNNER_MISSING)


def test_exit_1_without_a_reported_failure_is_not_a_failure() -> None:
    status, blocker, _ = _interpret_exit("pytest", 1, "something went wrong before any test ran\n")
    assert (status, blocker) == (VERIFICATION_UNKNOWN, BLOCKER_COLLECTION_ERROR)


def test_a_genuine_pytest_failure_still_fails() -> None:
    assert _interpret_exit("pytest", 1, REAL_FAILURE) == (VERIFICATION_FAILED, None, None)


def _repo_with_interpreter(tmp_path: Path, script: str) -> Path:
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "tests").mkdir()
    for name in ("test_a", "test_b", "test_c"):
        (tmp_path / "tests" / f"{name}.py").write_text(f"def {name}():\n    assert True\n")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(script)
    python.chmod(python.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def test_a_runner_that_cannot_start_is_tried_once_and_names_no_failed_test(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    repo = _repo_with_interpreter(
        tmp_path, f"#!/bin/sh\necho x >> {calls}\necho '{NO_PYTEST.strip()}' >&2\nexit 1\n"
    )
    tests = [MappedTest(id=f"tests/{n}.py::{n}", name=n, case_name=n, repo="app", file=f"tests/{n}.py", line=1)
             for n in ("test_a", "test_b", "test_c")]
    executions, notes = execute_mapped_tests(
        tests=tests, files=load_repo_files("app", repo), repo_root=repo, settings=ExecutionSettings(),
    )
    assert [e.status for e in executions] == [VERIFICATION_UNKNOWN] * 3
    assert {e.blocker for e in executions} == {BLOCKER_RUNNER_MISSING}
    assert all("Test execution unavailable" in (e.reason or "") for e in executions)
    assert calls.read_text().count("x") == 1  # the same environment failure is not re-run per test
    assert any(n.startswith("test_execution_unavailable=pytest") for n in notes)


def test_infrastructure_failure_cannot_produce_action_required(tmp_path: Path) -> None:
    repo = _repo_with_interpreter(tmp_path, f"#!/bin/sh\necho '{NO_PYTEST.strip()}' >&2\nexit 1\n")
    test = MappedTest(id="tests/test_a.py::test_a", name="test_a", case_name="test_a", repo="app",
                      file="tests/test_a.py", line=1)
    [execution], _ = execute_mapped_tests(
        tests=[test], files=load_repo_files("app", repo), repo_root=repo, settings=ExecutionSettings(),
    )
    obligation = VerificationObligation(id="o1", flow_id="f1", kind="route_contract", statement="s",
                                        origin="api_contract", required=True, mapped_tests=[test],
                                        executions=[execution])
    resolve_obligation_status_from_executions(obligation)
    assert obligation.status == VERIFICATION_UNKNOWN and "failed" not in (obligation.reason or "")
    result = ChangeVerificationResult(change=ChangeSet(base="main"))
    result.affected_flows = [AffectedFlow(id="f1", entry_label="POST /x", obligations=[obligation])]
    assert _compute_summary(result).verdict != VERDICT_ACTION_REQUIRED
