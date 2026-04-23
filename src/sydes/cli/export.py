"""CLI command to export saved Sydes artifacts as Sydes-native JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from sydes.export.json_export import export_stored_artifact


def _write_output(path: Path, content: str) -> None:
    """Write rendered export output to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def export_command(
    artifact_path: Annotated[
        Path,
        typer.Argument(help="Path to a saved Sydes artifact JSON file."),
    ],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    pretty: Annotated[bool, typer.Option("--pretty/--no-pretty")] = True,
) -> None:
    """Export an existing Sydes artifact into Sydes-native JSON."""
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

