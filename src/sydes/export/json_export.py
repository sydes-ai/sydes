"""Sydes-native JSON export helpers for graph-backed trace artifacts.

This module defines the public OSS export format for Sydes traces for now.
It intentionally stays Sydes-native (JSON dict structure) instead of committing
to GraphML or third-party graph interchange schemas at this stage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from sydes.core.models import TraceResult

_SINK_NODE_TYPES = {"database", "external_api", "queue", "file_sink", "sink"}


def _derive_sinks(trace_result: TraceResult) -> list[dict[str, Any]]:
    """Derive sink records from sink-like graph nodes when present."""
    sinks: list[dict[str, Any]] = []
    for node in trace_result.nodes:
        if node.type not in _SINK_NODE_TYPES:
            continue
        action = None
        if isinstance(node.metadata, dict):
            action = node.metadata.get("action")
        sinks.append(
            {
                "id": node.id,
                "kind": node.type,
                "name": node.name,
                "repo": node.repo,
                "service": node.service,
                "file": node.file,
                "symbol": node.symbol,
                "action": action,
                "confidence": node.confidence,
                "status": node.status,
                "evidence": [item.model_dump() for item in node.evidence],
            }
        )
    return sinks


def export_trace_result(trace_result: TraceResult) -> dict[str, Any]:
    """Export TraceResult into a stable Sydes-native JSON shape.

    The export payload is intentionally lightweight and forwards-compatible:
    top-level trace keys remain available, while metadata and derived sink
    records are included for OSS artifact consumers.
    """
    payload = trace_result.model_dump()
    payload["metadata"] = {
        "format": "sydes_trace_json",
        "export_version": "v1",
        "trace_version": trace_result.version,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    payload["sinks"] = _derive_sinks(trace_result)
    return payload


def _is_trace_result_like(payload: dict[str, Any]) -> bool:
    """Return True when payload appears to be a direct TraceResult object."""
    required = {"target", "nodes", "edges", "flows", "summary"}
    return required.issubset(payload.keys())


def _trace_payload_from_graph_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal trace-like payload from a stored graph artifact envelope."""
    graph = payload.get("graph")
    if not isinstance(graph, dict):
        raise ValueError("Graph artifact is missing a valid 'graph' object.")
    target = payload.get("target")
    if not isinstance(target, dict):
        raise ValueError("Graph artifact is missing a valid 'target' object.")

    return {
        "version": payload.get("version", "v1"),
        "target": target,
        "repos": payload.get("repo_inputs", []),
        "nodes": graph.get("nodes", []),
        "edges": graph.get("edges", []),
        "flows": graph.get("flows", []),
        "tests": payload.get("tests", []),
        "unknowns": payload.get("unknowns", []),
        "notes": payload.get("notes", []),
        "summary": {
            "key_flow_id": payload.get("key_flow_id"),
            "confidence": payload.get("confidence"),
        },
    }


def export_stored_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    """Export a stored Sydes artifact payload into Sydes-native trace JSON.

    Supported inputs:
    - direct TraceResult JSON payload
    - saved trace artifact envelope containing `result`
    - saved graph artifact envelope containing `graph`
    """
    if not isinstance(payload, dict):
        raise ValueError("Artifact payload must be a JSON object.")

    source_kind = "trace_result"
    if "result" in payload:
        result_payload = payload.get("result")
        if not isinstance(result_payload, dict):
            raise ValueError("Artifact 'result' field must be a JSON object.")
        trace_payload = result_payload
        source_kind = "trace_result_envelope"
    elif "graph" in payload:
        trace_payload = _trace_payload_from_graph_artifact(payload)
        source_kind = "trace_graph_envelope"
    elif _is_trace_result_like(payload):
        trace_payload = payload
        source_kind = "trace_result"
    else:
        raise ValueError(
            "Unsupported artifact shape. Expected TraceResult JSON or stored trace/graph artifact payload."
        )

    try:
        trace_result = TraceResult.model_validate(trace_payload)
    except ValidationError as exc:
        raise ValueError(f"Artifact is not valid Sydes trace JSON: {exc.errors()[0]['msg']}") from exc

    exported = export_trace_result(trace_result)
    exported_metadata = exported.setdefault("metadata", {})
    exported_metadata["source_artifact_kind"] = source_kind
    if isinstance(payload.get("timestamp"), str):
        exported_metadata["source_timestamp"] = payload["timestamp"]
    return exported
