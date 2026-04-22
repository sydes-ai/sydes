"""Trace command plumbing with target grounding against discovered endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.core.models import (
    FlowExpansionResult,
    TargetSpec,
    TraceResult,
    TraceSummary,
    Unknown,
)
from sydes.core.graph import build_graph_from_inferred_flow
from sydes.discover.endpoints import discover_endpoints
from sydes.discover.target_match import resolve_trace_target
from sydes.ingest.repos import parse_repo_specs
from sydes.report.json_report import render_json
from sydes.report.terminal import render_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact
from sydes.generate.tests import generate_test_suggestions
from sydes.trace.expand import run_flow_expansion
from sydes.trace.sinks import normalize_sink_candidates


def _build_trace_result(
    path: str, method: str | None, repo_specs: list[str]
) -> tuple[TraceResult, FlowExpansionResult | None]:
    """Run endpoint discovery and target resolution to ground a trace target."""
    try:
        repos = parse_repo_specs(repo_specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    target = TargetSpec(path=path, method=method)
    routes = discover_endpoints(repos)
    match = resolve_trace_target(
        routes.routes,
        path=target.path,
        method=target.method,
    )

    nodes = []
    edges = []
    flows = []
    unknowns: list[Unknown] = []
    notes: list[str] = []
    notes.extend(routes.notes)
    notes.extend(match.notes)
    flow_expansion: FlowExpansionResult | None = None

    if match.selected is not None:
        flow_expansion = run_flow_expansion(match.selected, routes.repos)
        flow_expansion.sinks = normalize_sink_candidates(flow_expansion.sinks)
        graph_nodes, graph_edges, graph_flows = build_graph_from_inferred_flow(
            match.selected,
            flow_expansion,
        )
        nodes.extend(graph_nodes)
        edges.extend(graph_edges)
        flows.extend(graph_flows)
        notes.extend(flow_expansion.notes)
        notes.append(
            f"Flow expansion extracted {len(flow_expansion.steps)} step(s) and "
            f"{len(flow_expansion.sinks)} sink candidate(s)."
        )
        if match.alternatives:
            notes.append(
                f"{len(match.alternatives)} alternative endpoint candidate(s) available for target."
            )
    else:
        unknowns.append(
            Unknown(
                id=f"target:{target.path}:{target.method or 'ANY'}",
                kind="unmatched_target",
                description=(
                    f"No discovered endpoint matched target {target.method or 'ANY'} {target.path}."
                ),
                confidence=0.0,
            )
        )

    for idx, alternative in enumerate(match.alternatives, start=1):
        unknowns.append(
            Unknown(
                id=f"alternative:{idx}:{alternative.repo}:{alternative.file}",
                kind="ambiguous_target_candidate",
                service=alternative.service,
                repo=alternative.repo,
                file=alternative.file,
                symbol=alternative.handler,
                description=(
                    f"Alternative candidate {alternative.method or '?'} {alternative.path or '?'}"
                ),
                confidence=alternative.confidence,
            )
        )

    if flow_expansion is not None and flow_expansion.confidence is not None:
        summary_confidence = flow_expansion.confidence
    elif match.selected is not None:
        summary_confidence = match.confidence
    elif routes.confidence_summary is not None:
        summary_confidence = routes.confidence_summary.average
    else:
        summary_confidence = 0.0

    result = TraceResult(
        target=target,
        repos=routes.repos,
        nodes=nodes,
        edges=edges,
        flows=flows,
        unknowns=unknowns,
        notes=notes,
        summary=TraceSummary(confidence=summary_confidence),
    )
    if flows:
        result.summary.key_flow_id = flows[0].id
    elif nodes:
        result.summary.key_flow_id = nodes[0].id
    result.tests = generate_test_suggestions(result)
    return result, flow_expansion


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
    """Run target-grounded trace preparation with first-pass downstream expansion."""
    _ = emit_tests, max_hops, max_files
    try:
        result, flow_expansion = _build_trace_result(
            path=path,
            method=method,
            repo_specs=repo or [],
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    artifact_payload = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "repo_inputs": [item.model_dump() for item in result.repos],
        "target": result.target.model_dump(),
        "result": result.model_dump(),
    }
    try:
        workspace_id = compute_workspace_id(result.repos)
        run_id = create_run_id()
        trace_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="trace_result",
            payload=artifact_payload,
        )
        result.notes.append(f"Saved trace artifact: {trace_artifact_path}")

        if flow_expansion is not None:
            expansion_artifact_payload = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "repo_inputs": [item.model_dump() for item in result.repos],
                "target": result.target.model_dump(),
                "entry_node_id": result.summary.key_flow_id,
                "expansion": flow_expansion.model_dump(),
            }
            expansion_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="flow_expansion",
                payload=expansion_artifact_payload,
            )
            result.notes.append(f"Saved flow expansion artifact: {expansion_artifact_path}")

        if result.nodes or result.edges or result.flows:
            graph_artifact_payload = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "repo_inputs": [item.model_dump() for item in result.repos],
                "target": result.target.model_dump(),
                "key_flow_id": result.summary.key_flow_id,
                "graph": {
                    "nodes": [item.model_dump() for item in result.nodes],
                    "edges": [item.model_dump() for item in result.edges],
                    "flows": [item.model_dump() for item in result.flows],
                },
            }
            graph_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="trace_graph",
                payload=graph_artifact_payload,
            )
            result.notes.append(f"Saved graph artifact: {graph_artifact_path}")
    except OSError as exc:
        result.notes.append(f"Could not save trace artifact: {exc}")

    rendered = render_json(result) if output_format == "json" else render_terminal(result)
    typer.echo(rendered)
    if output is not None:
        _write_output(output, rendered)
