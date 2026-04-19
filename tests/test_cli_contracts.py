"""Contract tests for the current Sydes CLI surface."""

import json

from typer.testing import CliRunner

from sydes.cli.main import app

runner = CliRunner()


def test_trace_terminal_output_contains_target_and_repos() -> None:
    """Trace terminal mode should include target and selected repos."""
    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            "gateway=./gateway",
            "--repo",
            "api=./api",
        ],
    )

    assert result.exit_code == 0
    assert "Sydes Trace (V1 Placeholder)" in result.stdout
    assert "Target: POST /checkout" in result.stdout
    assert "gateway: ./gateway" in result.stdout
    assert "api: ./api" in result.stdout


def test_trace_json_output_contains_expected_fields() -> None:
    """Trace JSON mode should emit stable structured fields."""
    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            "gateway=./gateway",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["version"] == "v1"
    assert payload["target"]["path"] == "/checkout"
    assert payload["target"]["method"] == "POST"
    assert payload["repos"][0]["name"] == "gateway"


def test_routes_terminal_output_runs_successfully() -> None:
    """Routes command should run and report placeholder discovery state."""
    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            "gateway=./gateway",
            "--repo",
            "api=./api",
        ],
    )

    assert result.exit_code == 0
    assert "Sydes Routes (V1 Placeholder)" in result.stdout
    assert "Routes discovered:" in result.stdout
    assert "No routes discovered yet" in result.stdout
