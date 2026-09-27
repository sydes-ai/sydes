"""`sydes.verify.mutation` -- targeted comparator-boundary mutation
verification (Phase 1 of the test-understanding capability evaluation,
2026-09-26).

Direct, hand-built-result unit tests (matching
`test_mapped_test_execution_policy.py`'s convention): `execute_mapped_test`
is monkeypatched so these never depend on a real pytest/jest/go subprocess,
but the comparator-finding and file-mutate/revert logic runs against a real
file on disk, since that is exactly the part this module exists to get
right.
"""

from __future__ import annotations

from pathlib import Path

from sydes.verify.models import (
    VERIFICATION_FAILED,
    VERIFICATION_PASSED,
    VERIFICATION_UNVERIFIED,
    AffectedFlow,
    ChangeVerificationResult,
    ChangeSet,
    EvidenceRef,
    Hunk,
    MappedTest,
    VerificationObligation,
)
from sydes.verify.mutation import run_mutation_verification
from sydes.verify.test_execution import ExecutionSettings, TestExecution


def _mapped_test(test_id: str, file: str) -> MappedTest:
    return MappedTest(id=test_id, name=test_id, case_name=test_id, file=file)


def _obligation(
    *, kind: str = "validation", introduced: bool = True, status: str = VERIFICATION_PASSED,
    mapped_tests: list[MappedTest] | None = None, evidence_file: str | None = None,
) -> VerificationObligation:
    return VerificationObligation(
        id="flow:x::ob-1", flow_id="flow:x", kind=kind,
        statement="POST /orders enforces `if order.quantity > available_stock:` and responds 400",
        origin="trace_step", introduced_by_change=introduced, status=status,
        mapped_tests=mapped_tests or [],
        evidence=[EvidenceRef(file=evidence_file, symbol="", label="x", snippet="x")] if evidence_file else [],
    )


def _result(obligations: list[VerificationObligation]) -> ChangeVerificationResult:
    flow = AffectedFlow(id="flow:x", entry_label="POST /orders", handler="create_order", obligations=obligations)
    return ChangeVerificationResult(change=ChangeSet(base="main", head="abc"), affected_flows=[flow])


def _write_service(tmp_path: Path) -> Path:
    service = tmp_path / "app" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(
        "\n".join(
            [
                "def create_order(order):",
                "    available_stock = get_stock(order.sku)",
                "    if order.quantity > available_stock:",
                "        raise InsufficientStockError(order.sku)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return service


def test_mutation_survived_when_mapped_test_still_passes(tmp_path, monkeypatch) -> None:
    service = _write_service(tmp_path)
    original_text = service.read_text(encoding="utf-8")
    test = _mapped_test("tests/test_orders.py::test_rejects", "tests/test_orders.py")
    obligation = _obligation(mapped_tests=[test])
    result = _result([obligation])
    hunks = {"app/service.py": [Hunk(start_line=1, end_line=5)]}

    seen_line_during_execution: str | None = None

    def fake_execute(*, test, detections, repo_root, settings):
        nonlocal seen_line_during_execution
        seen_line_during_execution = service.read_text(encoding="utf-8").splitlines()[2]
        return TestExecution(test_id=test.id, framework="pytest", status=VERIFICATION_PASSED)

    monkeypatch.setattr("sydes.verify.mutation.execute_mapped_test", fake_execute)
    monkeypatch.setattr("sydes.verify.mutation.detect_frameworks", lambda files: [])

    run_mutation_verification(
        result, repo_root=tmp_path, repo_files=None,
        changed_file_hunks=hunks, settings=ExecutionSettings(enabled=True),
    )

    assert obligation.mutation is not None
    assert obligation.mutation.status == "mutation_survived"
    assert obligation.mutation.original_operator == ">"
    assert obligation.mutation.mutated_operator == ">="
    assert obligation.mutation.file == "app/service.py"
    assert obligation.mutation.line == 3
    # The mutated file is what the test actually ran against...
    assert seen_line_during_execution == "    if order.quantity >= available_stock:"
    # ...but the file on disk must be back to its original content after.
    assert service.read_text(encoding="utf-8") == original_text


def test_mutation_killed_when_mapped_test_fails(tmp_path, monkeypatch) -> None:
    _write_service(tmp_path)
    test = _mapped_test("tests/test_orders.py::test_rejects", "tests/test_orders.py")
    obligation = _obligation(mapped_tests=[test])
    result = _result([obligation])
    hunks = {"app/service.py": [Hunk(start_line=1, end_line=5)]}

    monkeypatch.setattr(
        "sydes.verify.mutation.execute_mapped_test",
        lambda *, test, detections, repo_root, settings: TestExecution(
            test_id=test.id, framework="pytest", status=VERIFICATION_FAILED,
        ),
    )
    monkeypatch.setattr("sydes.verify.mutation.detect_frameworks", lambda files: [])

    run_mutation_verification(
        result, repo_root=tmp_path, repo_files=None,
        changed_file_hunks=hunks, settings=ExecutionSettings(enabled=True),
    )

    assert obligation.mutation is not None
    assert obligation.mutation.status == "mutation_killed"


def test_execution_exception_is_reported_not_raised_and_file_still_reverted(tmp_path, monkeypatch) -> None:
    """A crash mid-execution must never abort the whole analysis run, and
    the file must still come back exactly as it was (the context manager's
    `finally`, not the broad except, is what guarantees this)."""
    service = _write_service(tmp_path)
    original_text = service.read_text(encoding="utf-8")
    test = _mapped_test("tests/test_orders.py::test_rejects", "tests/test_orders.py")
    obligation = _obligation(mapped_tests=[test])
    result = _result([obligation])
    hunks = {"app/service.py": [Hunk(start_line=1, end_line=5)]}

    def boom(*, test, detections, repo_root, settings):
        raise RuntimeError("simulated runner crash")

    monkeypatch.setattr("sydes.verify.mutation.execute_mapped_test", boom)
    monkeypatch.setattr("sydes.verify.mutation.detect_frameworks", lambda files: [])

    run_mutation_verification(
        result, repo_root=tmp_path, repo_files=None,
        changed_file_hunks=hunks, settings=ExecutionSettings(enabled=True),
    )

    assert obligation.mutation is not None
    assert obligation.mutation.status == "execution_blocked"
    assert "simulated runner crash" in (obligation.mutation.detail or "")
    assert service.read_text(encoding="utf-8") == original_text


def test_ineligible_obligations_are_never_touched(tmp_path, monkeypatch) -> None:
    """Wrong kind, not introduced by this change, or not already passing --
    none of these should even reach `execute_mapped_test`."""
    _write_service(tmp_path)
    hunks = {"app/service.py": [Hunk(start_line=1, end_line=5)]}

    def fail_if_called(*, test, detections, repo_root, settings):
        raise AssertionError("execute_mapped_test should never be called for an ineligible obligation")

    monkeypatch.setattr("sydes.verify.mutation.execute_mapped_test", fail_if_called)
    monkeypatch.setattr("sydes.verify.mutation.detect_frameworks", lambda files: [])

    test = _mapped_test("t", "tests/test_orders.py")
    wrong_kind = _obligation(kind="route_contract", mapped_tests=[test])
    not_introduced = _obligation(introduced=False, mapped_tests=[test])
    not_passing = _obligation(status=VERIFICATION_UNVERIFIED, mapped_tests=[test])
    no_mapped_tests = _obligation(mapped_tests=[])

    result = _result([wrong_kind, not_introduced, not_passing, no_mapped_tests])
    run_mutation_verification(
        result, repo_root=tmp_path, repo_files=None,
        changed_file_hunks=hunks, settings=ExecutionSettings(enabled=True),
    )

    assert all(ob.mutation is None for ob in [wrong_kind, not_introduced, not_passing, no_mapped_tests])


def test_no_comparator_in_hunk_leaves_mutation_unset(tmp_path, monkeypatch) -> None:
    service = tmp_path / "app" / "service.py"
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text("def f():\n    return True\n", encoding="utf-8")
    test = _mapped_test("t", "tests/test_orders.py")
    obligation = _obligation(mapped_tests=[test])
    result = _result([obligation])
    hunks = {"app/service.py": [Hunk(start_line=1, end_line=2)]}

    monkeypatch.setattr(
        "sydes.verify.mutation.execute_mapped_test",
        lambda **_: (_ for _ in ()).throw(AssertionError("must not run without a comparator")),
    )
    monkeypatch.setattr("sydes.verify.mutation.detect_frameworks", lambda files: [])

    run_mutation_verification(
        result, repo_root=tmp_path, repo_files=None,
        changed_file_hunks=hunks, settings=ExecutionSettings(enabled=True),
    )

    assert obligation.mutation is None


def test_budget_caps_at_three_mutations_per_run(tmp_path, monkeypatch) -> None:
    _write_service(tmp_path)
    hunks = {"app/service.py": [Hunk(start_line=1, end_line=5)]}
    monkeypatch.setattr(
        "sydes.verify.mutation.execute_mapped_test",
        lambda *, test, detections, repo_root, settings: TestExecution(
            test_id=test.id, framework="pytest", status=VERIFICATION_PASSED,
        ),
    )
    monkeypatch.setattr("sydes.verify.mutation.detect_frameworks", lambda files: [])

    test = _mapped_test("t", "tests/test_orders.py")
    obligations = [
        VerificationObligation(
            id=f"flow:x::ob-{i}", flow_id="flow:x", kind="validation",
            statement=f"obligation {i}", origin="trace_step",
            introduced_by_change=True, status=VERIFICATION_PASSED, mapped_tests=[test],
        )
        for i in range(4)
    ]
    result = _result(obligations)

    run_mutation_verification(
        result, repo_root=tmp_path, repo_files=None,
        changed_file_hunks=hunks, settings=ExecutionSettings(enabled=True),
    )

    checked = [ob for ob in obligations if ob.mutation is not None]
    assert len(checked) == 3
