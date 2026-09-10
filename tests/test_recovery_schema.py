"""`sydes.recovery.schema` — strict parsing of the edge-level path shape.

Parsing itself never assigns `established`/`accepted` status to anything —
that is entirely `sydes.recovery.verify`'s job (see its own tests). These
tests pin the structural contract: nodes/edges must form one contiguous
chain from `nodes[0]`, `target_node` must name a real node and differ from
the entrypoint node, and every edge/test/evidence entry must have its
required fields.

A malformed individual entry is DROPPED (recorded in `unresolved`), not
fatal to the whole response — partial recovery beats an all-or-nothing
failure (see `test_one_malformed_path_does_not_discard_a_valid_test_or_
other_paths`, which pins the exact real failure this fixed: one bad path
entry used to discard an otherwise-valid, legitimately recovered test).
Only a response that is not JSON at all, or whose top-level field is not
even an array, still raises `RecoveryError` outright.
"""

from __future__ import annotations

import json

import pytest

from sydes.recovery.schema import RecoveryError, STATUS_UNRESOLVED, parse_recovery_result


def _payload(**overrides) -> dict:
    base = {
        "recovered_paths": [],
        "recovered_tests": [],
        "corrected_first_pass_claims": [],
        "unresolved": [],
    }
    base.update(overrides)
    return base


def _two_node_path(**overrides) -> dict:
    base = {
        "entrypoint": "GET /users",
        "target_node": "handler",
        "nodes": [{"symbol": "route", "file": "a.ts"}, {"symbol": "handler", "file": "a.ts"}],
        "edges": [
            {
                "from": "route",
                "to": "handler",
                "relationship": "registers",
                "evidence": [{"file": "a.ts", "line_start": 1, "line_end": 2, "fact": "route registers handler"}],
            }
        ],
    }
    base.update(overrides)
    return base


def _dropped_path_reasons(result) -> list[str]:
    return [u.missing_evidence for u in result.unresolved if "recovered_paths" in u.question]


def test_parses_a_well_formed_two_node_path():
    payload = _payload(recovered_paths=[_two_node_path()])
    result = parse_recovery_result(json.dumps(payload))
    assert len(result.recovered_paths) == 1
    path = result.recovered_paths[0]
    assert path.target_node == "handler"
    assert len(path.nodes) == 2
    assert len(path.edges) == 1
    # Parsing never assigns status -- everything starts unresolved until verified.
    assert path.status == STATUS_UNRESOLVED
    assert path.edges[0].status == STATUS_UNRESOLVED


def test_result_status_is_unresolved_until_verified():
    payload = _payload(recovered_paths=[_two_node_path()])
    result = parse_recovery_result(json.dumps(payload))
    assert result.status == STATUS_UNRESOLVED


def test_missing_target_node_is_dropped_not_fatal():
    path = _two_node_path()
    del path["target_node"]
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_target_node_not_among_nodes_is_dropped_not_fatal():
    path = _two_node_path(target_node="does_not_exist")
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_edge_from_not_matching_a_node_is_dropped_not_fatal():
    path = _two_node_path()
    path["edges"][0]["from"] = "not_a_node"
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_non_contiguous_edge_chain_is_dropped_not_fatal():
    # three nodes but the edge list skips the middle one
    path = {
        "entrypoint": "GET /users",
        "target_node": "c",
        "nodes": [{"symbol": "a", "file": "x.ts"}, {"symbol": "b", "file": "x.ts"}, {"symbol": "c", "file": "x.ts"}],
        "edges": [{"from": "a", "to": "c", "relationship": "calls", "evidence": []}],
    }
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_node_count_edge_count_mismatch_is_dropped_not_fatal():
    path = _two_node_path()
    path["nodes"].append({"symbol": "extra", "file": "a.ts"})  # 3 nodes, still 1 edge
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_evidence_entry_missing_file_is_dropped_not_fatal():
    path = _two_node_path()
    path["edges"][0]["evidence"] = [{"fact": "some claim with no file"}]
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_evidence_entry_missing_fact_is_dropped_not_fatal():
    path = _two_node_path()
    path["edges"][0]["evidence"] = [{"file": "a.ts", "line_start": 1, "line_end": 2}]
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_recovered_test_missing_required_field_is_dropped_not_fatal():
    payload = _payload(recovered_tests=[{"file": "a.spec.ts", "test": "rejects too-large limit"}])
    result = parse_recovery_result(json.dumps(payload))
    assert result.recovered_tests == []
    assert any("recovered_tests" in u.question for u in result.unresolved)


def test_non_json_output_raises_recovery_error():
    with pytest.raises(RecoveryError):
        parse_recovery_result("this is not JSON at all")


def test_top_level_field_not_a_list_raises_recovery_error():
    payload = _payload(recovered_paths="not a list")
    with pytest.raises(RecoveryError):
        parse_recovery_result(json.dumps(payload))


def test_response_wrapped_in_markdown_fence_still_parses():
    payload = _payload()
    text = f"```json\n{json.dumps(payload)}\n```"
    result = parse_recovery_result(text)
    assert result.status == STATUS_UNRESOLVED


def test_single_node_path_is_dropped_not_a_real_path():
    """A one-node "path" (the changed symbol declared as its own
    entrypoint) proves nothing about reachability and must never survive
    parsing -- this is exactly the shape an early agent run used to
    sidestep the harder dispatch-evidence requirement instead of honestly
    reporting `unresolved`."""
    path = {"entrypoint": "GET /users", "target_node": "handler", "nodes": [{"symbol": "handler", "file": "a.ts"}], "edges": []}
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_target_node_equal_to_entrypoint_node_is_dropped():
    path = _two_node_path(target_node="route")  # nodes[0] is "route"
    result = parse_recovery_result(json.dumps(_payload(recovered_paths=[path])))
    assert result.recovered_paths == []
    assert _dropped_path_reasons(result)


def test_one_malformed_path_does_not_discard_a_valid_test_or_other_paths():
    """Partial recovery beats all-or-nothing: this pins the exact real
    failure that motivated the fix -- one bad path entry used to discard
    an otherwise-valid, legitimately recovered test (observed on a real
    RS-M-01 evaluation run where the trigger was test-mapping-only and the
    agent nonetheless emitted one malformed, degenerate path)."""
    good_path = _two_node_path()
    bad_path = {"entrypoint": "GET /x", "target_node": "only_node", "nodes": [{"symbol": "only_node", "file": "a.ts"}], "edges": []}
    payload = _payload(
        recovered_paths=[good_path, bad_path],
        recovered_tests=[{"file": "a.spec.ts", "test": "t1", "covers": "c1", "evidence": []}],
    )
    result = parse_recovery_result(json.dumps(payload))
    assert len(result.recovered_paths) == 1
    assert result.recovered_paths[0].entrypoint == "GET /users"
    assert len(result.recovered_tests) == 1
    assert any("recovered_paths[1]" in u.question for u in result.unresolved)
