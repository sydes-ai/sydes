"""`sydes.recovery.canonical_merge` -- the one deliberate exception to
"recovery never touches the structural result": a VERIFIED/ESTABLISHED
finding is merged additively into `ChangeVerificationResult` so the
renderer (which reads `affected_flows`/`accepted_impacts`/`summary.counts`,
never `notes`) actually shows it.
"""

from __future__ import annotations

from sydes.recovery.canonical_merge import PROVENANCE_AI_RECOVERY, merge_verified_recovery_into_result
from sydes.recovery.schema import (
    EntityRef,
    PathRecoveryResult,
    RecoveredEdge,
    RecoveredPath,
    RecoveredTest,
    STATUS_ESTABLISHED,
    STATUS_PARTIAL,
    STATUS_UNRESOLVED,
    TEST_STATUS_ACCEPTED,
    TEST_STATUS_REJECTED,
    TestRecoveryResult,
)
from sydes.verify.models import (
    AcceptedImpact,
    AffectedFlow,
    ChangedSymbol,
    ChangeSet,
    ChangeSummary,
    ChangeVerificationResult,
    MappedTest,
    SourceRef,
    VerificationCounts,
    VerificationObligation,
)

REPO = "app"


def _entity(symbol: str, file: str) -> EntityRef:
    return EntityRef(symbol=symbol, file=file)


def _result(*, changed_symbols=None, **overrides) -> ChangeVerificationResult:
    overrides.setdefault("summary", ChangeSummary(counts=VerificationCounts()))
    change = ChangeSet(base="main", head="abc", symbols=changed_symbols or [])
    return ChangeVerificationResult(change=change, **overrides)


def _established_path(entrypoint="GET /users", nodes=None) -> PathRecoveryResult:
    nodes = nodes or [_entity("Controller.findUsers", "controller.ts"), _entity("Handler.execute", "handler.ts")]
    target = nodes[-1]
    edges = []
    for a, b in zip(nodes, nodes[1:]):
        edges.append(RecoveredEdge(**{"from": a, "to": b}, relationship="dispatches to", status=STATUS_ESTABLISHED))
    path = RecoveredPath(entrypoint=entrypoint, target_node=target.symbol, nodes=nodes, edges=edges, status=STATUS_ESTABLISHED)
    return PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[path])


def _recovered_test(file="a.spec.ts", test="t1", target=None, status=TEST_STATUS_ACCEPTED) -> RecoveredTest:
    return RecoveredTest(file=file, test=test, covers="c", target=target or _entity("Handler.execute", "handler.ts"), status=status)


def test_established_path_appears_in_canonical_affected_flows():
    result = _result()
    merge_verified_recovery_into_result(result, _established_path(), TestRecoveryResult())
    assert len(result.affected_flows) == 1
    flow = result.affected_flows[0]
    assert flow.entry_label == "GET /users"
    assert flow.handler == "Controller.findUsers"
    assert flow.changed_nodes[0].symbol == "Handler.execute"
    assert flow.changed_nodes[0].file == "handler.ts"


def test_counts_affected_flows_matches_the_list_length_after_merge():
    """Issue 7 invariant: counts must match lists. counts.affected_flows is
    defined as len(result.affected_flows) -- appending a recovered flow
    without also bumping this count would silently desync the summary
    from the list it's supposed to describe."""
    result = _result(
        affected_flows=[AffectedFlow(id="flow:pre-existing", entry_label="GET /pre-existing")],
        summary=ChangeSummary(counts=VerificationCounts(affected_flows=1)),
    )
    merge_verified_recovery_into_result(result, _established_path(), TestRecoveryResult())

    assert len(result.affected_flows) == 2
    assert result.summary.counts.affected_flows == 2


def test_established_impact_is_visible_to_the_renderer_without_notes():
    """Mirrors exactly what the external renderer reads: an
    `AffectedFlow.id` matching an `AcceptedImpact.id` with `status ==
    "proven"` is what makes `select_representative_paths` treat a flow as
    ESTABLISHED -- never a parse of `notes`."""
    result = _result()
    merge_verified_recovery_into_result(result, _established_path(), TestRecoveryResult())
    flow = result.affected_flows[0]
    impacts_by_id = {imp.id: imp.status for imp in result.accepted_impacts}
    assert impacts_by_id.get(flow.id) == "proven"


def test_zero_hop_path_has_no_handler_segment():
    """Entrypoint and target are the SAME node (see
    `sydes.recovery.agent._direct_entrypoint_edge`) -- there is no
    separate handler to show, just route -> symbol."""
    node = _entity("UserController.getUser", "UserController.java")
    path = RecoveredPath(
        entrypoint="GET /user/{id}", target_node=node.symbol, nodes=[node],
        edges=[RecoveredEdge(**{"from": node, "to": node}, relationship="self", status=STATUS_ESTABLISHED)],
        status=STATUS_ESTABLISHED,
    )
    result = _result()
    merge_verified_recovery_into_result(result, PathRecoveryResult(status=STATUS_ESTABLISHED, paths=[path]), TestRecoveryResult())
    flow = result.affected_flows[0]
    assert flow.handler is None
    assert flow.changed_nodes[0].symbol == "UserController.getUser"


def test_unresolved_recovery_leaves_result_completely_untouched():
    result = _result()
    before = result.model_copy(deep=True)
    merge_verified_recovery_into_result(result, PathRecoveryResult(status=STATUS_UNRESOLVED), TestRecoveryResult(status=STATUS_UNRESOLVED))
    assert result == before


def test_partial_path_recovery_does_not_merge_a_path():
    result = _result()
    before = result.model_copy(deep=True)
    partial = PathRecoveryResult(status=STATUS_PARTIAL, paths=[])
    merge_verified_recovery_into_result(result, partial, TestRecoveryResult())
    assert result.affected_flows == before.affected_flows


def test_recovered_test_increments_mapped_tests_count():
    result = _result()
    test_recovery = TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[_recovered_test()])
    merge_verified_recovery_into_result(result, PathRecoveryResult(), test_recovery)
    assert result.summary.counts.mapped_tests == 1
    assert result.summary.counts.supporting_tests == 1


def test_recovered_test_never_becomes_executed():
    result = _result()
    test_recovery = TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[_recovered_test()])
    merge_verified_recovery_into_result(result, PathRecoveryResult(), test_recovery)
    assert result.summary.counts.tests_executed == 0
    assert result.test_executions == []


def test_rejected_test_is_not_counted():
    result = _result()
    test_recovery = TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[_recovered_test(status=TEST_STATUS_REJECTED)])
    merge_verified_recovery_into_result(result, PathRecoveryResult(), test_recovery)
    assert result.summary.counts.mapped_tests == 0


def test_no_duplicate_flow_when_structural_result_already_covers_the_route():
    """An existing flow (any provenance) for the same entrypoint ->
    changed-symbol pair must never get a second, recovery-derived twin."""
    existing = AffectedFlow(
        id="flow:structural:1", entry_label="GET /users", handler="Controller.findUsers",
        changed_nodes=[SourceRef(file="handler.ts", symbol="Handler.execute")],
    )
    result = _result(affected_flows=[existing])
    merge_verified_recovery_into_result(result, _established_path(), TestRecoveryResult())
    assert len(result.affected_flows) == 1
    assert result.affected_flows[0] is existing


def test_no_duplicate_test_when_already_mapped_via_an_existing_obligation():
    mapped = MappedTest(id="t1", name="t1", file="a.spec.ts", match_rule="pre-existing")
    obligation = VerificationObligation(id="ob1", flow_id="flow1", kind="test", statement="x", origin="y", mapped_tests=[mapped])
    flow = AffectedFlow(id="flow1", entry_label="GET /users", obligations=[obligation])
    result = _result(affected_flows=[flow], summary=ChangeSummary(counts=VerificationCounts(mapped_tests=1)))
    test_recovery = TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[_recovered_test(file="a.spec.ts", test="t1")])
    merge_verified_recovery_into_result(result, PathRecoveryResult(), test_recovery)
    assert result.summary.counts.mapped_tests == 1  # unchanged -- already mapped


def test_structural_evidence_is_never_downgraded_or_replaced():
    existing = AffectedFlow(id="flow:structural:1", entry_label="GET /users", handler="Controller.findUsers", status="verified")
    result = _result(affected_flows=[existing])
    before_flow = existing.model_copy(deep=True)
    merge_verified_recovery_into_result(
        result,
        _established_path(entrypoint="GET /users", nodes=[_entity("Controller.findUsers", "controller.ts")]),
        TestRecoveryResult(),
    )
    assert result.affected_flows[0] == before_flow  # the structural flow itself, untouched
    # No new flow either -- "Controller.findUsers" (the single-node target
    # here) already appears as this existing flow's own handler.
    assert len(result.affected_flows) == 1


def test_provenance_is_recorded_on_merged_entries():
    result = _result()
    merge_verified_recovery_into_result(result, _established_path(), TestRecoveryResult())
    assert result.affected_flows[0].provenance == PROVENANCE_AI_RECOVERY
    assert result.accepted_impacts[0].provenance == PROVENANCE_AI_RECOVERY


def test_existing_flows_default_to_structural_provenance():
    flow = AffectedFlow(id="f1", entry_label="GET /x")
    impact = AcceptedImpact(id="f1", label="x")
    assert flow.provenance == "structural"
    assert impact.provenance == "structural"


def test_verdict_and_risk_are_never_touched_by_merge():
    result = _result(summary=ChangeSummary(verdict="VERIFICATION INCOMPLETE", risk="MEDIUM", counts=VerificationCounts()))
    merge_verified_recovery_into_result(result, _established_path(), TestRecoveryResult(status=STATUS_ESTABLISHED, tests=[_recovered_test()]))
    assert result.summary.verdict == "VERIFICATION INCOMPLETE"
    assert result.summary.risk == "MEDIUM"


# ---------------------------------------------------------------------------
# Superseding a stale inferred impact for the SAME changed behavior.
# ---------------------------------------------------------------------------


def _whole_change_inferred_impact(changed_symbol_name: str) -> AcceptedImpact:
    """Shapes exactly the real case this was found on: a whole-change-level
    semantic-guide inference whose OWN `changed_symbols` is a generic
    placeholder, not a real name -- so the only structured link back to
    the diff's own changed symbol is the documented `id` anchor
    convention (`impact:{repo}:{qualified_name or symbol}`)."""
    return AcceptedImpact(
        id=f"impact:app:{changed_symbol_name}", label="some inferred behavior",
        status="inferred", changed_symbols=["(whole change)"],
        verification_model_status="unsupported_or_partial", behavior_label="some inferred behavior",
    )


def test_inferred_impact_for_the_same_changed_symbol_is_dropped_when_recovery_establishes_it():
    changed = [ChangedSymbol(id="s1", repo=REPO, file="handler.ts", name="execute", qualified_name="Handler.execute")]
    stale = _whole_change_inferred_impact("execute")
    result = _result(changed_symbols=changed, accepted_impacts=[stale], summary=ChangeSummary(counts=VerificationCounts(impacts_inferred=1, impacts_not_modeled=1)))
    path = _established_path(nodes=[_entity("Controller.findUsers", "controller.ts"), _entity("Handler.execute", "handler.ts")])
    merge_verified_recovery_into_result(result, path, TestRecoveryResult())
    ids = [imp.id for imp in result.accepted_impacts]
    assert "impact:app:execute" not in ids
    assert result.summary.counts.impacts_inferred == 0
    assert result.summary.counts.impacts_not_modeled == 0


def test_inferred_impact_for_a_different_changed_symbol_is_retained():
    changed = [ChangedSymbol(id="s1", repo=REPO, file="handler.ts", name="execute", qualified_name="Handler.execute")]
    unrelated = _whole_change_inferred_impact("someOtherFunction")
    result = _result(changed_symbols=changed, accepted_impacts=[unrelated], summary=ChangeSummary(counts=VerificationCounts(impacts_inferred=1, impacts_not_modeled=1)))
    path = _established_path(nodes=[_entity("Controller.findUsers", "controller.ts"), _entity("Handler.execute", "handler.ts")])
    merge_verified_recovery_into_result(result, path, TestRecoveryResult())
    ids = [imp.id for imp in result.accepted_impacts]
    assert "impact:app:someOtherFunction" in ids
    assert result.summary.counts.impacts_inferred == 1


def test_proven_structural_impact_is_never_dropped_even_for_the_same_symbol():
    changed = [ChangedSymbol(id="s1", repo=REPO, file="handler.ts", name="execute", qualified_name="Handler.execute")]
    proven = AcceptedImpact(id="impact:app:execute", label="x", status="proven", changed_symbols=["execute"], verification_model_status="modeled")
    result = _result(changed_symbols=changed, accepted_impacts=[proven])
    path = _established_path(
        entrypoint="a different route", nodes=[_entity("Other.handler", "other.ts"), _entity("Handler.execute", "handler.ts")],
    )
    merge_verified_recovery_into_result(result, path, TestRecoveryResult())
    ids = [imp.id for imp in result.accepted_impacts]
    assert "impact:app:execute" in ids  # untouched -- status == "proven", never a dedupe target


def test_unresolved_recovery_never_drops_an_inferred_impact():
    changed = [ChangedSymbol(id="s1", repo=REPO, file="handler.ts", name="execute", qualified_name="Handler.execute")]
    stale = _whole_change_inferred_impact("execute")
    result = _result(changed_symbols=changed, accepted_impacts=[stale])
    merge_verified_recovery_into_result(result, PathRecoveryResult(status=STATUS_UNRESOLVED), TestRecoveryResult())
    assert result.accepted_impacts == [stale]


def test_matching_by_file_identity_when_symbol_name_differs():
    """A recovered target's bare/qualified symbol need not textually match
    the diff's own changed-symbol name for the match to hold -- the SAME
    file is itself a structured identity signal, not a text comparison."""
    changed = [ChangedSymbol(id="s1", repo=REPO, file="handler.ts", name="execute", qualified_name="Handler.execute")]
    stale = _whole_change_inferred_impact("execute")
    result = _result(changed_symbols=changed, accepted_impacts=[stale])
    # The recovered target names the QUALIFIED form ("Handler.execute"),
    # matched here via file identity, not a literal string match against
    # the changed symbol's bare "execute".
    path = _established_path(nodes=[_entity("Controller.findUsers", "controller.ts"), _entity("Handler.execute", "handler.ts")])
    merge_verified_recovery_into_result(result, path, TestRecoveryResult())
    assert result.accepted_impacts[0].id != "impact:app:execute"  # the stale one is gone; only the new one remains
    assert len(result.accepted_impacts) == 1
