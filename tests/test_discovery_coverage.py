"""Tests for discovery coverage scoring and auto-policy decisions."""

from sydes.discover.discovery_coverage import auto_policy_should_skip_llm, evaluate_discovery_coverage


def test_discovery_coverage_strong_when_composed_routes_cover_declarations() -> None:
    coverage = evaluate_discovery_coverage(
        route_index_summary={
            "files_indexed": 80,
            "files_with_route_calls": 50,
            "route_call_count": 250,
            "mount_call_count": 60,
            "router_symbol_count": 55,
        },
        route_graph_summary={
            "containers": 55,
            "declarations": 250,
            "mount_edges": 60,
            "composed_routes": 240,
            "unresolved_mounts": 2,
        },
        deterministic_route_count=240,
        deterministic_scan_truncated_files=0,
    )

    assert coverage["label"] == "strong"
    assert coverage["score"] >= 0.8
    assert auto_policy_should_skip_llm(coverage) is True


def test_discovery_coverage_weak_when_signals_high_but_routes_low() -> None:
    coverage = evaluate_discovery_coverage(
        route_index_summary={
            "files_indexed": 80,
            "files_with_route_calls": 50,
            "route_call_count": 250,
            "mount_call_count": 60,
            "router_symbol_count": 55,
        },
        route_graph_summary={
            "containers": 55,
            "declarations": 250,
            "mount_edges": 60,
            "composed_routes": 6,
            "unresolved_mounts": 30,
        },
        deterministic_route_count=6,
        deterministic_scan_truncated_files=0,
    )

    assert coverage["label"] == "weak"
    assert coverage["score"] < 0.62
    assert auto_policy_should_skip_llm(coverage) is False


def test_discovery_coverage_unknown_with_no_signals() -> None:
    coverage = evaluate_discovery_coverage(
        route_index_summary=None,
        route_graph_summary=None,
        deterministic_route_count=0,
        deterministic_scan_truncated_files=0,
    )

    assert coverage["label"] == "unknown"
    assert auto_policy_should_skip_llm(coverage) is False


def test_discovery_coverage_truncated_scan_not_strong() -> None:
    coverage = evaluate_discovery_coverage(
        route_index_summary={
            "files_indexed": 20,
            "files_with_route_calls": 20,
            "route_call_count": 40,
            "mount_call_count": 0,
            "router_symbol_count": 20,
        },
        route_graph_summary={
            "containers": 20,
            "declarations": 40,
            "mount_edges": 0,
            "composed_routes": 40,
            "unresolved_mounts": 0,
        },
        deterministic_route_count=40,
        deterministic_scan_truncated_files=3,
    )

    assert coverage["label"] in {"moderate", "weak"}
    assert coverage["score"] < 0.8
