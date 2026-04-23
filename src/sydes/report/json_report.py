"""JSON helpers for machine-readable Sydes command output."""

import json

from sydes.core.models import RoutesResult, TraceResult
from sydes.export.json_export import export_trace_result


def render_json(result: TraceResult) -> str:
    """Serialize a trace result to pretty-printed JSON."""
    payload = export_trace_result(result)
    return json.dumps(payload, indent=2)


def render_routes_json(result: RoutesResult) -> str:
    """Serialize routes discovery output to pretty-printed JSON."""
    return result.model_dump_json(indent=2)
