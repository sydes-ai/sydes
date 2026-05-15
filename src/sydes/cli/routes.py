"""Routes CLI plumbing for V1 placeholder endpoint discovery."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.discover.endpoints import discover_endpoints
from sydes.ingest.repos import parse_repo_specs
from sydes.report.json_report import render_routes_json
from sydes.report.terminal import render_routes_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact


def _write_output(path: Path, content: str) -> None:
    """Write rendered command output to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")


def routes_command(
    repo: Annotated[list[str] | None, typer.Option("--repo")] = None,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Model selection:\n"
                "  --model ollama:llama3.1:8b\n"
                "  --model openai:gpt-4.1-mini\n"
                "  --model anthropic:claude-3-5-sonnet-latest\n\n"
                "Environment defaults:\n"
                "  SYDES_LLM_PROVIDER=openai\n"
                "  SYDES_LLM_MODEL=gpt-4.1-mini\n"
                "  OPENAI_API_KEY=...\n\n"
                "  SYDES_LLM_PROVIDER=anthropic\n"
                "  SYDES_LLM_MODEL=claude-3-5-sonnet-latest\n"
                "  ANTHROPIC_API_KEY=..."
            ),
        ),
    ] = None,
    output_format: Annotated[
        Literal["terminal", "json"], typer.Option("--format")
    ] = "terminal",
    output: Annotated[Path | None, typer.Option("--output")] = None,
) -> None:
    """Discover routes for input repositories using shallow+LLM pipeline."""
    try:
        repos = parse_repo_specs(repo or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    try:
        result = discover_endpoints(repos, model_spec=model)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    artifact_payload = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "repo_inputs": [item.model_dump() for item in repos],
        "result": result.model_dump(),
    }
    try:
        workspace_id = compute_workspace_id(repos)
        run_id = create_run_id()
        artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="routes_discovery",
            payload=artifact_payload,
        )
        result.notes.append(f"Saved discovery artifact: {artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save discovery artifact: {exc}")

    rendered = (
        render_routes_json(result)
        if output_format == "json"
        else render_routes_terminal(result)
    )

    typer.echo(rendered)
    if output is not None:
        _write_output(output, rendered)
