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


def test_a_runner_that_cannot_start_is_tried_once_and_names_no_failed_test(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SYDES_TEST_PYTHON", raising=False)  # the repository's own (broken) .venv
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


def test_infrastructure_failure_cannot_produce_action_required(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SYDES_TEST_PYTHON", raising=False)
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


# ----------------------------------------------------------------------------- target environment


def test_the_target_repositorys_environment_is_selected_not_sydes(tmp_path: Path, monkeypatch) -> None:
    from sydes.verify.test_execution import resolve_python_environment

    monkeypatch.delenv("SYDES_TEST_PYTHON", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    repo = _repo_with_interpreter(tmp_path, "#!/bin/sh\nexit 0\n")
    env = resolve_python_environment(repo)
    assert env.owner == "target" and env.argv == [str(repo / ".venv" / "bin" / "python")]
    assert "repository virtualenv" in env.reason


def test_no_target_environment_is_unavailable_never_sydes_python(tmp_path: Path, monkeypatch) -> None:
    import sys

    from sydes.verify.test_execution import (
        NO_TARGET_ENVIRONMENT,
        resolve_python_environment,
    )

    monkeypatch.delenv("SYDES_TEST_PYTHON", raising=False)
    # Sydes' own environment is active (as under `uv run`): it must not stand in for the target's
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n[tool.uv]\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = resolve_python_environment(tmp_path)
    assert env.argv is None and env.owner == "none" and env.reason.startswith(NO_TARGET_ENVIRONMENT)
    test = MappedTest(id="t::a", name="a", case_name="a", repo="app", file="tests/test_a.py", line=1)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
    [execution], notes = execute_mapped_tests(tests=[test], files=load_repo_files("app", tmp_path),
                                              repo_root=tmp_path, settings=ExecutionSettings())
    assert execution.status == VERIFICATION_UNKNOWN and execution.blocker == BLOCKER_RUNNER_MISSING
    assert NO_TARGET_ENVIRONMENT in (execution.reason or "")
    assert any(n.startswith("test_environment=pytest owner=none interpreter=none") for n in notes)


def test_a_declared_environment_manager_command_is_not_run_as_declared() -> None:
    from sydes.core.models import EvidenceRef
    from sydes.verify.test_execution import (
        FrameworkDetection,
        _resolve_declared_runner_argv,
    )

    target = FrameworkDetection(framework="pytest", language="python", evidence=EvidenceRef(file="pytest.ini"),
                                runner_argv=["/repo/.venv/bin/python", "-m", "pytest"])
    assert _resolve_declared_runner_argv(["uv", "run", "--extra", "test", "pytest", "api/tests"], [target]) == (
        ["/repo/.venv/bin/python", "-m", "pytest", "api/tests"], ".", "pytest")
    missing = FrameworkDetection(framework="pytest", language="python", evidence=EvidenceRef(file="pytest.ini"),
                                 runner_available=False)
    assert _resolve_declared_runner_argv(["uv", "run", "pytest"], [missing]) is None
    assert _resolve_declared_runner_argv(["poetry", "install"], [target]) is None
