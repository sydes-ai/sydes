"""Test-execution tests for verify-change V2.

Every case runs a real subprocess against a temporary repository. Nothing here
touches the network or any external service.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from sydes.verify.models import (
    BLOCKER_FRAMEWORK_UNSUPPORTED,
    BLOCKER_MISSING_DEPENDENCY,
    BLOCKER_NO_TESTS_COLLECTED,
    BLOCKER_RUNNER_MISSING,
    BLOCKER_TIMEOUT,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    MappedTest,
)
from sydes.verify.repo_scan import scan_repository
from sydes.verify.test_execution import (
    FRAMEWORK_JEST,
    FRAMEWORK_PYTEST,
    FRAMEWORK_UNITTEST,
    MAX_OUTPUT_CHARS,
    ExecutionSettings,
    detect_frameworks,
    execute_mapped_test,
    execute_mapped_tests,
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture()
def pytest_repo(tmp_path: Path) -> Path:
    """A repo that declares pytest and has one passing and one failing case."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest>=8"]\n')
    _write(
        root,
        "tests/test_items.py",
        "def test_passes():\n"
        "    assert 1 == 1\n"
        "\n"
        "def test_fails():\n"
        "    expected = 1\n"
        "    actual = 2\n"
        "    assert actual == expected\n",
    )
    return root


def _mapped(file: str, case: str, *, suite: str | None = None) -> MappedTest:
    return MappedTest(
        id=f"{file}::{case}",
        name=f"{suite} :: {case}" if suite else case,
        case_name=case,
        file=file,
        line=1,
        suite=suite,
    )


def _run(root: Path, test: MappedTest, *, timeout: float = 60.0):
    scan = scan_repository("svc", root)
    return execute_mapped_test(
        test=test,
        detections=detect_frameworks(scan),
        repo_root=root,
        settings=ExecutionSettings(timeout_seconds=timeout),
    )


# --- framework / runner detection ------------------------------------------


def test_detects_pytest_from_declared_dependency(pytest_repo: Path) -> None:
    """pytest is detected from the dependency declaration, not guessed."""
    detections = detect_frameworks(scan_repository("svc", pytest_repo))

    assert [item.framework for item in detections] == [FRAMEWORK_PYTEST]
    assert detections[0].evidence.file == "pyproject.toml"


def test_detects_unittest_from_test_module_contents(tmp_path: Path) -> None:
    """A unittest-based repo with no pytest config is detected as unittest."""
    root = tmp_path / "svc"
    _write(root, "requirements.txt", "flask==2.3.3\n")
    _write(
        root,
        "tests/test_app.py",
        "import unittest\n"
        "\n"
        "class TestApp(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertEqual(1, 1)\n",
    )

    detections = detect_frameworks(scan_repository("svc", root))

    assert [item.framework for item in detections] == [FRAMEWORK_UNITTEST]


def test_no_framework_detected_without_evidence(tmp_path: Path) -> None:
    """A repo with tests but no declared runner yields no detection."""
    root = tmp_path / "svc"
    _write(root, "tests/test_thing.py", "def test_ok():\n    assert True\n")

    assert detect_frameworks(scan_repository("svc", root)) == []


def test_unsupported_language_is_unknown_not_failed(tmp_path: Path) -> None:
    """A Java test yields `unknown`, never a product failure."""
    root = tmp_path / "svc"
    _write(root, "src/test/java/AppTest.java", "class AppTest { void testOk() {} }\n")

    execution = _run(root, _mapped("src/test/java/AppTest.java", "testOk"))

    assert execution.status == VERIFICATION_UNKNOWN
    assert execution.blocker == BLOCKER_FRAMEWORK_UNSUPPORTED


def test_declared_but_uninstalled_runner_is_unknown(tmp_path: Path) -> None:
    """jest declared in package.json but absent from node_modules is `unknown`."""
    root = tmp_path / "svc"
    _write(root, "package.json", '{"devDependencies": {"jest": "^29.0.0"}}')
    _write(root, "tests/app.test.js", "test('ok', () => {});\n")

    scan = scan_repository("svc", root)
    detections = detect_frameworks(scan)
    assert [item.framework for item in detections] == [FRAMEWORK_JEST]
    assert detections[0].runner_available is False

    execution = _run(root, _mapped("tests/app.test.js", "ok"))

    assert execution.status == VERIFICATION_UNKNOWN
    assert execution.blocker == BLOCKER_RUNNER_MISSING
    assert "not installed" in (execution.reason or "")


# --- execution outcomes -----------------------------------------------------


def test_passing_case_is_executed_and_reported_passed(pytest_repo: Path) -> None:
    """A selected pytest case runs and reports `passed` with real timing."""
    execution = _run(pytest_repo, _mapped("tests/test_items.py", "test_passes"))

    assert execution.status == VERIFICATION_PASSED
    assert execution.exit_code == 0
    assert execution.granularity == "case"
    assert execution.duration_ms is not None and execution.duration_ms >= 0
    assert "tests/test_items.py::test_passes" in execution.command


def test_failing_case_is_reported_failed_with_a_summary(pytest_repo: Path) -> None:
    """A failing case reports `failed` and keeps the assertion detail."""
    execution = _run(pytest_repo, _mapped("tests/test_items.py", "test_fails"))

    assert execution.status == VERIFICATION_FAILED
    assert execution.exit_code == 1
    assert execution.blocker is None
    assert execution.failure_summary is not None
    assert "assert" in execution.failure_summary.lower()


def test_file_granularity_runs_the_whole_file(pytest_repo: Path) -> None:
    """A test with no identifiable case runs at file granularity."""
    execution = _run(pytest_repo, _mapped("tests/test_items.py", "test_items"))

    assert execution.granularity == "file"
    assert execution.status == VERIFICATION_FAILED  # the file holds a failing case
    assert "tests/test_items.py" in execution.command


def test_unknown_case_widens_to_the_file_and_records_it(pytest_repo: Path) -> None:
    """A case name that collects nothing widens to the file rather than lying."""
    execution = _run(pytest_repo, _mapped("tests/test_items.py", "test_does_not_exist"))

    assert execution.granularity == "file"
    assert "widened to the whole file" in (execution.reason or "")


def test_timeout_is_distinguishable_from_a_failure(tmp_path: Path) -> None:
    """A hanging test is `unknown` with a timeout blocker, never `failed`."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(root, "tests/test_slow.py", "import time\n\ndef test_slow():\n    time.sleep(30)\n")

    execution = _run(root, _mapped("tests/test_slow.py", "test_slow"), timeout=2.0)

    assert execution.status == VERIFICATION_UNKNOWN
    assert execution.blocker == BLOCKER_TIMEOUT
    assert execution.status != VERIFICATION_FAILED


def test_missing_dependency_is_unknown_with_the_module_named(tmp_path: Path) -> None:
    """An unimportable dependency is an environment problem, not a test failure."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(
        root,
        "tests/test_needs_dep.py",
        "import a_module_that_does_not_exist_anywhere\n\ndef test_ok():\n    assert True\n",
    )

    execution = _run(root, _mapped("tests/test_needs_dep.py", "test_ok"))

    assert execution.status == VERIFICATION_UNKNOWN
    assert execution.blocker == BLOCKER_MISSING_DEPENDENCY
    assert execution.missing_dependency == "a_module_that_does_not_exist_anywhere"


def test_collection_error_is_unknown(tmp_path: Path) -> None:
    """A test file that cannot be imported is `unknown`, not `failed`."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(root, "tests/test_broken.py", "def test_ok(:\n    pass\n")

    execution = _run(root, _mapped("tests/test_broken.py", "test_ok"))

    assert execution.status == VERIFICATION_UNKNOWN
    assert execution.blocker in {BLOCKER_NO_TESTS_COLLECTED, "collection_error"}


def test_missing_interpreter_is_reported_as_a_process_error(tmp_path: Path, monkeypatch) -> None:
    """A runner executable that does not exist is `unknown`, not a crash."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(root, "tests/test_a.py", "def test_ok():\n    assert True\n")
    monkeypatch.setattr(
        "sydes.verify.test_execution._repo_python",
        lambda root, working_dir=".": (["sydes-no-such-interpreter"], False),
    )

    execution = _run(root, _mapped("tests/test_a.py", "test_ok"))

    assert execution.status == VERIFICATION_UNKNOWN
    assert execution.blocker == "process_error"


def test_output_is_truncated_but_keeps_the_failure_tail(tmp_path: Path) -> None:
    """Runner output is bounded; the tail holding the failure survives."""
    root = tmp_path / "svc"
    _write(root, "pyproject.toml", '[project]\nname = "svc"\ndependencies = ["pytest"]\n')
    _write(
        root,
        "tests/test_loud.py",
        "def test_loud():\n"
        "    for index in range(4000):\n"
        "        print(f'noise line {index} ' + 'x' * 60)\n"
        "    assert False\n",
    )

    execution = _run(root, _mapped("tests/test_loud.py", "test_loud"))

    assert execution.status == VERIFICATION_FAILED
    assert execution.output_truncated is True
    assert len(execution.stdout_excerpt or "") <= MAX_OUTPUT_CHARS + 64


def test_execution_is_skipped_when_disabled(pytest_repo: Path) -> None:
    """`enabled=False` runs no subprocess at all."""
    scan = scan_repository("svc", pytest_repo)

    executions, notes = execute_mapped_tests(
        tests=[_mapped("tests/test_items.py", "test_passes")],
        scan=scan,
        repo_root=pytest_repo,
        settings=ExecutionSettings(enabled=False),
    )

    assert executions == []
    assert "test_execution=disabled" in notes


def test_each_mapped_test_is_executed_once(pytest_repo: Path) -> None:
    """A test mapped to several behaviors is not run repeatedly."""
    test = _mapped("tests/test_items.py", "test_passes")
    scan = scan_repository("svc", pytest_repo)

    executions, _ = execute_mapped_tests(
        tests=[test, test],
        scan=scan,
        repo_root=pytest_repo,
        settings=ExecutionSettings(),
    )

    assert len(executions) == 1


def test_command_is_argv_not_a_shell_string(pytest_repo: Path) -> None:
    """Commands are argv lists, so repository names cannot be interpolated."""
    execution = _run(pytest_repo, _mapped("tests/test_items.py", "test_passes"))

    assert isinstance(execution.command, list)
    assert all(isinstance(part, str) for part in execution.command)
    assert execution.command[0].endswith(("python", "python3", "python.exe")) or execution.command[
        0
    ] == sys.executable


def test_monorepo_manifest_in_a_subdirectory_is_detected(tmp_path: Path) -> None:
    """A runner declared in a package subdirectory is found, not just at the root."""
    root = tmp_path / "mono"
    _write(root, "package.json", '{"name": "mono"}')
    _write(root, "backend/package.json", '{"devDependencies": {"jest": "^29.0.0"}}')
    _write(root, "backend/src/tests/app.test.js", "test('ok', () => {});\n")

    detections = detect_frameworks(scan_repository("mono", root))

    assert [item.framework for item in detections] == [FRAMEWORK_JEST]
    assert detections[0].working_dir == "backend"
    assert detections[0].evidence.file == "backend/package.json"


def test_monorepo_command_paths_are_relative_to_the_package(tmp_path: Path) -> None:
    """Commands address the test relative to the runner's working directory."""
    root = tmp_path / "mono"
    _write(root, "backend/pyproject.toml", '[project]\nname = "b"\ndependencies = ["pytest"]\n')
    _write(root, "backend/tests/test_items.py", "def test_ok():\n    assert True\n")

    execution = _run(root, _mapped("backend/tests/test_items.py", "test_ok"))

    assert execution.status == VERIFICATION_PASSED
    assert "tests/test_items.py::test_ok" in execution.command
