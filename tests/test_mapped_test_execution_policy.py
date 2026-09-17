"""`sydes.verify.analyzer._run_test_execution`'s mapped-test-first execution
policy: run an obligation's own mapped test(s) individually instead of
defaulting to the whole repo suite (`execute_mapped_tests` existed, already
production-shaped, but had no caller before this).

These are direct, hand-built-result unit tests (not full CLI runs) so the
policy branches (A: mapped-only, B: whole-suite fallback, C: partial) can be
exercised precisely without depending on which real obligations a route
happens to produce mapped tests for.
"""

from __future__ import annotations

from pathlib import Path

from sydes.verify.analyzer import VerifyChangeOptions, _run_test_execution
from sydes.verify.models import (
    AffectedFlow,
    ChangeSet,
    ChangeVerificationResult,
    MappedTest,
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNKNOWN,
    VerificationObligation,
)
from sydes.verify.test_execution import BLOCKER_FRAMEWORK_UNSUPPORTED, TestExecution


def _mapped_test(test_id: str, file: str = "test/a.spec.ts") -> MappedTest:
    return MappedTest(id=test_id, name=test_id, case_name=test_id, file=file)


def _result_with_two_obligations(test_a: MappedTest, test_b: MappedTest) -> ChangeVerificationResult:
    flow = AffectedFlow(
        id="flow:x", entry_label="POST /x", handler="create",
        obligations=[
            VerificationObligation(
                id="flow:x::ob-1", flow_id="flow:x", kind="validation",
                statement="rejects invalid input", origin="trace_step",
                mapped_tests=[test_a], required=True, introduced_by_change=True,
            ),
            VerificationObligation(
                id="flow:x::ob-2", flow_id="flow:x", kind="side_effect",
                statement="dispatches an event", origin="trace_sink",
                mapped_tests=[test_b], required=True, introduced_by_change=True,
            ),
        ],
    )
    return ChangeVerificationResult(change=ChangeSet(base="main", head="abc"), affected_flows=[flow])


def _execution(test_id: str, status: str, *, blocker: str | None = None, reason: str | None = None) -> TestExecution:
    return TestExecution(test_id=test_id, framework="jest", status=status, blocker=blocker, reason=reason)


def _run(result: ChangeVerificationResult, monkeypatch, execute_mapped_impl=None, ci_suite_impl=None):
    if execute_mapped_impl is not None:
        monkeypatch.setattr("sydes.verify.analyzer.execute_mapped_tests", execute_mapped_impl)
    if ci_suite_impl is not None:
        monkeypatch.setattr("sydes.verify.analyzer.run_ci_suite", ci_suite_impl)
    _run_test_execution(
        result, VerifyChangeOptions(run_tests=True), repo_files=None, repo_root=Path("/tmp/nonexistent"),
        changed_files=set(),
    )


def test_policy_a_mapped_tests_run_individually_whole_suite_never_called(monkeypatch) -> None:
    test_a, test_b = _mapped_test("a::1"), _mapped_test("b::1")
    result = _result_with_two_obligations(test_a, test_b)

    def _fake_execute(*, tests, files, repo_root, settings):
        assert {t.id for t in tests} == {"a::1", "b::1"}
        return [_execution("a::1", VERIFICATION_PASSED), _execution("b::1", VERIFICATION_PASSED)], []

    def _fail_if_called(*a, **k):
        raise AssertionError("run_ci_suite must not be called when mapped tests are executable")

    _run(result, monkeypatch, execute_mapped_impl=_fake_execute, ci_suite_impl=_fail_if_called)

    obligations = result.affected_flows[0].obligations
    assert all(item.status == VERIFICATION_PASSED for item in obligations)
    assert all(item.executions for item in obligations)
    assert any("test_execution=mapped_tests" in note for note in result.notes)


def test_policy_b_falls_back_to_whole_suite_when_nothing_individually_executable(monkeypatch) -> None:
    test_a, test_b = _mapped_test("a::1"), _mapped_test("b::1")
    result = _result_with_two_obligations(test_a, test_b)

    def _all_blocked(*, tests, files, repo_root, settings):
        return [
            _execution(t.id, VERIFICATION_UNKNOWN, blocker=BLOCKER_FRAMEWORK_UNSUPPORTED, reason="cannot invoke")
            for t in tests
        ], []

    calls = {"n": 0}

    def _fake_ci_suite(*, files, repo_root, settings, changed_files):
        calls["n"] += 1
        return None, ["ci_suite=disabled"]

    _run(result, monkeypatch, execute_mapped_impl=_all_blocked, ci_suite_impl=_fake_ci_suite)

    assert calls["n"] == 1, "whole-suite fallback must run when nothing was individually executable"
    assert any(
        "test_execution=whole_suite_fallback" in note and "no_mapped_test_individually_executable" in note
        for note in result.notes
    )


def test_policy_b_falls_back_when_there_are_no_mapped_tests_at_all(monkeypatch) -> None:
    flow = AffectedFlow(
        id="flow:x", entry_label="POST /x", handler="create",
        obligations=[
            VerificationObligation(
                id="flow:x::ob-1", flow_id="flow:x", kind="validation",
                statement="rejects invalid input", origin="trace_step",
                mapped_tests=[], required=True,
            ),
        ],
    )
    result = ChangeVerificationResult(change=ChangeSet(base="main", head="abc"), affected_flows=[flow])

    calls = {"n": 0}

    def _fake_ci_suite(*, files, repo_root, settings, changed_files):
        calls["n"] += 1
        return None, ["ci_suite=disabled"]

    _run(result, monkeypatch, ci_suite_impl=_fake_ci_suite)

    assert calls["n"] == 1
    assert any("test_execution=whole_suite_fallback" in note and "no_mapped_tests" in note for note in result.notes)


def test_policy_c_partial_set_attributes_what_ran_and_reports_what_did_not(monkeypatch) -> None:
    """One mapped test runs and fails; the OTHER obligation's mapped test is
    unexecutable. The failing one must be attributed FAILED; the
    unexecutable one must be reported honestly, never silently swapped for
    an unrelated whole-suite result -- and the whole-suite fallback must
    never run at all in this mixed case."""
    test_a, test_b = _mapped_test("a::1"), _mapped_test("b::1")
    result = _result_with_two_obligations(test_a, test_b)

    def _mixed(*, tests, files, repo_root, settings):
        out = []
        for t in tests:
            if t.id == "a::1":
                out.append(_execution("a::1", VERIFICATION_FAILED))
            else:
                out.append(
                    _execution(
                        "b::1", VERIFICATION_UNKNOWN,
                        blocker=BLOCKER_FRAMEWORK_UNSUPPORTED, reason="cannot invoke individually",
                    )
                )
        return out, []

    def _fail_if_called(*a, **k):
        raise AssertionError("run_ci_suite must not run for a mixed executable/unexecutable set")

    _run(result, monkeypatch, execute_mapped_impl=_mixed, ci_suite_impl=_fail_if_called)

    ob_a, ob_b = result.affected_flows[0].obligations
    assert ob_a.status == VERIFICATION_FAILED
    assert ob_a.reason == "`a::1` failed"
    assert ob_b.status == VERIFICATION_UNKNOWN
    assert ob_b.reason == "cannot invoke individually"
    assert not any("test_execution=whole_suite_fallback" in note for note in result.notes)


def test_unrelated_execution_never_contaminates_a_different_obligations_status(monkeypatch) -> None:
    """The individual-execution policy's whole point: one obligation's own
    mapped test failing must never flip a DIFFERENT obligation's own
    passing mapped test to failed (the old whole-suite pass/fail could not
    tell these apart; per-test attribution can)."""
    test_a, test_b = _mapped_test("a::1"), _mapped_test("b::1")
    result = _result_with_two_obligations(test_a, test_b)

    def _mixed(*, tests, files, repo_root, settings):
        return [
            _execution("a::1", VERIFICATION_PASSED),
            _execution("b::1", VERIFICATION_FAILED),
        ], []

    _run(result, monkeypatch, execute_mapped_impl=_mixed)

    ob_a, ob_b = result.affected_flows[0].obligations
    assert ob_a.status == VERIFICATION_PASSED
    assert ob_b.status == VERIFICATION_FAILED
