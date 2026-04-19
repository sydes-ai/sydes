"""Routes CLI plumbing for V1 placeholder endpoint discovery."""

from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.core.models import RoutesResult
from sydes.discover.endpoints import discover_endpoints
from sydes.ingest.repos import parse_repo_specs
from sydes.report.json_report import render_routes_json
from sydes.report.terminal import render_routes_terminal


def _write_output(path: Path, content: str) -> None:
    """Write rendered command output to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def routes_command(
    repo: Annotated[list[str] | None, typer.Option("--repo")] = None,
    output_format: Annotated[
        Literal["terminal", "json"], typer.Option("--format")
    ] = "terminal",
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Discover routes for input repositories using V1 placeholder discovery."""
    try:
        repos = parse_repo_specs(repo or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    routes = discover_endpoints(repos)
    result = RoutesResult(repos=repos, routes=routes)
    rendered = (
        render_routes_json(result)
        if output_format == "json"
        else render_routes_terminal(result)
    )

    typer.echo(rendered)
    if output is not None:
        _write_output(output, rendered)
