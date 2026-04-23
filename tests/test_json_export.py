"""Tests for Sydes-native JSON trace export helpers."""

import json

from sydes.core.models import GraphNode, TargetSpec, TraceResult, TraceSummary
from sydes.export.json_export import export_trace_result
from sydes.report.json_report import render_json


def _trace_with_sink_node() -> TraceResult:
    """Build a minimal trace fixture with one sink-like graph node."""
    return TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[
            GraphNode(
                id="endpoint:1",
                type="api_endpoint",
                name="/users",
                repo="api",
                file="src/routes.py",
                method="POST",
                path="/users",
            ),
            GraphNode(
                id="sink:1",
                type="database",
                name="users_db",
                repo="api",
                file="src/repo.py",
                metadata={"action": "write"},
            ),
        ],
        summary=TraceSummary(confidence=0.8),
    )


def test_export_trace_result_includes_metadata_and_clean_top_level_shape() -> None:
    """Exporter should include intended top-level fields and metadata."""
    result = _trace_with_sink_node()

    payload = export_trace_result(result)

    assert payload["version"] == "v1"
    assert payload["target"]["path"] == "/users"
    assert "metadata" in payload
    assert payload["metadata"]["format"] == "sydes_trace_json"
    assert payload["metadata"]["export_version"] == "v1"
    assert "nodes" in payload
    assert "edges" in payload
    assert "flows" in payload
    assert "tests" in payload
    assert "notes" in payload
    assert "summary" in payload
    assert "sinks" not in payload


def test_render_json_keeps_trace_keys_and_uses_exporter_shape() -> None:
    """JSON renderer should preserve intended keys and omit helper-only fields."""
    result = _trace_with_sink_node()

    rendered = render_json(result)
    payload = json.loads(rendered)

    assert payload["target"]["method"] == "POST"
    assert "nodes" in payload
    assert "edges" in payload
    assert "flows" in payload
    assert "tests" in payload
    assert "unknowns" in payload
    assert "notes" in payload
    assert "summary" in payload
    assert "metadata" in payload
    assert "sinks" not in payload
