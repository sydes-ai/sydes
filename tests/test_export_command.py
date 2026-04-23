"""Tests for Sydes artifact export CLI command."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sydes.cli.main import app

runner = CliRunner()


def _write_json(path: Path, payload: dict) -> None:
    """Write JSON fixture payload to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_export_command_exports_trace_result_envelope(tmp_path: Path) -> None:
    """Export command should emit Sydes-native JSON from saved trace envelope."""
    artifact = tmp_path / "trace_result.json"
    _write_json(
        artifact,
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "result": {
                "version": "v1",
                "target": {"kind": "api_route", "method": "POST", "path": "/users"},
                "repos": [{"name": "api", "root": "/tmp/api"}],
                "nodes": [],
                "edges": [],
                "flows": [],
                "tests": [],
                "unknowns": [],
                "notes": [],
                "summary": {"key_flow_id": None, "confidence": 0.4},
            },
        },
    )

    output_path = tmp_path / "exported.json"
    result = runner.invoke(
        app,
        ["export", str(artifact), "--output", str(output_path)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["target"]["path"] == "/users"
    assert payload["metadata"]["format"] == "sydes_trace_json"
    assert payload["metadata"]["source_artifact_kind"] == "trace_result_envelope"
    assert output_path.exists()


def test_export_command_exports_trace_graph_envelope(tmp_path: Path) -> None:
    """Export command should support saved graph artifact envelopes."""
    artifact = tmp_path / "trace_graph.json"
    _write_json(
        artifact,
        {
            "timestamp": "2026-04-23T10:00:00Z",
            "repo_inputs": [{"name": "api", "root": "/tmp/api"}],
            "target": {"kind": "api_route", "method": "GET", "path": "/books"},
            "key_flow_id": "flow:books",
            "graph": {
                "nodes": [
                    {"id": "n1", "type": "api_endpoint", "name": "/books"},
                    {"id": "n2", "type": "database", "name": "books_db", "metadata": {"action": "read"}},
                ],
                "edges": [{"id": "e1", "source": "n1", "target": "n2", "type": "READS_DB"}],
                "flows": [
                    {
                        "id": "flow:books",
                        "name": "GET /books",
                        "entry_node": "n1",
                        "steps": [{"node_id": "n1", "kind": "endpoint"}],
                    }
                ],
            },
        },
    )

    result = runner.invoke(app, ["export", str(artifact), "--no-pretty"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["summary"]["key_flow_id"] == "flow:books"
    assert payload["metadata"]["source_artifact_kind"] == "trace_graph_envelope"
    assert payload["sinks"]
    assert payload["sinks"][0]["kind"] == "database"


def test_export_command_fails_for_missing_file(tmp_path: Path) -> None:
    """Export command should return a clear error for missing artifact files."""
    missing = tmp_path / "does_not_exist.json"

    result = runner.invoke(app, ["export", str(missing)])

    assert result.exit_code != 0
    assert "Artifact file does not exist" in result.output


def test_export_command_fails_for_non_sydes_json(tmp_path: Path) -> None:
    """Export command should reject JSON that is not a Sydes artifact shape."""
    artifact = tmp_path / "random.json"
    _write_json(artifact, {"hello": "world"})

    result = runner.invoke(app, ["export", str(artifact)])

    assert result.exit_code != 0
    assert "Artifact is not valid Sydes JSON" in result.output
