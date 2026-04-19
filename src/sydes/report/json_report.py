"""JSON helpers for machine-readable Sydes command output."""

from sydes.core.models import RoutesResult, TraceResult


def render_json(result: TraceResult) -> str:
    """Serialize a trace result to pretty-printed JSON."""
    return result.model_dump_json(indent=2)


def render_routes_json(result: RoutesResult) -> str:
    """Serialize routes discovery output to pretty-printed JSON."""
    return result.model_dump_json(indent=2)
