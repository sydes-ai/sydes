"""`sydes verify-change` CLI: system-level analysis of a backend code change."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.cli.output_paths import resolve_output_file_path, write_output_text
from sydes.code_intelligence.base import CodeIntelligenceError
from sydes.core.models import RepoRef
from sydes.ingest.repos import parse_repo_specs
from sydes.llm.client import LLMClientError, _resolve_provider_and_model, create_default_llm_client
from sydes.observability import trace as _trace
from sydes.recovery.agent import RecoveryBudget, recover
from sydes.recovery.canonical_merge import merge_verified_recovery_into_result
from sydes.recovery.context import build_context
from sydes.recovery.merge import build_recovery_view, summarize_for_notes
from sydes.recovery.schema import RecoveryError
from sydes.recovery.trigger import answered_by_behavioral, evaluate_trigger
from sydes.report.verify_terminal import render_verify_change_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact
from sydes.verify.analyzer import VerifyChangeOptions, analyze_change
from sydes.verify.git_change import GitChangeError
from sydes.verify.models import ChangeVerificationResult


def _run_diffgenome_before_analysis(
    repo_root: Path, base: str, runtime_args: str, probes: int, out_dir: Path | None,
    test_budget: int = 10,
) -> tuple[Path | None, str, dict | None, dict | None]:
    """Run DiffGenome for merge-base(base, HEAD)..HEAD. Returns (artifact path, note, test
    selection, environment); a failure is a note, never an error: the analysis then proceeds
    without runtime evidence. With no runtime configuration, Sydes detects the Python project
    and environment (`sydes.behavioral.python_project`, `.sydes.yml` overrides); with a Python
    runtime and no `--tests`, it selects the tests itself."""
    import shlex
    import subprocess

    from sydes.behavioral.diffgenome_adapter import BehavioralUnavailable, DiffGenomeRequest, run_diffgenome
    from sydes.behavioral.python_project import (
        DetectionError, changed_files, detect_python_project, diffgenome_args, provenance,
    )
    from sydes.behavioral.test_selection import (
        has_tests_arg, runtime_of, select_python_tests, with_selected_tests,
    )

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=True
        ).stdout.strip()

    try:
        head = git("rev-parse", "HEAD")
        merge_base = git("merge-base", base, head)
        args = shlex.split(runtime_args)
        environment: dict | None = None
        if not args:
            changed = changed_files(repo_root, merge_base, head)
            project = detect_python_project(repo_root, changed)
            args = diffgenome_args(project, changed)
            environment = provenance(project)
        selection: dict | None = None
        if runtime_of(args) == "python" and not has_tests_arg(args):
            chosen = select_python_tests(repo_root, merge_base, head, budget=test_budget)
            if not chosen.files:
                return None, (
                    "runtime_evidence unavailable: no existing test file changes or calls the "
                    "changed functions (automatic test selection found none)"
                ), None, environment
            selection = chosen.as_dict()
            args = with_selected_tests(args, chosen)
        request = DiffGenomeRequest(
            repo_root=repo_root, diff=f"{merge_base}..{head}",
            out_dir=out_dir or (Path.cwd() / ".sydes-behavioral"),
            runtime_args=args, probe_budget=probes,
        )
        run_diffgenome(request)
        return request.out_dir / "diffgenome-change.json", "", selection, environment
    except (BehavioralUnavailable, DetectionError, subprocess.CalledProcessError, OSError) as exc:
        return None, f"runtime_evidence unavailable: {exc}", None, None


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
    behavioral_map: Annotated[
        Literal["off", "diffgenome"],
        typer.Option(
            "--behavioral-map",
            help=(
                "Experimental: attach runtime/composed behavioral evidence for the "
                "change from DiffGenome (`diffgenome change`), merged with the "
                "structural view without flattening provenance. `off` (default) "
                "does nothing. Requires DiffGenome on PATH or "
                "SYDES_DIFFGENOME_COMMAND, and --behavioral-args naming the target's "
                "runtime and test location. Never changes the verdict; when the "
                "evidence cannot be obtained the report says so explicitly."
            ),
        ),
    ] = "off",
    behavioral_args: Annotated[
        str,
        typer.Option(
            "--behavioral-args",
            help=(
                "DiffGenome runtime configuration, forwarded opaquely, e.g. "
                "'--runtime go --test-root api --tests ./api/ --mock-dir db/mock' or "
                "'--runtime python --python .venv/bin/python --test-root tests'."
            ),
        ),
    ] = "",
    behavioral_artifact: Annotated[
        Path | None,
        typer.Option(
            "--behavioral-artifact",
            help=(
                "Consume an existing diffgenome-change/1 artifact instead of running "
                "DiffGenome (deterministic replays, CI artifacts)."
            ),
        ),
    ] = None,
    behavioral_probes: Annotated[
        int,
        typer.Option(
            "--behavioral-probes",
            help=(
                "DiffGenome probe budget: how many generated probes it may attempt "
                "for gaps near the change (each is at most one LLM call). 0 (default): "
                "existing tests only, no LLM call."
            ),
        ),
    ] = 0,
    behavioral_out: Annotated[
        Path | None,
        typer.Option("--behavioral-out", help="Where DiffGenome writes its run (default: ./.sydes-behavioral)."),
    ] = None,
    runtime_test_budget: Annotated[
        int,
        typer.Option(
            "--runtime-test-budget",
            help=(
                "Python runtime evidence without --tests in --behavioral-args: at most this many "
                "test files are selected automatically (changed in the diff first, then files that "
                "call the changed functions), capped at about 100 test functions."
            ),
        ),
    ] = 10,
    runtime_evidence: Annotated[
        str,
        typer.Option(
            "--runtime-evidence",
            help=(
                "`auto` (default): when DiffGenome is the behavioral map and its artifact carries "
                "runtime evidence (diffgenome-runtime/1), use it in Sydes' own analysis: observed "
                "call edges for reachability, the exact tests that executed each changed function, "
                "runtime verification gaps, and the AI-recovery decision. A live DiffGenome run "
                "then happens before analysis. `off`: render the evidence only."
            ),
        ),
    ] = "auto",
    behavioral_context: Annotated[
        str,
        typer.Option(
            "--behavioral-context",
            help=(
                "Experimental. `on`: also give the compact runtime evidence to the code-review and "
                "PR semantic-analysis models. `off` (default): prompts are unchanged."
            ),
        ),
    ] = "off",
    behavioral_review_context: Annotated[
        str,
        typer.Option(
            "--behavioral-review-context",
            help=(
                "Experimental. `on`: give the code-review and PR semantic-analysis models the "
                "checked behavioral rules of the --behavioral-artifact's genome section (verified "
                "and supported rules, checked identities and literals, declared unknowns) as "
                "advisory evidence before they read the change. `off` (default): prompts are "
                "unchanged. Never affects obligations or the verdict."
            ),
        ),
    ] = "off",
    mutation_verify: Annotated[
        bool,
        typer.Option(
            "--mutation-verify",
            help=(
                "Targeted comparator-boundary mutation check (off by "
                "default). For a validation obligation this run introduced "
                "and already resolved as passed, flips a `>`/`>=`/`</`<=` "
                "found on a line inside the diff's own hunks, reruns that "
                "obligation's own mapped test against the mutated file, "
                "then restores the original file. If the test still "
                "passes, the boundary may be uncovered -- reported as "
                "`mutation_survived`, never as proof it is untested. Only "
                "runs when --no-run-tests is not set (it needs to execute "
                "a test to mean anything); capped at 3 obligations per "
                "run. See sydes.verify.mutation."
            ),
        ),
    ] = False,
) -> None:
    """Analyze a change, run the tests that verify it, and report the evidence."""
    run_started = time.monotonic()
    _trace.reset_run_usage()
    try:
        repos = parse_repo_specs(repo or [])
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    if not repos:
        repos = [RepoRef(name="api", root=str(Path.cwd()))]

    checked_preamble = ""
    checked_note = ""
    if behavioral_review_context not in ("off", "on"):
        raise typer.BadParameter("must be off or on", param_hint="--behavioral-review-context")
    if behavioral_review_context == "on":
        from sydes.behavioral.review_context import checked_evidence_block, load_genome_summary, review_preamble

        genome = load_genome_summary(behavioral_artifact)
        checked_preamble = review_preamble(genome)
        checked_note = (
            f"behavioral_review_context=supplied chars={len(checked_evidence_block(genome))}"
            if checked_preamble
            else "behavioral_review_context unavailable: no --behavioral-artifact with a checked "
            "genome section; review prompts unchanged"
        )

    if behavioral_context not in ("off", "on"):
        raise typer.BadParameter("must be off or on", param_hint="--behavioral-context")
    if runtime_evidence not in ("off", "auto"):
        raise typer.BadParameter("must be auto or off", param_hint="--runtime-evidence")
    loaded_context = None
    test_selection: dict | None = None
    runtime_environment: dict | None = None
    live_run_failed: str | None = None
    use_runtime = behavioral_map == "diffgenome" and (runtime_evidence == "auto" or behavioral_context == "on")
    if use_runtime:
        from sydes.behavioral.context import BehavioralContext

        if behavioral_artifact is None:
            # live: run DiffGenome before analysis so its evidence can inform reachability; the
            # post-analysis attach then reuses this artifact instead of running it again
            behavioral_artifact, early_note, test_selection, runtime_environment = _run_diffgenome_before_analysis(
                Path(repos[0].root), base, behavioral_args, behavioral_probes, behavioral_out,
                test_budget=runtime_test_budget,
            )
            if early_note:
                checked_note = early_note
                live_run_failed = early_note.removeprefix("runtime_evidence unavailable: ")
        loaded_context = BehavioralContext.load(behavioral_artifact)
        if loaded_context is None and not checked_note:
            checked_note = (
                "runtime_evidence unavailable: the DiffGenome artifact carries no diffgenome-runtime/1 "
                "section; analysis unchanged"
            )

    options = VerifyChangeOptions(
        behavioral_context=loaded_context,
        behavioral_prompt_context=behavioral_context == "on",
        checked_behavior_preamble=checked_preamble,
        base=base,
        include_working_tree=not no_working_tree,
        code_review=code_review,
        llm_policy=llm_policy,
        model_spec=model,
        run_tests=not no_run_tests,
        test_timeout_seconds=test_timeout,
        impact_guide=impact_guide,
        persist_system_model=persist_system_model,
        mutation_verify=mutation_verify,
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

    if checked_note:
        result.diagnostics.append(checked_note)

    if behavioral_map != "off":
        from sydes.behavioral.attach import attach_behavioral_evidence

        attach_behavioral_evidence(
            result, repo_root=Path(repos[0].root), provider=behavioral_map,
            artifact_path=behavioral_artifact, runtime_args=behavioral_args,
            probe_budget=behavioral_probes, out_dir=behavioral_out,
            use_runtime=runtime_evidence == "auto", test_selection=test_selection,
            runtime_environment=runtime_environment,
            unavailable_reason=live_run_failed,
        )

    if no_ai_recovery:
        _note_ai_recovery_disabled(result)
    else:
        _run_ai_recovery(
            result, repo_root=Path(repos[0].root), model_spec=model, json_output=json_output,
            include_repo_routes=recovery_route_context,
            max_edge_turns=recovery_max_edge_turns, max_edge_retries=recovery_max_edge_retries,
            use_graph_path_search=recovery_graph_path_search,
        )

    result.run_metrics = run_metrics(result, wall_seconds=time.monotonic() - run_started)
    result.diagnostics.append(run_metrics_line(result.run_metrics))

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


#: `SYDES_LLM_PRICE_PER_MTOK="<input>,<output>"`: USD per million input/output tokens for
#: the selected model. Sydes keeps no price list; without it no cost is estimated.
PRICE_ENV_VAR = "SYDES_LLM_PRICE_PER_MTOK"


def _price_per_mtok() -> tuple[float, float] | None:
    raw = os.environ.get(PRICE_ENV_VAR, "").strip()
    try:
        input_price, output_price = (float(part) for part in raw.split(","))
    except ValueError:
        return None
    return input_price, output_price


def run_metrics(result: ChangeVerificationResult, *, wall_seconds: float) -> dict:
    """Run economics, as summary metadata: LLM calls by purpose with tokens (and an estimated
    cost when a price is configured), CBM requests (enrichment's own included), wall time."""
    usage = _trace.run_usage(price_per_mtok=_price_per_mtok())
    enrichment = next((item for item in result.diagnostics if item.startswith("structural_enrichment: triggered")), "")
    match = re.search(r"cbm_requests=(\d+)", enrichment)
    usage["cbm"]["enrichment_requests"] = int(match.group(1)) if match else 0
    usage["wall_seconds"] = round(wall_seconds, 1)
    return usage


def run_metrics_line(metrics: dict) -> str:
    totals = metrics["llm_totals"]
    purposes = ",".join(f"{stage}:{entry['calls']}" for stage, entry in sorted(metrics["llm_by_purpose"].items()))
    cost = metrics.get("estimated_cost_usd")
    return (
        f"run_metrics: wall_seconds={metrics['wall_seconds']} llm_calls={totals['calls']} ({purposes or 'none'}) "
        f"llm_errors={totals['errors']} input_tokens={totals['input_tokens']} output_tokens={totals['output_tokens']} "
        f"estimated_cost_usd={cost if cost is not None else f'unavailable (set {PRICE_ENV_VAR})'} "
        f"cbm_requests={metrics['cbm']['calls']} cbm_enrichment_requests={metrics['cbm']['enrichment_requests']}"
    )


AI_RECOVERY_DISABLED_NOTE = "AI recovery: disabled (--no-ai-recovery); not run."


def _note_ai_recovery_disabled(result: ChangeVerificationResult) -> None:
    """With `--no-ai-recovery`, say so: the report must not read as if recovery ran."""
    result.notes.append(AI_RECOVERY_DISABLED_NOTE)
    typer.echo("AI recovery (experimental): disabled (--no-ai-recovery).")


def _recovery_model_label(model_spec: str | None) -> str:
    try:
        provider, model, _ = _resolve_provider_and_model(model_spec)
    except Exception:  # noqa: BLE001 - a label only; never fail the run over it
        return model_spec or "default model"
    return f"{provider}:{model}"


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
    answered = answered_by_behavioral(result)
    if answered:
        result.notes.append(
            "AI recovery: gap(s) already answered by executed behavioral evidence, not "
            "sent to the model: " + ", ".join(answered)
        )
    if trigger is None:
        suffix = " (the rest was answered by executed behavioral evidence)" if answered else ""
        typer.echo(f"AI recovery (experimental): no high-value gap found; skipped{suffix}.")
        return

    typer.echo(f"AI recovery (experimental): triggered ({trigger.reason})")
    try:
        # No pinned temperature: some models reject an explicit value (e.g.
        # one observed rejecting 0.0, accepting only their own default) --
        # the same fix already applied to the impact guide, route
        # discovery, and code review. Every request the recovery pipeline
        # itself sends already omits `temperature` too (see
        # `recovery/verify.py`, `recovery/react.py`), so without this the
        # client's own settings-derived default (0.0) was the one value
        # actually reaching the provider -- confirmed failing for
        # `gpt-5.6-sol` on two real repos.
        client = create_default_llm_client(model_spec, temperature=None, stage="ai_recovery")
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
        typer.echo(
            f"AI recovery (experimental): failed ({_recovery_model_label(model_spec)}), "
            f"first-pass result left unchanged: {exc}"
        )
        return

    merged = merge_verified_recovery_into_result(result, outcome.path_recovery, outcome.test_recovery)
    result.notes.append(summarize_for_notes(outcome.path_recovery, outcome.test_recovery, merged=merged))
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
