"""Sydes-native JSON export helpers for graph-backed trace artifacts.

This module defines the public OSS export format for Sydes traces for now.
It intentionally stays Sydes-native (JSON dict structure) instead of committing
to GraphML or third-party graph interchange schemas at this stage.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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

