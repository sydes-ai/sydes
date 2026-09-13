"""`_compute_summary`'s distinct exercise/support/verify test counts
(`tests_exercising_flows`/`tests_supporting_behavior`/`tests_verifying_behavior`).

Reproduces the exact shape found diagnosing `sydes-examples/nestjs-
boilerplate#1`: real evidence existed only on non-`required` (test-matrix
origin) obligations, so `mapped_tests`/`supporting_tests` (required-only, by
design -- see `derive_obligations`) were both silent about it. These three
counts are computed across *every* obligation instead, specifically so that
real evidence never becomes invisible just because it landed on an advisory
obligation.
"""

from __future__ import annotations

from sydes.verify.analyzer import _compute_summary
from sydes.verify.models import (
    AffectedFlow,
    ChangedFile,
    ChangedSymbol,
    ChangeSet,
    ChangeVerificationResult,
    MappedTest,
    VerificationObligation,
)


def _test(id_: str, name: str = "case") -> MappedTest:
    return MappedTest(id=id_, name=name, evidence_tier="A_direct_invocation", match_rule="x")


def _result(flows: list[AffectedFlow]) -> ChangeVerificationResult:
    change = ChangeSet(
        base="main", head="abc123",
        files=[ChangedFile(repo="app", path="x.py")],
        symbols=[ChangedSymbol(id="x", repo="app", file="x.py", name="x")],
    )
    return ChangeVerificationResult(change=change, affected_flows=flows)


def test_real_evidence_on_a_non_required_obligation_is_still_counted():
    """The core NestJS bug shape: `required=True` route_contract obligation
    has only supporting tests; a `required=False` test-matrix obligation on
    the SAME flow has real, distinct mapped (evidence) tests. Old behavior:
    `mapped_tests` (required-only) == 0. New behavior: the distinct counts
    below see the real evidence regardless of which obligation it landed on.
    """
    required_ob = VerificationObligation(
        id="ob:required", flow_id="flow:x", kind="route_contract", statement="responds 201",
        origin="api_contract", required=True,
        supporting_tests=[_test("t1"), _test("t2")],
    )
    advisory_ob = VerificationObligation(
        id="ob:advisory", flow_id="flow:x", kind="route_contract", statement="contract happy path",
        origin="test_matrix", required=False,
        mapped_tests=[_test("t1"), _test("t2")],
    )
    flow = AffectedFlow(id="flow:x", entry_label="POST /x", obligations=[required_ob, advisory_ob])
    summary = _compute_summary(_result([flow]))

    assert summary.counts.mapped_tests == 0
    assert summary.counts.supporting_tests == 2
    assert summary.counts.tests_verifying_behavior == 2
    assert summary.counts.tests_exercising_flows == 2
    assert summary.counts.tests_supporting_behavior == 0


def test_supporting_only_test_is_not_double_counted_as_verifying():
    ob = VerificationObligation(
        id="ob:1", flow_id="flow:x", kind="route_contract", statement="x",
        origin="api_contract", required=True, supporting_tests=[_test("t1")],
    )
    flow = AffectedFlow(id="flow:x", entry_label="POST /x", obligations=[ob])
    summary = _compute_summary(_result([flow]))

    assert summary.counts.tests_verifying_behavior == 0
    assert summary.counts.tests_supporting_behavior == 1
    assert summary.counts.tests_exercising_flows == 1


def test_same_test_verifying_one_obligation_and_supporting_another_counts_once_as_verifying():
    """A test id that is evidence for one obligation and merely supporting
    for another must not be counted in both buckets -- `exercising` stays
    the union, and `verifying`/`supporting` stay disjoint."""
    ob_a = VerificationObligation(
        id="ob:a", flow_id="flow:x", kind="route_contract", statement="a",
        origin="api_contract", required=True, mapped_tests=[_test("t1")],
    )
    ob_b = VerificationObligation(
        id="ob:b", flow_id="flow:x", kind="validation", statement="b",
        origin="api_contract", required=True, supporting_tests=[_test("t1")],
    )
    flow = AffectedFlow(id="flow:x", entry_label="POST /x", obligations=[ob_a, ob_b])
    summary = _compute_summary(_result([flow]))

    assert summary.counts.tests_verifying_behavior == 1
    assert summary.counts.tests_supporting_behavior == 0
    assert summary.counts.tests_exercising_flows == 1


def test_no_evidence_anywhere_is_all_zero():
    ob = VerificationObligation(
        id="ob:1", flow_id="flow:x", kind="route_contract", statement="x",
        origin="api_contract", required=True,
    )
    flow = AffectedFlow(id="flow:x", entry_label="POST /x", obligations=[ob])
    summary = _compute_summary(_result([flow]))

    assert summary.counts.tests_exercising_flows == 0
    assert summary.counts.tests_supporting_behavior == 0
    assert summary.counts.tests_verifying_behavior == 0
