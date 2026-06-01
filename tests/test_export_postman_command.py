"""Tests for `sydes export postman` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from sydes.cli.main import app

runner = CliRunner()


def test_export_postman_stdout_from_fixture() -> None:
    result = runner.invoke(
        app,
        [
            "export",
            "postman",
            "--test-matrix",
            "fixtures/artifacts/test_matrix_v2.json",
            "--method",
            "POST",
            "--path",
            "/items",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["info"]["schema"] == "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def test_export_postman_file_output_writes_json(tmp_path: Path) -> None:
    output_file = tmp_path / "postman_collection.json"
    result = runner.invoke(
        app,
        [
            "export",
            "postman",
            "--test-matrix",
            "fixtures/artifacts/test_matrix_v2.json",
            "--method",
            "POST",
            "--path",
            "/items",
            "--output",
            str(output_file),
        ],
    )

    assert result.exit_code == 0
    assert output_file.exists()
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    assert payload["info"]["schema"] == "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def test_export_postman_output_directory_writes_default_file(tmp_path: Path) -> None:
    output_dir = tmp_path / "exports"
    output_dir.mkdir()
    result = runner.invoke(
        app,
        [
            "export",
            "postman",
            "--test-matrix",
            "fixtures/artifacts/test_matrix_v2.json",
            "--output",
            str(output_dir),
        ],
    )

    assert result.exit_code == 0
    default_file = output_dir / "sydes-postman-collection.json"
    assert default_file.exists()


def test_export_postman_missing_test_matrix_fails(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    result = runner.invoke(
        app,
        ["export", "postman", "--test-matrix", str(missing)],
    )

    assert result.exit_code != 0
    assert "Test matrix file does not exist" in result.output


def test_export_postman_invalid_json_fails(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not-json", encoding="utf-8")

    result = runner.invoke(
        app,
        ["export", "postman", "--test-matrix", str(bad)],
    )

    assert result.exit_code != 0
    assert "Test matrix file is not valid JSON" in result.output


def test_export_postman_invalid_shape_fails(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    result = runner.invoke(
        app,
        ["export", "postman", "--test-matrix", str(invalid)],
    )

    assert result.exit_code != 0
    assert "test-matrix" in result.output.lower()
