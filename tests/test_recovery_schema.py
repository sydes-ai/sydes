"""`sydes.recovery.schema` — canonical `EntityRef` identity, discovery
parsing, and atomic-completion/decomposition parsing.

Path/test construction now happens programmatically in `agent.py`, not by
parsing one big "final recovery result" blob from the agent — so this
module no longer has (or needs) a whole-path JSON parser. What IS still
parsed from raw model output: Stage A's candidate proposal, Stage B's
atomic evidence-completion answer, and the recursive-decomposition answer.
"""

from __future__ import annotations

import json

import pytest

from sydes.recovery.schema import (
    MAX_BRIDGE_NODES,
    RecoveryError,
    parse_atomic_completion_result,
    parse_decomposition_result,
    parse_discovery_result,
)


# ---------------------------------------------------------------------------
# Stage A (discovery) candidate-path/test parsing.
# ---------------------------------------------------------------------------


def test_parses_a_well_formed_candidate_path_and_tests():
    payload = {
        "candidate_path": {
            "entrypoint": "GET /users", "target_node": "handler",
            "nodes": [{"symbol": "route", "file": "a.ts"}, {"symbol": "middle"}, {"symbol": "handler", "file": "b.ts"}],
        },
        "candidate_tests": [{"file": "a.spec.ts", "test": "t1", "covers": "limit behavior", "target": {"symbol": "handler", "file": "b.ts"}}],
    }
    path, tests = parse_discovery_result(json.dumps(payload))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["route", "middle", "handler"]
    assert path.nodes[0].file == "a.ts"
    assert path.nodes[1].file == ""  # unresolved at discovery time is valid
    assert path.target_node == "handler"
    assert len(tests) == 1
    assert tests[0].target.symbol == "handler"


def test_discovery_with_null_candidate_path_is_valid():
    payload = {"candidate_path": None, "candidate_tests": []}
    path, tests = parse_discovery_result(json.dumps(payload))
    assert path is None
    assert tests == []


def test_discovery_candidate_path_with_empty_nodes_degrades_to_none():
    payload = {"candidate_path": {"entrypoint": "x", "target_node": "a", "nodes": []}, "candidate_tests": []}
    path, _tests = parse_discovery_result(json.dumps(payload))
    assert path is None


def test_discovery_genuine_single_node_zero_hop_path_is_valid():
    """A real PR (a scheduled/cron job registered directly on the changed
    function, with no separate dispatcher symbol to name at all) produced
    exactly this shape -- a single node IS a legitimate answer when the
    entrypoint has nothing else to list, not automatically a lazy
    non-answer. Whether it can actually be ESTABLISHED is decided
    downstream, deterministically (see
    `sydes.recovery.agent._direct_entrypoint_edge`), never here."""
    payload = {
        "candidate_path": {
            "entrypoint": "scheduled job incident-report-weekly", "target_node": "incident_report_weekly",
            "nodes": [{"symbol": "incident_report_weekly", "file": "scheduled.py"}],
        },
        "candidate_tests": [],
    }
    path, _tests = parse_discovery_result(json.dumps(payload))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["incident_report_weekly"]
    assert path.target_node == "incident_report_weekly"


def test_discovery_target_node_equal_to_entrypoint_degrades_to_none():
    payload = {
        "candidate_path": {"entrypoint": "x", "target_node": "a", "nodes": [{"symbol": "a"}, {"symbol": "b"}]},
        "candidate_tests": [],
    }
    path, _tests = parse_discovery_result(json.dumps(payload))
    assert path is None


def test_discovery_all_nodes_collapsing_to_the_target_is_a_valid_single_node_path():
    """A real reliability-experiment run showed the model correctly
    determining that the entrypoint's own decorator sits directly on the
    changed symbol (no intermediate hop exists), expressed by naming it
    twice -- this must NOT be treated the same as the genuinely malformed
    'target_node == nodes[0], but nodes[0] != nodes[1]' case just above."""
    payload = {
        "candidate_path": {
            "entrypoint": "GET /user/{id}", "target_node": "UserController.getUser",
            "nodes": [
                {"symbol": "UserController.getUser", "file": "UserController.java"},
                {"symbol": "UserController.getUser", "file": "UserController.java"},
            ],
        },
        "candidate_tests": [],
    }
    path, _tests = parse_discovery_result(json.dumps(payload))
    assert path is not None
    assert [n.symbol for n in path.nodes] == ["UserController.getUser"]
    assert path.target_node == "UserController.getUser"


def test_discovery_target_node_not_among_nodes_degrades_to_none():
    payload = {
        "candidate_path": {"entrypoint": "x", "target_node": "z", "nodes": [{"symbol": "a"}, {"symbol": "b"}]},
        "candidate_tests": [],
    }
    path, _tests = parse_discovery_result(json.dumps(payload))
    assert path is None


def test_discovery_malformed_candidate_path_does_not_lose_valid_candidate_tests():
    payload = {
        "candidate_path": {"entrypoint": "x", "target_node": "a", "nodes": []},  # malformed: empty nodes
        "candidate_tests": [{"file": "a.ts", "test": "t1", "covers": "c1"}],
    }
    path, tests = parse_discovery_result(json.dumps(payload))
    assert path is None
    assert len(tests) == 1


def test_discovery_candidate_test_without_target_is_still_valid():
    payload = {"candidate_path": None, "candidate_tests": [{"file": "a.ts", "test": "t1", "covers": "c1"}]}
    _path, tests = parse_discovery_result(json.dumps(payload))
    assert tests[0].target is None


def test_non_json_discovery_output_raises_recovery_error():
    with pytest.raises(RecoveryError):
        parse_discovery_result("not json at all")


# ---------------------------------------------------------------------------
# Stage B atomic evidence-completion parsing.
# ---------------------------------------------------------------------------


def test_parses_atomic_completion_with_resolved_identity_and_evidence():
    payload = {
        "from_file": "a.ts", "to_file": "b.ts", "to_qualified_name": "pkg.B",
        "relationship": "constructs and dispatches",
        "evidence": [{"file": "b.ts", "line_start": 1, "line_end": 2, "fact": "x"}],
    }
    result = parse_atomic_completion_result(json.dumps(payload))
    assert result.relationship == "constructs and dispatches"
    assert result.from_file == "a.ts"
    assert result.to_file == "b.ts"
    assert result.to_qualified_name == "pkg.B"
    assert len(result.evidence) == 1


def test_parses_atomic_completion_with_nothing_found():
    payload = {"relationship": "", "evidence": [], "from_file": "", "to_file": ""}
    result = parse_atomic_completion_result(json.dumps(payload))
    assert result.relationship == ""
    assert result.evidence == []
    assert result.to_file == ""


def test_atomic_completion_non_json_raises_recovery_error():
    with pytest.raises(RecoveryError):
        parse_atomic_completion_result("not json")


# ---------------------------------------------------------------------------
# Recursive missing-link decomposition parsing.
# ---------------------------------------------------------------------------


def test_parses_a_decomposition_chain():
    payload = {"intermediates": [{"symbol": "b", "file": "b.ts"}, {"symbol": "c", "file": "c.ts"}]}
    intermediates = parse_decomposition_result(json.dumps(payload))
    assert [n.symbol for n in intermediates] == ["b", "c"]


def test_decomposition_with_no_plausible_chain_is_empty():
    intermediates = parse_decomposition_result(json.dumps({"intermediates": []}))
    assert intermediates == []


def test_decomposition_exceeding_max_bridge_nodes_is_rejected():
    too_many = [{"symbol": f"n{i}", "file": "a.ts"} for i in range(MAX_BRIDGE_NODES + 1)]
    intermediates = parse_decomposition_result(json.dumps({"intermediates": too_many}))
    assert intermediates == []


def test_decomposition_non_json_raises_recovery_error():
    with pytest.raises(RecoveryError):
        parse_decomposition_result("not json")
