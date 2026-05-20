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


def export_trace_result(trace_result: TraceResult) -> dict[str, Any]:
    """Export TraceResult into a stable Sydes-native JSON shape.

    The export payload is intentionally lightweight and forwards-compatible:
    top-level trace keys remain available, while metadata and derived sink
    records are included for OSS artifact consumers.
    """
    payload = {
        "version": trace_result.version,
        "target": trace_result.target.model_dump(exclude_none=True),
        "repos": [item.model_dump(exclude_none=True) for item in trace_result.repos],
        "nodes": [item.model_dump(exclude_none=True) for item in trace_result.nodes],
        "edges": [item.model_dump(exclude_none=True) for item in trace_result.edges],
        "flows": [item.model_dump(exclude_none=True) for item in trace_result.flows],
        "tests": [item.model_dump(exclude_none=True) for item in trace_result.tests],
        "unknowns": [item.model_dump(exclude_none=True) for item in trace_result.unknowns],
        "notes": list(trace_result.notes),
        "summary": trace_result.summary.model_dump(exclude_none=True),
    }
    if trace_result.test_matrix is not None:
        payload["test_matrix"] = trace_result.test_matrix.model_dump(exclude_none=True)
    payload["metadata"] = {
        "format": "sydes_trace_json",
        "export_version": "v1",
        "trace_version": trace_result.version,
        "exported_at": datetime.now(UTC).isoformat(),
    }
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
    artifact_metadata = payload.get("artifact_metadata")
    if isinstance(artifact_metadata, dict):
        exported_metadata["artifact"] = {
            "timestamp": artifact_metadata.get("timestamp"),
            "artifact_kind": artifact_metadata.get("artifact_kind"),
            "workspace_id": artifact_metadata.get("workspace_id"),
            "run_id": artifact_metadata.get("run_id"),
            "repo_inputs": artifact_metadata.get("repo_inputs"),
            "target_route": artifact_metadata.get("target_route"),
        }
    return exported
