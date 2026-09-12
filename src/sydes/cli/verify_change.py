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
from sydes.recovery.agent import RecoveryBudget, recover
from sydes.recovery.canonical_merge import merge_verified_recovery_into_result
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
    no_ai_recovery: Annotated[
        bool,
        typer.Option(
            "--no-ai-recovery",
            help=(
                "Opt out of AI recovery (on by default). When the first pass "
                "leaves a high-value gap (no established path, a boundary with "
                "no complete flow, or a missing test mapping despite a changed "
                "test file), Sydes automatically runs a second-stage LLM "
                "recovery pass that may search the whole repository. It never "
                "alters the structural result, verdict, or CBM graph; a run "
                "that cannot establish the missing evidence leaves the "
                "first-pass result completely unchanged. A successful run adds "
                "its findings via a summary line in `notes` and its own JSON "
                "artifact, never by rewriting the verdict. Use this flag to "
                "skip that pass entirely (cost control, debugging, or a repo "
                "with no LLM provider configured). See `sydes.recovery` for "
                "the full design."
            ),
        ),
    ] = False,
    persist_system_model: Annotated[
        bool,
        typer.Option(
            "--persist-system-model",
            help=(
                "MVP: persist canonical flow/symbol entities and per-obligation "
                "verification history across runs (off by default; never changes "
                "the verdict). When on, an obligation already established under "
                "the exact same head commit (with unchanged tracked inputs) "
                "restores that result instead of re-running the test suite; a "
                "different commit always runs the suite normally, even if the "
                "tracked inputs still match — see sydes.verify.system_model_reconcile."
            ),
        ),
    ] = False,
    recovery_route_context: Annotated[
        bool,
        typer.Option(
            "--recovery-route-context",
            help=(
                "Give AI recovery (see --no-ai-recovery) the full list of routes "
                "this run's own deterministic route discovery found in the repo, "
                "not only the ones already connected to this diff. Off by "
                "default. This exists for the same reason "
                "`declarative_entrypoints` already does: recovery's discovery "
                "step has no other deterministic anchor when the changed "
                "behavior is far (in file-tree distance) from the HTTP route "
                "that reaches it, and without one it can propose the wrong "
                "kind of node as the entrypoint entirely. A route named here "
                "is a hint only; sydes.recovery.verify still independently "
                "proves or rejects every hop before anything is merged into "
                "the canonical result — see sydes.recovery.canonical_merge."
            ),
        ),
    ] = False,
    recovery_max_edge_turns: Annotated[
        int | None,
        typer.Option(
            "--recovery-max-edge-turns",
            help=(
                "Override AI recovery's per-hop tool-call turn budget "
                "(sydes.recovery.agent.RecoveryBudget.max_edge_turns, default "
                "4). A pure search-capacity knob: raising it lets Stage B "
                "spend more turns searching for one hop's evidence before "
                "giving up; it changes nothing about what counts as "
                "evidence or how sydes.recovery.verify judges it. Unset "
                "keeps the default."
            ),
        ),
    ] = None,
    recovery_max_edge_retries: Annotated[
        int,
        typer.Option(
            "--recovery-max-edge-retries",
            help=(
                "How many additional, fresh attempts a single recovery "
                "edge gets (same prove_relationship call, same prompt) "
                "after both the direct proof and recursive decomposition "
                "still leave it with no evidence at all. 0 (default): no "
                "retry, identical to previous behavior. A search-capacity "
                "knob only — sydes.recovery.verify's proof requirements "
                "are unchanged either way."
            ),
        ),
    ] = 0,
    recovery_graph_path_search: Annotated[
        bool,
        typer.Option(
            "--recovery-graph-path-search",
            help=(
                "Before the LLM-driven discover/prove loop, try a "
                "deterministic candidate path per changed symbol built "
                "entirely from CBM's already-indexed graph (CALLS/USAGE "
                "edges plus a generic decorator/annotation-argument "
                "correlation) -- see sydes.recovery.graph_path. Off by "
                "default. Costs no extra LLM calls beyond the one shared "
                "Layer-2 judging pass every candidate edge already goes "
                "through: a graph-proposed path is judged by "
                "sydes.recovery.verify exactly like an LLM-proposed one, "
                "never trusted more or less because of where it came "
                "from."
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
        persist_system_model=persist_system_model,
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

    if not no_ai_recovery:
        _run_ai_recovery(
            result, repo_root=Path(repos[0].root), model_spec=model, json_output=json_output,
            include_repo_routes=recovery_route_context,
            max_edge_turns=recovery_max_edge_turns, max_edge_retries=recovery_max_edge_retries,
            use_graph_path_search=recovery_graph_path_search,
        )

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
    include_repo_routes: bool = False, max_edge_turns: int | None = None, max_edge_retries: int = 0,
    use_graph_path_search: bool = False,
) -> None:
    """The entire AI-recovery integration surface, run automatically by
    default (see `--no-ai-recovery`): evaluate the trigger, run one
    recovery attempt if it fires, and — only for what it actually
    ESTABLISHED — merge it into the canonical result via
    `sydes.recovery.canonical_merge`, plus a one-line summary in `notes`
    either way. Never raises past this function: no LLM provider
    configured, a provider failure, or a malformed agent response is
    reported to the terminal and left out of the result entirely, so a
    broken recovery pass can never fail a normal `verify-change` run or
    leave a misleading trace. `summary.verdict`/`risk`/`headline` and
    CBM's graph are never touched here, whether recovery establishes
    something or not — see `canonical_merge` for exactly what is.

    `include_repo_routes` (see `--recovery-route-context`) only changes
    what discovery is SHOWN; `sydes.recovery.verify`'s Layer 0/1/2 proof
    requirements are exactly the same either way."""
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

    context = build_context(result, trigger, repo_root=repo_root, include_repo_routes=include_repo_routes)
    budget = None
    if max_edge_turns is not None or max_edge_retries:
        budget_kwargs: dict[str, int] = {"max_edge_retries": max_edge_retries}
        if max_edge_turns is not None:
            budget_kwargs["max_edge_turns"] = max_edge_turns
        budget = RecoveryBudget(**budget_kwargs)
    try:
        outcome = recover(
            context, repo_root=repo_root, client=client, trigger_reason=trigger.reason, budget=budget,
            use_graph_path_search=use_graph_path_search,
        )
    except RecoveryError as exc:
        typer.echo(f"AI recovery (experimental): failed, first-pass result left unchanged: {exc}")
        return

    merge_verified_recovery_into_result(result, outcome.path_recovery, outcome.test_recovery)
    result.notes.append(summarize_for_notes(outcome.path_recovery, outcome.test_recovery))
    typer.echo(
        f"AI recovery (experimental): path={outcome.path_recovery.status} test={outcome.test_recovery.status} "
        f"turns={outcome.stats.turns} llm_calls={outcome.stats.llm_calls} "
        f"tokens={outcome.stats.prompt_tokens}+{outcome.stats.completion_tokens} "
        f"edge_retries_attempted={outcome.stats.edge_retries_attempted} "
        f"edge_retries_succeeded={outcome.stats.edge_retries_succeeded} "
        f"graph_paths_proposed={outcome.stats.graph_paths_proposed} "
        f"graph_paths_established={outcome.stats.graph_paths_established} "
        f"graph_paths_established_unverified_boundary={outcome.stats.graph_paths_established_unverified_boundary}"
    )

    if json_output is not None:
        try:
            target = resolve_output_file_path(json_output, default_filename="change_verification.json")
            recovery_target = target.with_name(target.stem + ".ai_recovery.json")
            payload = build_recovery_view(outcome.path_recovery, outcome.test_recovery)
            write_output_text(recovery_target, json.dumps(payload, indent=2))
            typer.echo(f"Wrote AI recovery result: {recovery_target}")
        except (OSError, ValueError) as exc:
            typer.echo(f"AI recovery (experimental): could not write recovery artifact: {exc}")
