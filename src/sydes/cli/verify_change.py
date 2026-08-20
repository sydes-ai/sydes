"""`sydes verify-change` CLI: system-level analysis of a backend code change."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.cli.output_paths import resolve_output_file_path, write_output_text
from sydes.core.models import RepoRef
from sydes.ingest.repos import parse_repo_specs
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact
from sydes.verify.analyzer import VerifyChangeOptions, analyze_change
from sydes.verify.git_change import GitChangeError


def verify_change_command(
    base: Annotated[str, typer.Option("--base", help="Base revision to diff against, e.g. main.")] = "main",
    repo: Annotated[
        list[str] | None,
        typer.Option(
            "--repo",
            help=(
                "Repository as name=path. Defaults to api=<cwd>. "
                "The first repo is the changed repo; extra repos are matched for cross-repo impact."
            ),
        ),
    ] = None,
    json_output: Annotated[
        Path | None,
        typer.Option("--json", help="Write the verification result artifact to this path."),
    ] = None,
    no_code_review: Annotated[
        bool,
        typer.Option("--no-code-review", help="Skip the LLM code-findings pass."),
    ] = False,
    llm_policy: Annotated[
        Literal["auto", "never"],
        typer.Option("--llm-policy", help="`never` runs deterministic analysis only."),
    ] = "auto",
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Model selection:\n"
                "  --model ollama:llama3.1:8b\n"
                "  --model openai:gpt-4.1-mini\n"
                "  --model anthropic:claude-3-5-sonnet-latest"
            ),
        ),
    ] = None,
    no_working_tree: Annotated[
        bool,
        typer.Option("--no-working-tree", help="Ignore uncommitted changes; diff committed work only."),
    ] = False,
    no_run_tests: Annotated[
        bool,
        typer.Option("--no-run-tests", help="Map existing tests but do not execute them."),
    ] = False,
    test_timeout: Annotated[
        float,
        typer.Option("--test-timeout", help="Per-test process timeout in seconds."),
    ] = 120.0,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
) -> None:
    """Analyze a change, run the tests that verify it, and report the evidence."""
    try:
        repos = parse_repo_specs(repo or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    if not repos:
        repos = [RepoRef(name="api", root=str(Path.cwd()))]

    options = VerifyChangeOptions(
        base=base,
        include_working_tree=not no_working_tree,
        code_review=not no_code_review,
        llm_policy=llm_policy,
        model_spec=model,
        run_tests=not no_run_tests,
        test_timeout_seconds=test_timeout,
    )

    try:
        result = analyze_change(repos=repos, options=options)
    except GitChangeError as exc:
        typer.echo(f"Git error: {exc}")
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    workspace_id = compute_workspace_id(repos)
    run_id = create_run_id()
    try:
        artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="change_verification",
            payload={
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "repo_inputs": [item.model_dump() for item in repos],
                "result": result.model_dump(),
            },
        )
        result.notes.append(f"Saved change verification artifact: {artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save change verification artifact: {exc}")

    typer.echo(render_verify_change_terminal(result, verbose=verbose))

    if json_output is not None:
        try:
            target = resolve_output_file_path(json_output, default_filename="change_verification.json")
            write_output_text(target, result.model_dump_json(indent=2))
        except (OSError, ValueError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
        typer.echo(f"Wrote verification result: {target}")
