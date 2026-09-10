"""`sydes verify-change` CLI: system-level analysis of a backend code change."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.cli.output_paths import resolve_output_file_path, write_output_text
from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.core.models import RepoRef
from sydes.ingest.repos import parse_repo_specs
from sydes.llm.client import LLMClientError, create_default_llm_client
from sydes.recovery.agent import recover
from sydes.recovery.context import build_context
from sydes.recovery.merge import build_recovery_view, summarize_for_notes
from sydes.recovery.schema import RecoveryError
from sydes.recovery.trigger import evaluate_trigger
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact
from sydes.verify.analyzer import VerifyChangeOptions, analyze_change
from sydes.verify.git_change import GitChangeError
from sydes.verify.models import ChangeVerificationResult


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
    code_review: Annotated[
        bool,
        typer.Option(
            "--code-review",
            help="Run the advisory LLM code-findings pass (off by default; never affects the verdict).",
        ),
    ] = False,
    llm_policy: Annotated[
        Literal["auto", "never"],
        typer.Option(
            "--llm-policy",
            help=(
                "Controls optional LLM use in general route/flow discovery and "
                "expansion ONLY — independent of --impact-guide, which separately "
                "controls the semantic impact-inference guide. `never` here does "
                "NOT disable --impact-guide; `--llm-policy never --impact-guide auto` "
                "is a valid, meaningful combination: deterministic structural/flow "
                "analysis plus the AI impact guide only. `auto` (default): LLM-assisted "
                "route/flow discovery may run. `never`: that pass is skipped."
            ),
        ),
    ] = "auto",
    impact_guide: Annotated[
        Literal["off", "auto", "always"],
        typer.Option(
            "--impact-guide",
            help=(
                "Semantic impact-inference guide for unresolved impact (cbm backend "
                "only) — independent of --llm-policy (see its help). "
                "`off` (default): deterministic impact analysis only. "
                "`auto`: consult the guide only on unresolved structural triggers. "
                "`always`: consult it whenever any symbol is unresolved (dev/debug)."
            ),
        ),
    ] = "off",
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
    ai_recovery: Annotated[
        bool,
        typer.Option(
            "--ai-recovery",
            help=(
                "EXPERIMENTAL, off by default: when the first pass leaves a "
                "high-value gap (no established path, a boundary with no "
                "complete flow, or a missing test mapping despite a changed "
                "test file), run a second-stage LLM recovery pass that may "
                "search the whole repository. Never alters the structural "
                "result, verdict, or CBM graph; writes its own JSON "
                "artifact and appends one summary line to `notes`. See "
                "`sydes.recovery` for the full design."
            ),
        ),
    ] = False,
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
        code_review=code_review,
        llm_policy=llm_policy,
        model_spec=model,
        run_tests=not no_run_tests,
        test_timeout_seconds=test_timeout,
        impact_guide=impact_guide,
    )

    try:
        result = analyze_change(repos=repos, options=options)
    except GitChangeError as exc:
        typer.echo(f"Git error: {exc}")
        raise typer.Exit(code=1) from exc
    except CodeIntelligenceError as exc:
        # `cbm_client.py` already frames an initialization failure clearly;
        # this only stops it from surfacing as a raw traceback, and it never
        # falls back to the native backend the operator did not choose.
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    if ai_recovery:
        _run_ai_recovery(result, repo_root=Path(repos[0].root), model_spec=model, json_output=json_output)

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


def _run_ai_recovery(
    result: ChangeVerificationResult, *, repo_root: Path, model_spec: str | None, json_output: Path | None,
) -> None:
    """The entire `--ai-recovery` integration surface: evaluate the
    trigger, run one recovery attempt if it fires, and touch the canonical
    result in exactly one place — appending a summary line to `notes`.
    Never raises past this function: a provider failure or a malformed
    agent response is reported to the terminal and left out of `notes`
    entirely, so a broken recovery pass can never fail a normal
    `verify-change` run or leave a misleading trace in the result."""
    trigger = evaluate_trigger(result)
    if trigger is None:
        typer.echo("AI recovery (experimental): no high-value gap found; skipped.")
        return

    typer.echo(f"AI recovery (experimental): triggered ({trigger.reason})")
    try:
        client = create_default_llm_client(model_spec, stage="ai_recovery")
    except LLMClientError as exc:
        typer.echo(f"AI recovery (experimental): could not create LLM client: {exc}")
        return

    context = build_context(result, trigger)
    try:
        outcome = recover(context, repo_root=repo_root, client=client, trigger_reason=trigger.reason)
    except RecoveryError as exc:
        typer.echo(f"AI recovery (experimental): failed, first-pass result left unchanged: {exc}")
        return

    result.notes.append(summarize_for_notes(outcome.result))
    typer.echo(
        f"AI recovery (experimental): status={outcome.result.status} "
        f"turns={outcome.stats.turns} llm_calls={outcome.stats.llm_calls} "
        f"tokens={outcome.stats.prompt_tokens}+{outcome.stats.completion_tokens}"
    )

    if json_output is not None:
        try:
            target = resolve_output_file_path(json_output, default_filename="change_verification.json")
            recovery_target = target.with_name(target.stem + ".ai_recovery.json")
            write_output_text(recovery_target, json.dumps(build_recovery_view(outcome.result), indent=2))
            typer.echo(f"Wrote AI recovery result: {recovery_target}")
        except (OSError, ValueError) as exc:
            typer.echo(f"AI recovery (experimental): could not write recovery artifact: {exc}")
