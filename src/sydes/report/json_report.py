"""JSON helpers for machine-readable trace output."""

from sydes.core.models import TraceResult


def render_json(result: TraceResult) -> str:
    """Serialize a trace result to pretty-printed JSON."""
    return result.model_dump_json(indent=2)
