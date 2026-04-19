"""Trace command plumbing for V1 placeholder execution."""

from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.core.models import TargetSpec, TraceResult, TraceSummary
from sydes.ingest.repos import parse_repo_specs
from sydes.report.json_report import render_json
from sydes.report.terminal import render_terminal


def _build_placeholder_result(
    path: str,
    method: str | None,
    repo_specs: list[str],
) -> TraceResult:
    """Create a minimal V1 placeholder trace result."""
    try:
        repos = parse_repo_specs(repo_specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    target = TargetSpec(path=path, method=method)
    return TraceResult(target=target, repos=repos, summary=TraceSummary())


def _write_output(path: Path, content: str) -> None:
    """Write rendered command output to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def trace_command(
    path: Annotated[str, typer.Argument(help="Target API path, e.g. /checkout")],
    method: Annotated[str | None, typer.Option("--method")] = None,
    repo: Annotated[list[str] | None, typer.Option("--repo")] = None,
    output_format: Annotated[
        Literal["terminal", "json"], typer.Option("--format")
    ] = "terminal",
    output: Annotated[Path | None, typer.Option("--output")] = None,
    emit_tests: Annotated[bool, typer.Option("--emit-tests")] = False,
    max_hops: Annotated[int | None, typer.Option("--max-hops")] = None,
    max_files: Annotated[int | None, typer.Option("--max-files")] = None,
) -> None:
    """Run the V1 placeholder trace pipeline with parsed CLI inputs."""
    _ = emit_tests, max_hops, max_files
    result = _build_placeholder_result(
        path=path,
        method=method,
        repo_specs=repo or [],
    )
    rendered = render_json(result) if output_format == "json" else render_terminal(result)

    typer.echo(rendered)
    if output is not None:
        _write_output(output, rendered)
