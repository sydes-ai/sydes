"""Tests for Sydes-native JSON trace export helpers."""

import json

from sydes.core.models import (
    GraphNode,
    TargetSpec,
    TestMatrix as SydesTestMatrix,
    TestMatrixGroup as SydesTestMatrixGroup,
    TraceResult,
    TraceSummary,
)
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
    assert "test_matrix" not in payload
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
    assert "test_matrix" not in payload
    assert "unknowns" in payload
    assert "notes" in payload
    assert "summary" in payload
    assert "metadata" in payload
    assert "sinks" not in payload


def test_export_trace_result_includes_test_matrix_when_present() -> None:
    """Exporter should include grouped test matrix when trace result has one."""
    result = _trace_with_sink_node()
    result.test_matrix = SydesTestMatrix(groups=[SydesTestMatrixGroup(category="happy_path", tests=[])])

    payload = export_trace_result(result)

    assert "test_matrix" in payload
    assert payload["test_matrix"]["groups"][0]["category"] == "happy_path"


def test_export_trace_result_includes_layered_fields_back_compat() -> None:
    result = _trace_with_sink_node()
    result.flow = {"steps": [{"id": "step:1", "kind": "endpoint", "name": "endpoint", "repo": "api", "evidence": [], "confidence": 1.0, "status": "grounded"}]}
    result.layers = [{"depth": 0, "kind": "endpoint", "name": "POST /users", "steps": []}]
    result.sinks = [{"kind": "database", "operation": "write", "name": "users"}]
    result.diagnostics = ["trace_truncated=false"]
    result.artifacts = {"trace_result": "/tmp/trace_result.json"}

    payload = export_trace_result(result)

    assert "nodes" in payload and "edges" in payload and "flows" in payload
    assert "flow" in payload
    assert "layers" in payload
    assert "sinks" in payload
    assert "diagnostics" in payload
    assert "artifacts" in payload
