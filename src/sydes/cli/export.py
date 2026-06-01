"""CLI command to export Sydes artifacts, including Postman collections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from sydes.core.models import TestMatrix
from sydes.export.json_export import export_stored_artifact
from sydes.export.postman import render_postman_collection_json


def _write_output(path: Path, content: str) -> None:
    """Write rendered export output to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def _resolve_output_path(output: Path, *, default_filename: str) -> Path:
    """Resolve output target path, allowing directories."""
    if output.exists() and output.is_dir():
        return output / default_filename
    return output


def _load_test_matrix(path: Path) -> TestMatrix:
    """Load and validate Sydes test matrix JSON from disk."""
    if not path.exists():
        raise typer.BadParameter(f"Test matrix file does not exist: {path}", param_hint="--test-matrix")
    if not path.is_file():
        raise typer.BadParameter(f"Test matrix path is not a file: {path}", param_hint="--test-matrix")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(f"Could not read test matrix file: {exc}", param_hint="--test-matrix") from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Test matrix file is not valid JSON: {exc}", param_hint="--test-matrix") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
        raise typer.BadParameter(
            "Test matrix file is not valid Sydes TestMatrix JSON: missing `groups` list",
            param_hint="--test-matrix",
        )

    try:
        return TestMatrix.model_validate(payload)
    except ValidationError as exc:
        raise typer.BadParameter(
            f"Test matrix file is not valid Sydes TestMatrix JSON: {exc.errors()[0]['msg']}",
            param_hint="--test-matrix",
        ) from exc


def _run_legacy_export(
    artifact_path: Path,
    *,
    output: Path | None,
    pretty: bool,
) -> None:
    if not artifact_path.exists():
        raise typer.BadParameter(
            f"Artifact file does not exist: {artifact_path}",
            param_hint="artifact-path",
        )
    if not artifact_path.is_file():
        raise typer.BadParameter(
            f"Artifact path is not a file: {artifact_path}",
            param_hint="artifact-path",
        )

    try:
        raw_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise typer.BadParameter(
            f"Could not read artifact file: {exc}",
            param_hint="artifact-path",
        ) from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"Artifact file is not valid JSON: {exc}",
            param_hint="artifact-path",
        ) from exc

    try:
        exported = export_stored_artifact(raw_payload)
    except ValueError as exc:
        raise typer.BadParameter(
            f"Artifact is not valid Sydes JSON: {exc}",
            param_hint="artifact-path",
        ) from exc

    rendered = json.dumps(
        exported,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    typer.echo(rendered)
    if output is not None:
        _write_output(output, rendered)


def export_command(
    artifact_path: Annotated[
        str,
        typer.Argument(help="Path to a saved Sydes artifact JSON file, or `postman` mode."),
    ],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    pretty: Annotated[bool, typer.Option("--pretty/--no-pretty")] = True,
    test_matrix: Annotated[
        Path | None,
        typer.Option("--test-matrix", help="Path to Sydes test_matrix.json artifact (for `export postman`)."),
    ] = None,
    method: Annotated[str | None, typer.Option("--method", help="Optional route method context for Postman export.")] = None,
    path: Annotated[str | None, typer.Option("--path", help="Optional route path context for Postman export.")] = None,
    collection_name: Annotated[
        str | None,
        typer.Option("--collection-name", help="Optional Postman collection display name."),
    ] = None,
) -> None:
    """Export artifacts as Sydes JSON, or export Postman collection via `export postman`."""
    if artifact_path.strip().lower() == "postman":
        if test_matrix is None:
            raise typer.BadParameter("Missing required option for postman mode: --test-matrix", param_hint="--test-matrix")
        matrix = _load_test_matrix(test_matrix)
        rendered = render_postman_collection_json(
            matrix,
            route_method=method,
            route_path=path,
            collection_name=collection_name,
        )
        if output is None:
            typer.echo(rendered)
            return
        out_path = _resolve_output_path(output, default_filename="sydes-postman-collection.json")
        _write_output(out_path, rendered)
        typer.echo(f"Postman collection written: {out_path}")
        return

    _run_legacy_export(Path(artifact_path), output=output, pretty=pretty)
