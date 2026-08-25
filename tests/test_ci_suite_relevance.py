"""Netflix/dispatch#5837: a Python backend change under
`src/dispatch/signal/views.py` selected the nested frontend
`src/dispatch/static/dispatch/package.json`'s `npm test --silent` as the
*sole* regression suite, which then exited 254 without ever running the 5
backend tests Sydes itself had already identified as exercising the flow.

Root cause: `resolve_ci_test_command` picked the first test command it found
(workflow file, else the first `package.json` with a test script, else the
first detected framework) with zero awareness of *where* the change actually
was — a nested frontend manifest could out-rank a repo-root pytest setup
purely by file-discovery order.

The fix scores every discoverable test command by repository/test topology:
does the command's own working directory actually contain a changed file,
and how specifically (the deepest/closest enclosing directory wins)? No
language or framework is ever preferred by name — `_working_dir_relevance`
is pure path containment.

These tests exercise `resolve_ci_test_command` directly (no subprocess, no
real repository on disk) plus one full `run_ci_suite` pass with a faked
subprocess to confirm the selected command/working_dir actually reach
execution correctly end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sydes.ingest.file_roles import classify_candidate_file_role
from sydes.verify.analyzer import resolve_obligation_status
from sydes.verify.source_files import RepoFiles, SourceFile
from sydes.verify.test_execution import (
    ExecutionSettings,
    FRAMEWORK_PYTEST,
    detect_frameworks,
    resolve_ci_test_command,
    run_ci_suite,
)
from sydes.verify.models import (
    TIER_ASSERTED_EFFECT,
    VERIFICATION_PASSED,
    VERIFICATION_UNVERIFIED,
    CiSuiteRun,
    MappedTest,
    VerificationObligation,
)

_PYPROJECT_WITH_PYTEST = "[tool.pytest.ini_options]\ntestpaths = [\"tests\"]\n"
_PACKAGE_JSON_JEST = '{"scripts": {"test": "jest"}}'


def _file(path: str, text: str = "") -> SourceFile:
    return SourceFile(
        repo="app", path=path, text=text,
        role=classify_candidate_file_role(path), extension=Path(path).suffix.lower(),
    )


def _repo_files(paths_and_text: dict[str, str]) -> RepoFiles:
    files = RepoFiles(repo="app", root=Path("/nonexistent-repo-root"))
    files.files = [_file(path, text) for path, text in paths_and_text.items()]
    return files


# --- Case 1: backend Python change must not select a nested frontend npm ----

def test_backend_change_prefers_root_pytest_over_nested_frontend_npm() -> None:
    files = _repo_files({
        "pyproject.toml": _PYPROJECT_WITH_PYTEST,
        "src/dispatch/signal/views.py": "def return_signal_stats(): ...\n",
        "src/dispatch/static/dispatch/package.json": _PACKAGE_JSON_JEST,
    })
    detections = detect_frameworks(files)
    changed_files = frozenset({"src/dispatch/signal/views.py"})

    resolved, notes = resolve_ci_test_command(files, detections, changed_files=changed_files)

    assert resolved is not None, f"expected a resolved command; notes={notes}"
    command, source, candidate = resolved
    assert candidate.framework == FRAMEWORK_PYTEST
    assert candidate.working_dir == "."
    assert "npm" not in command


# --- Case 2: a frontend-only change may legitimately select the nested npm -

def test_frontend_only_change_may_select_the_nested_npm_script() -> None:
    files = _repo_files({
        "pyproject.toml": _PYPROJECT_WITH_PYTEST,
        "src/dispatch/signal/views.py": "def return_signal_stats(): ...\n",
        "src/dispatch/static/dispatch/package.json": _PACKAGE_JSON_JEST,
    })
    detections = detect_frameworks(files)
    changed_files = frozenset({"src/dispatch/static/dispatch/src/App.jsx"})

    resolved, notes = resolve_ci_test_command(files, detections, changed_files=changed_files)

    assert resolved is not None, f"expected a resolved command; notes={notes}"
    command, source, candidate = resolved
    assert candidate.working_dir == "src/dispatch/static/dispatch"
    assert command[:2] == ["npm", "test"]


# --- Case 3: multiple genuinely relevant suites -----------------------------

def test_multiple_equally_relevant_suites_is_explicit_and_deterministic() -> None:
    """Two backend packages, both touched in the same diff, each with their
    own pytest setup at the same specificity — Sydes must not silently
    arbitrary-pick one; it must say so, and must pick the same one every
    time given the same input."""
    files = _repo_files({
        "services/api/pyproject.toml": _PYPROJECT_WITH_PYTEST,
        "services/api/app.py": "def handler(): ...\n",
        "services/worker/pyproject.toml": _PYPROJECT_WITH_PYTEST,
        "services/worker/task.py": "def run(): ...\n",
    })
    detections = detect_frameworks(files)
    changed_files = frozenset({"services/api/app.py", "services/worker/task.py"})

    resolved_1, notes_1 = resolve_ci_test_command(files, detections, changed_files=changed_files)
    resolved_2, notes_2 = resolve_ci_test_command(files, detections, changed_files=changed_files)

    assert resolved_1 is not None and resolved_2 is not None
    # Deterministic: repeated calls over identical input agree exactly.
    assert resolved_1[2].working_dir == resolved_2[2].working_dir == "services/api"
    # Explicit: the tie is recorded, not silently resolved.
    assert any("ci_suite_multiple_relevant=true" in note for note in notes_1)
    assert any("services/worker" in note for note in notes_1)


# --- Case 4: no safely discoverable relevant runner -------------------------

def _repo_files_on_disk(tmp_path: Path, paths_and_text: dict[str, str]) -> RepoFiles:
    """Like `_repo_files`, but backed by a real temp directory — needed
    whenever a candidate's `runner_available` depends on an actual binary
    existing on disk (e.g. `node_modules/.bin/jest`)."""
    files = RepoFiles(repo="app", root=tmp_path)
    files.files = [_file(path, text) for path, text in paths_and_text.items()]
    return files


def test_no_relevant_runner_is_reported_rather_than_running_an_unrelated_suite(tmp_path: Path) -> None:
    """The only discoverable, actually-runnable test command lives in a
    directory that shares nothing with what changed — Sydes must refuse to
    run it and say so, never silently treat it as having verified the
    change."""
    jest_bin = tmp_path / "src/dispatch/static/dispatch/node_modules/.bin/jest"
    jest_bin.parent.mkdir(parents=True)
    jest_bin.write_text("#!/bin/sh\n")
    files = _repo_files_on_disk(tmp_path, {
        "src/dispatch/signal/views.py": "def return_signal_stats(): ...\n",
        "src/dispatch/static/dispatch/package.json": _PACKAGE_JSON_JEST,
    })
    detections = detect_frameworks(files)
    assert any(d.runner_available for d in detections)  # the jest binary really is "available"
    changed_files = frozenset({"src/dispatch/signal/views.py"})

    resolved, notes = resolve_ci_test_command(files, detections, changed_files=changed_files)

    assert resolved is None
    assert any("none_relevant=true" in note for note in notes)


def test_run_ci_suite_reports_unknown_with_a_clear_reason_when_nothing_is_relevant(
    tmp_path: Path,
) -> None:
    jest_bin = tmp_path / "src/dispatch/static/dispatch/node_modules/.bin/jest"
    jest_bin.parent.mkdir(parents=True)
    jest_bin.write_text("#!/bin/sh\n")
    files = _repo_files_on_disk(tmp_path, {
        "src/dispatch/signal/views.py": "def return_signal_stats(): ...\n",
        "src/dispatch/static/dispatch/package.json": _PACKAGE_JSON_JEST,
    })
    settings = ExecutionSettings(enabled=True)
    changed_files = frozenset({"src/dispatch/signal/views.py"})

    ci_suite, notes = run_ci_suite(
        files=files, repo_root=files.root, settings=settings, changed_files=changed_files,
    )

    assert ci_suite is not None
    assert ci_suite.status == "unknown"
    assert ci_suite.reason is not None
    assert "unrelated suite" in ci_suite.reason
    assert any("ci_suite=unresolved" in note for note in notes)


# --- Full pipeline: the correctly-selected command actually gets run -------

def test_run_ci_suite_executes_the_relevant_backend_command_not_the_frontend_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _repo_files({
        "pyproject.toml": _PYPROJECT_WITH_PYTEST,
        "src/dispatch/signal/views.py": "def return_signal_stats(): ...\n",
        "src/dispatch/static/dispatch/package.json": _PACKAGE_JSON_JEST,
    })
    settings = ExecutionSettings(enabled=True)
    changed_files = frozenset({"src/dispatch/signal/views.py"})

    captured: dict = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "===== 5 passed in 0.10s ====="
        stderr = ""

    def fake_run(command, cwd=None, capture_output=True, text=True, timeout=None, check=False):
        captured["command"] = command
        captured["cwd"] = cwd
        return _FakeCompleted()

    monkeypatch.setattr("sydes.verify.test_execution.subprocess.run", fake_run)

    ci_suite, notes = run_ci_suite(
        files=files, repo_root=files.root, settings=settings, changed_files=changed_files,
    )

    assert ci_suite is not None
    assert ci_suite.working_dir == "."
    assert "npm" not in captured["command"]
    assert captured["cwd"] == str(files.root)  # "." working_dir, never the nested frontend dir


# --- mapped vs supporting vs executed: confirming the semantics, not
# changing them (Task: "Do not weaken obligation semantics just to make
# tests pass") -----------------------------------------------------------

def test_a_green_ci_suite_never_verifies_an_obligation_with_no_mapped_test() -> None:
    """This is the exact dispatch#5837 shape once the CI suite command is
    fixed: 5 relevant tests get executed as part of a correctly-selected
    backend suite (`tests_executed` > 0), but none of them was strict
    enough to be *mapped* (a `MappedTest` directly asserting this specific
    obligation) — only *supporting* (it exercises the flow without proving
    the obligation). The obligation must stay UNVERIFIED regardless of how
    green the suite is; a passing suite is regression evidence, never proof
    of an unmapped behavior."""
    obligation = VerificationObligation(
        id="ob:1", flow_id="flow:1", kind="route_contract", statement="GET /stats returns signal stats",
        origin="api_contract", required=True,
        mapped_tests=[],  # the strict matcher found no test that *directly asserts* this
        supporting_tests=[
            MappedTest(
                id="t:1", name="test_signal_stats_endpoint", evidence_tier=TIER_ASSERTED_EFFECT,
                match_rule="calls GET /stats but does not assert the specific response shape",
            ),
        ],
    )
    green_suite = CiSuiteRun(
        command=["python3", "-m", "pytest"], source="detected:pytest:.", working_dir=".",
        framework="pytest", status=VERIFICATION_PASSED, tests_passed=5, tests_failed=0,
    )

    resolve_obligation_status(obligation, green_suite)

    assert obligation.status == VERIFICATION_UNVERIFIED
    assert "exercise this flow but none assert this behavior" in obligation.reason
    # The supporting test is not discarded either — it's exactly what a
    # reviewer needs to see to judge the gap for themselves.
    assert len(obligation.supporting_tests) == 1


def test_a_mapped_test_can_still_be_verified_by_the_same_green_suite() -> None:
    """Contrast case: once a test *is* mapped (it directly asserts the
    obligation), the same green suite legitimately verifies it — the fix in
    this task is about which suite runs, never about loosening what counts
    as a mapped test."""
    obligation = VerificationObligation(
        id="ob:2", flow_id="flow:1", kind="route_contract", statement="GET /stats returns signal stats",
        origin="api_contract", required=True,
        mapped_tests=[
            MappedTest(id="t:2", name="test_signal_stats_shape", evidence_tier=TIER_ASSERTED_EFFECT,
                       match_rule="asserts the exact response payload for GET /stats"),
        ],
    )
    green_suite = CiSuiteRun(
        command=["python3", "-m", "pytest"], source="detected:pytest:.", working_dir=".",
        framework="pytest", status=VERIFICATION_PASSED, tests_passed=5, tests_failed=0, failed_test_ids=[],
    )

    resolve_obligation_status(obligation, green_suite)

    assert obligation.status == VERIFICATION_PASSED
