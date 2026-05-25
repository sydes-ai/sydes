"""Deterministic coverage scoring for route discovery completeness."""

from __future__ import annotations


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def evaluate_discovery_coverage(
    *,
    route_index_summary: dict | None,
    route_graph_summary: dict | None,
    deterministic_route_count: int,
    deterministic_scan_truncated_files: int,
) -> dict:
    """Score deterministic route discovery coverage and return policy-friendly labels."""
    index_summary = route_index_summary or {}
    graph_summary = route_graph_summary or {}

    files_indexed = _safe_int(index_summary.get("files_indexed"))
    files_with_route_calls = _safe_int(index_summary.get("files_with_route_calls"))
    route_call_count = _safe_int(index_summary.get("route_call_count"))
    mount_call_count = _safe_int(index_summary.get("mount_call_count"))
    router_symbol_count = _safe_int(index_summary.get("router_symbol_count"))

    graph_containers = _safe_int(graph_summary.get("containers"))
    graph_declarations = _safe_int(graph_summary.get("declarations"))
    graph_mount_edges = _safe_int(graph_summary.get("mount_edges"))
    graph_composed_routes = _safe_int(graph_summary.get("composed_routes"))
    unresolved_mounts = _safe_int(graph_summary.get("unresolved_mounts"))

    deterministic_routes = max(0, _safe_int(deterministic_route_count))
    truncated_files = max(0, _safe_int(deterministic_scan_truncated_files))

    route_signal_volume = max(route_call_count, graph_declarations, files_with_route_calls)
    if route_signal_volume > 0:
        route_recovery_ratio = min(1.0, deterministic_routes / route_signal_volume)
    elif deterministic_routes > 0:
        route_recovery_ratio = 1.0
    else:
        route_recovery_ratio = 0.0

    if graph_declarations > 0:
        compose_ratio = min(1.0, graph_composed_routes / graph_declarations)
    elif deterministic_routes > 0:
        compose_ratio = 1.0
    else:
        compose_ratio = 0.0

    unresolved_ratio = (
        min(1.0, unresolved_mounts / max(1, graph_mount_edges + unresolved_mounts))
        if (graph_mount_edges or unresolved_mounts)
        else 0.0
    )

    score = (0.5 * route_recovery_ratio) + (0.3 * compose_ratio) + (0.2 * (1.0 - unresolved_ratio))

    if deterministic_routes > 0 and route_signal_volume == 0:
        score = max(score, 0.82)
    if deterministic_routes == 0 and route_signal_volume > 0:
        score = min(score, 0.35)
    if truncated_files > 0:
        score -= min(0.25, 0.08 * truncated_files)

    score = max(0.0, min(1.0, score))

    reasons: list[str] = []
    if deterministic_routes > 0:
        reasons.append("deterministic route extraction produced grounded routes")
    if graph_composed_routes > 0:
        reasons.append("route graph composition produced mounted routes")
    if unresolved_mounts > 0:
        reasons.append("some mount edges could not be resolved")
    if truncated_files > 0:
        reasons.append("deterministic scan encountered truncated files")
    if route_signal_volume > 0 and deterministic_routes <= max(3, route_signal_volume // 10):
        reasons.append("many route-like signals but relatively few final deterministic routes")

    if route_signal_volume == 0 and deterministic_routes == 0:
        label = "unknown"
        reasons.append("insufficient route index/graph signals")
    elif score >= 0.8:
        label = "strong"
    elif score >= 0.62:
        label = "moderate"
    else:
        label = "weak"

    return {
        "score": round(score, 2),
        "label": label,
        "signals": {
            "route_like_files": files_indexed,
            "files_with_route_calls": files_with_route_calls,
            "route_call_count": route_call_count,
            "router_symbol_count": router_symbol_count,
            "mount_call_count": mount_call_count,
            "route_graph_containers": graph_containers,
            "route_graph_declarations": graph_declarations,
            "route_graph_mount_edges": graph_mount_edges,
            "route_graph_composed_routes": graph_composed_routes,
            "final_deterministic_routes": deterministic_routes,
            "unresolved_mounts": unresolved_mounts,
            "deterministic_scan_truncated_files": truncated_files,
        },
        "reasons": reasons,
    }


def auto_policy_should_skip_llm(coverage: dict) -> bool:
    """Return True when llm-policy=auto should skip LLM discovery."""
    label = str(coverage.get("label") or "unknown")
    return label in {"strong", "moderate"}
