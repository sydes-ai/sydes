"""Routes CLI plumbing for V1 placeholder endpoint discovery."""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.cli.output_paths import resolve_output_file_path, write_output_text
from sydes.discover.endpoints import discover_endpoints
from sydes.discover.route_graph import build_route_graph_facts_batch
from sydes.discover.route_index import build_route_index_batch
from sydes.discover.repo_map import build_repo_map_batch
from sydes.ingest.repos import parse_repo_specs
from sydes.llm.client import LLMClientError, validate_llm_available
from sydes.report.json_report import render_routes_json
from sydes.report.terminal import render_routes_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact


def _write_command_output(
    output: Path,
    content: str,
    *,
    output_format: Literal["terminal", "json"],
) -> None:
    """Resolve and write command output without leaking low-level path errors."""
    default_name = "routes.json" if output_format == "json" else "routes.txt"
    resolved = resolve_output_file_path(output, default_filename=default_name)
    write_output_text(resolved, content)


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
    llm_policy: Annotated[
        Literal["auto", "always", "never"],
        typer.Option("--llm-policy"),
    ] = "auto",
    model_timeout: Annotated[
        float | None,
        typer.Option("--model-timeout"),
    ] = None,
    allow_partial: Annotated[bool, typer.Option("--allow-partial")] = False,
) -> None:
    """Discover routes for input repositories using shallow+LLM pipeline."""
    try:
        repos = parse_repo_specs(repo or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    validation = None
    if llm_policy == "always":
        validation = validate_llm_available(model_spec=model)
        if not validation.ok:
            message = validation.reason or "LLM preflight failed."
            if output_format == "json":
                payload = {
                    "ok": False,
                    "error": {
                        "provider": validation.provider,
                        "model": validation.model,
                        "base_url": validation.base_url,
                        "message": message,
                        "available_models": list(validation.available_models),
                    },
                }
                rendered = json.dumps(payload, indent=2)
                typer.echo(rendered)
                if output is not None:
                    try:
                        _write_command_output(output, rendered, output_format=output_format)
                    except (OSError, ValueError) as exc:
                        typer.echo(str(exc))
                        raise typer.Exit(code=1) from exc
                raise typer.Exit(code=1)
            typer.echo(f"LLM validation failed: {message}")
            raise typer.Exit(code=1)

    try:
        discover_kwargs: dict[str, object] = {
            "model_spec": model,
            "strict_llm": not allow_partial,
        }
        if llm_policy != "auto":
            discover_kwargs["llm_policy"] = llm_policy
        if model_timeout is not None:
            discover_kwargs["model_timeout_seconds"] = model_timeout
        result = discover_endpoints(repos, **discover_kwargs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc
    except LLMClientError as exc:
        message = str(exc)
        if output_format == "json":
            payload = {
                "ok": False,
                "error": {
                    "provider": validation.provider if validation is not None else None,
                    "model": validation.model if validation is not None else None,
                    "base_url": validation.base_url if validation is not None else None,
                    "message": message,
                    "available_models": list(validation.available_models) if validation is not None else [],
                },
            }
            rendered = json.dumps(payload, indent=2)
            typer.echo(rendered)
            if output is not None:
                try:
                    _write_command_output(output, rendered, output_format=output_format)
                except (OSError, ValueError) as exc:
                    typer.echo(str(exc))
                    raise typer.Exit(code=1) from exc
            raise typer.Exit(code=1)
        typer.echo(f"LLM discovery failed: {message}")
        raise typer.Exit(code=1)

    artifact_payload = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "repo_inputs": [item.model_dump() for item in repos],
        "result": result.model_dump(),
    }
    workspace_id = compute_workspace_id(repos)
    run_id = create_run_id()

    try:
        artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="routes_discovery",
            payload=artifact_payload,
        )
        result.notes.append(f"Saved discovery artifact: {artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save discovery artifact: {exc}")

    repo_map_batch: dict | None = None
    try:
        repo_map_batch = build_repo_map_batch(repos)
        repo_map_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "map": repo_map_batch,
        }
        repo_map_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="repo_map",
            payload=repo_map_payload,
        )
        result.notes.append(f"Saved repo map artifact: {repo_map_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save repo map artifact: {exc}")

    route_index_batch: dict | None = None
    try:
        route_index_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "index": build_route_index_batch(repos, repo_map_batch=repo_map_batch),
        }
        route_index_batch = route_index_payload["index"]
        route_index_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="route_index",
            payload=route_index_payload,
        )
        result.notes.append(f"Saved route index artifact: {route_index_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save route index artifact: {exc}")

    try:
        route_graph_facts = build_route_graph_facts_batch(repos, route_index_batch=route_index_batch)
        route_graph_facts.pop("_repo_endpoint_candidates", None)
        route_graph_payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "repo_inputs": [item.model_dump() for item in repos],
            "graph_facts": route_graph_facts,
        }
        route_graph_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="route_graph_facts",
            payload=route_graph_payload,
        )
        result.notes.append(f"Saved route graph facts artifact: {route_graph_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save route graph facts artifact: {exc}")

    rendered = (
        render_routes_json(result)
        if output_format == "json"
        else render_routes_terminal(result)
    )

    typer.echo(rendered)
    if output is not None:
        try:
            _write_command_output(output, rendered, output_format=output_format)
        except (OSError, ValueError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
