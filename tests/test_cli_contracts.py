"""Contract tests for the current Sydes CLI surface."""

import json
from pathlib import Path

from typer.testing import CliRunner

from sydes.cli.main import app

runner = CliRunner()


def test_trace_terminal_output_contains_target_and_repos(tmp_path: Path) -> None:
    """Trace terminal mode should include target and selected repos."""
    gateway_dir = tmp_path / "gateway"
    api_dir = tmp_path / "api"
    gateway_dir.mkdir()
    api_dir.mkdir()
    (api_dir / "src").mkdir()
    (api_dir / "src" / "routes.py").write_text("router.post('/checkout', checkout)\n")

    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"gateway={gateway_dir}",
            "--repo",
            f"api={api_dir}",
        ],
    )

    assert result.exit_code == 0
    assert "Sydes Trace Target Resolution" in result.stdout
    assert "Target: POST /checkout" in result.stdout
    assert "gateway:" in result.stdout
    assert "api:" in result.stdout
    assert "Downstream flow tracing is planned for the next phase." in result.stdout


def test_trace_json_output_contains_expected_fields(tmp_path: Path) -> None:
    """Trace JSON mode should emit stable structured fields."""
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir()

    result = runner.invoke(
        app,
        [
            "trace",
            "/checkout",
            "--method",
            "POST",
            "--repo",
            f"gateway={gateway_dir}",
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
    assert "notes" in payload


def test_routes_terminal_output_runs_successfully(tmp_path: Path) -> None:
    """Routes command should run and report discovery status."""
    gateway_dir = tmp_path / "gateway"
    api_dir = tmp_path / "api"
    gateway_dir.mkdir()
    api_dir.mkdir()
    (api_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "routes",
            "--repo",
            f"gateway={gateway_dir}",
            "--repo",
            f"api={api_dir}",
        ],
    )

    assert result.exit_code == 0
    assert "Sydes Routes Discovery" in result.stdout
    assert "Routes discovered:" in result.stdout
    assert "Files examined:" in result.stdout
