"""Trace command plumbing with target grounding against discovered endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from sydes.cli.output_paths import (
    resolve_output_file_path,
    resolve_trace_output_target,
    write_output_text,
)
from sydes.core.models import (
    EndpointCandidate,
    FlowExpansionResult,
    TargetSpec,
    TraceResult,
    TraceSummary,
    Unknown,
)
from sydes.core.confidence import (
    cap_trace_summary_confidence,
    compute_test_matrix_coverage,
    compute_trace_confidence,
)
from sydes.core.graph import (
    add_cross_repo_api_link,
    build_graph_from_inferred_flow,
    enrich_external_api_graph_evidence,
)
from sydes.discover.endpoints import discover_endpoints
from sydes.discover.target_match import resolve_trace_target
from sydes.ingest.repos import parse_repo_specs
from sydes.llm.client import LLMClientError, validate_llm_available
from sydes.report.json_report import render_json
from sydes.report.terminal import render_terminal
from sydes.store.workspace import compute_workspace_id, create_run_id, save_run_artifact
from sydes.generate.tests import generate_test_matrix, generate_test_suggestions
from sydes.trace.cross_repo import (
    build_call_source_lookup_id,
    detect_cross_repo_call_candidates,
    index_discovered_endpoints,
    link_cross_repo_call_candidates,
)
from sydes.trace.expand import prepare_flow_expansion_context, run_flow_expansion
from sydes.trace.sinks import normalize_sink_candidates
from sydes.trace.handler_symbol_index import build_handler_symbol_index_batch
from sydes.trace.handler_resolver import resolve_handler_reference
from sydes.trace.function_body_slicer import slice_resolved_handler_body
from sydes.trace.call_follower import CallFollowBudgets, build_layered_trace_expansion

VERBOSE_NOTE_MARKERS = (
    "Flow expansion context files selected:",
    "Flow expansion prompt chars:",
    "Flow expansion timeout:",
    "Selected ",
    "Included ",
    "Cross-repo candidate normalized:",
    "Matched target endpoint:",
    "Raw call text:",
    "Applied service hint narrowing",
    "Sink merge result:",
    "Flow expansion extracted ",
)


def _build_trace_result(
    path: str,
    method: str | None,
    repo_specs: list[str],
    model_spec: str | None = None,
    strict_llm: bool = False,
) -> tuple[TraceResult, FlowExpansionResult | None, EndpointCandidate | None]:
    """Run endpoint discovery and target resolution to ground a trace target."""
    try:
        repos = parse_repo_specs(repo_specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc

    target = TargetSpec(path=path, method=method)
    routes = discover_endpoints(repos, model_spec=model_spec, strict_llm=strict_llm)
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
        flow_expansion = run_flow_expansion(
            match.selected,
            routes.repos,
            model_spec=model_spec,
            strict_llm=strict_llm,
        )
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
        cross_repo_context = prepare_flow_expansion_context(
            matched_endpoint=match.selected,
            repos=routes.repos,
        )
        cross_repo_calls = detect_cross_repo_call_candidates(
            cross_repo_context,
            source_symbol_hint=match.selected.handler,
        )
        if cross_repo_calls:
            notes.append(
                f"Detected {len(cross_repo_calls)} cross-repo API call candidate(s) from flow context."
            )
            enrich_external_api_graph_evidence(
                nodes=nodes,
                edges=edges,
                calls=cross_repo_calls,
            )
            call_candidates_by_id = {
                build_call_source_lookup_id(item): item for item in cross_repo_calls
            }
            link_results = link_cross_repo_call_candidates(cross_repo_calls, routes.routes)
            endpoint_index = index_discovered_endpoints(routes.routes)
            endpoint_by_id = endpoint_index.get("by_endpoint_id", {})
            linked_count = 0
            ambiguous_count = 0
            no_match_count = 0
            low_confidence_count = 0
            for link in link_results:
                if link.matched_target_endpoint_id is None:
                    no_match_count += 1
                    source_call = call_candidates_by_id.get(link.source_endpoint_id or "")
                    if source_call is not None:
                        method_hint = source_call.normalized_target_method or source_call.target_method or "?"
                        path_hint = source_call.normalized_target_path or source_call.target_path or "?"
                        raw_hint = source_call.raw_call_text or "n/a"
                        notes.append(
                            f"Unmatched cross-repo candidate: {method_hint} {path_hint} (raw: {raw_hint})."
                        )
                    if link.notes:
                        notes.append(link.notes[-1])
                    continue
                if any("ambiguous endpoint link" in note.lower() for note in link.notes):
                    ambiguous_count += 1
                    source_call = call_candidates_by_id.get(link.source_endpoint_id or "")
                    if source_call is not None:
                        method_hint = source_call.normalized_target_method or source_call.target_method or "?"
                        path_hint = source_call.normalized_target_path or source_call.target_path or "?"
                        notes.append(
                            f"Ambiguous cross-repo candidate: {method_hint} {path_hint}."
                        )
                    continue
                if link.confidence is not None and link.confidence < 0.6:
                    low_confidence_count += 1
                    source_call = call_candidates_by_id.get(link.source_endpoint_id or "")
                    if source_call is not None:
                        method_hint = source_call.normalized_target_method or source_call.target_method or "?"
                        path_hint = source_call.normalized_target_path or source_call.target_path or "?"
                        notes.append(
                            f"Low-confidence cross-repo candidate skipped: {method_hint} {path_hint}."
                        )
                    continue
                endpoint_matches = endpoint_by_id.get(link.matched_target_endpoint_id, [])
                if not endpoint_matches:
                    continue
                source_call = call_candidates_by_id.get(link.source_endpoint_id or "")
                if source_call is None:
                    continue
                link_label = add_cross_repo_api_link(
                    nodes=nodes,
                    edges=edges,
                    call=source_call,
                    target_endpoint=endpoint_matches[0],
                    link_type=link.link_type,
                    confidence=link.confidence,
                    evidence=link.evidence,
                )
                if link_label:
                    linked_count += 1
                    notes.append(f"Cross-repo link added: {link_label}.")
                    if link.notes:
                        notes.append(link.notes[-1])
            if linked_count == 0:
                notes.append("No confident cross-repo endpoint links were added.")
            if ambiguous_count:
                notes.append(
                    f"Skipped {ambiguous_count} ambiguous cross-repo link candidate(s)."
                )
            if low_confidence_count:
                notes.append(
                    f"Skipped {low_confidence_count} low-confidence cross-repo link candidate(s)."
                )
            if no_match_count:
                notes.append(
                    f"{no_match_count} cross-repo call candidate(s) had no endpoint match."
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

    summary_confidence = compute_trace_confidence(
        selected_endpoint=match.selected,
        flow_expansion=flow_expansion,
        nodes_count=len(nodes),
        edges_count=len(edges),
    )
    summary_confidence, confidence_capped, cap_reasons = cap_trace_summary_confidence(
        summary_confidence,
        flow_expansion,
        has_strong_grounding=bool(
            match.selected
            and match.selected.handler
            and match.selected.file
            and (
                any(edge.type == "CALLS_API" for edge in edges)
                or (flow_expansion is not None and bool(flow_expansion.sinks))
            )
        ),
    )
    if confidence_capped:
        notes.append(
            f"Confidence capped at {summary_confidence:.2f} due to partial inference "
            f"({'; '.join(cap_reasons)})."
        )

    result = TraceResult(
        target=target,
        repos=routes.repos,
        nodes=nodes,
        edges=edges,
        flows=flows,
        unknowns=unknowns,
        notes=notes,
        summary=TraceSummary(
            confidence=summary_confidence,
            trace_confidence=summary_confidence,
        ),
    )
    if flows:
        result.summary.key_flow_id = flows[0].id
    elif nodes:
        result.summary.key_flow_id = nodes[0].id
    result.tests = generate_test_suggestions(result)
    result.test_matrix = generate_test_matrix(result)
    matrix_coverage = compute_test_matrix_coverage(result, result.test_matrix)
    if matrix_coverage is not None:
        result.summary.test_matrix_coverage = matrix_coverage
        # Backward-compatible alias
        result.summary.test_matrix_confidence = matrix_coverage
        result.test_matrix.coverage = matrix_coverage
        result.test_matrix.confidence = matrix_coverage
    return result, flow_expansion, match.selected


def _write_output(path: Path, content: str) -> None:
    """Write rendered command output to disk."""
    write_output_text(path, content)


def _write_trace_json_outputs(
    output: Path,
    rendered_trace_result: str,
    result: TraceResult | None = None,
    flow_expansion: FlowExpansionResult | None = None,
    handler_symbol_index: dict | None = None,
    resolved_handlers: dict | None = None,
    handler_body_slices: dict | None = None,
    layered_trace_expansion: dict | None = None,
) -> None:
    """Write trace JSON output to either a single file or an artifact directory."""
    target = resolve_trace_output_target(output)
    if target.kind == "file":
        _write_output(target.path, rendered_trace_result)
        return

    _write_output(target.path / "trace_result.json", rendered_trace_result)
    if result is None:
        return

    graph_payload = {
        "target": result.target.model_dump(),
        "key_flow_id": result.summary.key_flow_id,
        "nodes": [item.model_dump() for item in result.nodes],
        "edges": [item.model_dump() for item in result.edges],
        "flows": [item.model_dump() for item in result.flows],
    }
    _write_output(
        target.path / "trace_graph.json",
        json.dumps(graph_payload, indent=2),
    )

    if result.test_matrix is not None:
        _write_output(
            target.path / "test_matrix.json",
            result.test_matrix.model_dump_json(indent=2),
        )

    if flow_expansion is not None:
        _write_output(
            target.path / "flow_expansion.json",
            flow_expansion.model_dump_json(indent=2),
        )
    if handler_symbol_index is not None:
        _write_output(
            target.path / "handler_symbol_index.json",
            json.dumps(handler_symbol_index, indent=2),
        )
    if resolved_handlers is not None:
        _write_output(
            target.path / "resolved_handlers.json",
            json.dumps(resolved_handlers, indent=2),
        )
    if handler_body_slices is not None:
        _write_output(
            target.path / "handler_body_slices.json",
            json.dumps(handler_body_slices, indent=2),
        )
    if layered_trace_expansion is not None:
        _write_output(
            target.path / "layered_trace_expansion.json",
            json.dumps(layered_trace_expansion, indent=2),
        )


def _concise_terminal_notes(notes: list[str]) -> list[str]:
    """Keep concise user-facing notes while hiding verbose debug detail."""
    concise: list[str] = []
    for note in notes:
        if any(marker in note for marker in VERBOSE_NOTE_MARKERS):
            continue
        concise.append(note)
    return concise


def trace_command(
    path: Annotated[str, typer.Argument(help="Target API path, e.g. /checkout")],
    method: Annotated[str | None, typer.Option("--method")] = None,
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
    emit_tests: Annotated[bool, typer.Option("--emit-tests")] = False,
    max_hops: Annotated[int | None, typer.Option("--max-hops")] = None,
    max_files: Annotated[int | None, typer.Option("--max-files")] = None,
    trace_depth: Annotated[int, typer.Option("--trace-depth")] = 2,
    verbose: Annotated[bool, typer.Option("--verbose")] = False,
    allow_partial: Annotated[bool, typer.Option("--allow-partial")] = False,
) -> None:
    """Run target-grounded trace preparation with first-pass downstream expansion."""
    _ = emit_tests, max_hops, max_files
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
                    _write_trace_json_outputs(output, rendered)
                except (OSError, ValueError) as exc:
                    typer.echo(str(exc))
                    raise typer.Exit(code=1) from exc
            raise typer.Exit(code=1)
        typer.echo(f"LLM validation failed: {message}")
        raise typer.Exit(code=1)
    try:
        build_output = _build_trace_result(
            path=path,
            method=method,
            repo_specs=repo or [],
            model_spec=model,
            strict_llm=not allow_partial,
        )
        if len(build_output) == 3:
            result, flow_expansion, matched_endpoint = build_output
        else:
            result, flow_expansion = build_output
            matched_endpoint = None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--repo") from exc
    except LLMClientError as exc:
        message = str(exc)
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
                    _write_trace_json_outputs(output, rendered)
                except (OSError, ValueError) as exc:
                    typer.echo(str(exc))
                    raise typer.Exit(code=1) from exc
            raise typer.Exit(code=1)
        typer.echo(f"LLM trace failed: {message}")
        raise typer.Exit(code=1)

    artifact_payload = {
        "timestamp": datetime.now(tz=UTC).isoformat(),
        "repo_inputs": [item.model_dump() for item in result.repos],
        "target": result.target.model_dump(),
        "result": result.model_dump(),
    }
    handler_symbol_index: dict | None = None
    resolved_handlers_payload: dict | None = None
    handler_body_slices_payload: dict | None = None
    layered_trace_expansion_payload: dict | None = None
    try:
        workspace_id = compute_workspace_id(result.repos)
        run_id = create_run_id()
        handler_symbol_index = build_handler_symbol_index_batch(result.repos)
        resolved_handlers_payload = None
        if (
            matched_endpoint is not None
            and isinstance(matched_endpoint.handler, str)
            and matched_endpoint.handler.strip()
        ):
            repo_index = next(
                (
                    item
                    for item in handler_symbol_index.get("repos", [])
                    if item.get("repo") == matched_endpoint.repo
                ),
                None,
            )
            if repo_index is not None:
                resolved_handlers_payload = {
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "target": result.target.model_dump(),
                    "matched_endpoint": matched_endpoint.model_dump(),
                    "resolution": resolve_handler_reference(matched_endpoint, repo_index),
                }
                resolution = resolved_handlers_payload["resolution"]
                primary = resolution.get("primary_handler")
                if isinstance(primary, dict) and primary.get("symbol"):
                    symbol = primary["symbol"]
                    result.notes.append(
                        "Resolved handler: "
                        f"{primary.get('normalized_handler')} -> {symbol.get('file')}"
                    )
                else:
                    result.notes.append(
                        f"Handler resolution incomplete: {matched_endpoint.handler}"
                    )
                slices: list[dict] = []
                for handler_item in ([resolution.get("primary_handler")] + list(resolution.get("prehandlers", []))):
                    if not isinstance(handler_item, dict):
                        continue
                    symbol = handler_item.get("symbol")
                    if not isinstance(symbol, dict):
                        continue
                    symbol_file = symbol.get("file")
                    if not isinstance(symbol_file, str):
                        continue
                    repo_name = matched_endpoint.repo
                    repo_root_path = None
                    for repo_ref in result.repos:
                        if repo_ref.name == repo_name:
                            candidate = Path(repo_ref.root).expanduser().resolve() / symbol_file
                            if candidate.is_file():
                                repo_root_path = Path(repo_ref.root).expanduser().resolve()
                                break
                    if repo_root_path is None:
                        for repo_ref in result.repos:
                            candidate = Path(repo_ref.root).expanduser().resolve() / symbol_file
                            if candidate.is_file():
                                repo_root_path = Path(repo_ref.root).expanduser().resolve()
                                break
                    if repo_root_path is None:
                        continue
                    slice_payload = slice_resolved_handler_body(
                        repo_root=repo_root_path,
                        handler_name=handler_item.get("normalized_handler") or handler_item.get("handler_hint") or "handler",
                        symbol=symbol,
                        language=str(symbol.get("language") or "typescript"),
                    )
                    if slice_payload is not None:
                        slices.append(slice_payload)
                if slices:
                    handler_body_slices_payload = {
                        "timestamp": datetime.now(tz=UTC).isoformat(),
                        "target": result.target.model_dump(),
                        "matched_endpoint": matched_endpoint.model_dump(),
                        "resolved_handlers": resolution,
                        "slices": slices,
                    }
                    primary_slice = slices[0]
                    result.notes.append(
                        f"handler_body_slices={len(slices)}"
                    )
                    result.notes.append(
                        f"handler_body_slice_statements={primary_slice.get('summary', {}).get('statement_count', 0)}"
                    )
                    result.notes.append(
                        "handler_body_slice_signals="
                        + ",".join(primary_slice.get("summary", {}).get("signals", []))
                    )
                    if trace_depth >= 2:
                        primary_repo_root = None
                        primary_symbol = primary.get("symbol") if isinstance(primary, dict) else None
                        primary_symbol_file = (
                            primary_symbol.get("file")
                            if isinstance(primary_symbol, dict)
                            else None
                        )
                        if isinstance(primary_symbol_file, str):
                            for repo_ref in result.repos:
                                candidate = Path(repo_ref.root).expanduser().resolve() / primary_symbol_file
                                if candidate.is_file():
                                    primary_repo_root = Path(repo_ref.root).expanduser().resolve()
                                    break
                        if primary_repo_root is None:
                            primary_repo_root = repo_root_path
                        budgets = CallFollowBudgets(max_depth=max(1, trace_depth))
                        layered_trace_expansion_payload = build_layered_trace_expansion(
                            repo_root=primary_repo_root,
                            matched_endpoint=matched_endpoint.model_dump(),
                            resolution=resolution,
                            primary_slice=primary_slice,
                            repo_index=repo_index,
                            budgets=budgets,
                        )
                        summary = layered_trace_expansion_payload.get("summary", {})
                        result.notes.append(
                            f"layered_trace_functions_followed={summary.get('functions_followed', 0)}"
                        )
                        result.notes.append(
                            f"layered_trace_steps_added={summary.get('steps_added', 0)}"
                        )
        handler_symbol_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="handler_symbol_index",
            payload={
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "repo_inputs": [item.model_dump() for item in result.repos],
                "target": result.target.model_dump(),
                "index": handler_symbol_index,
            },
        )
        result.notes.append(f"Saved handler symbol index artifact: {handler_symbol_artifact_path}")
        summary = handler_symbol_index.get("summary", {})
        result.notes.append(
            "Handler symbol index summary: "
            f"handler_symbol_index_files={summary.get('files_indexed', 0)}, "
            f"handler_symbol_index_symbols={summary.get('symbols', 0)}, "
            f"handler_symbol_index_imports={summary.get('imports', 0)}, "
            f"handler_symbol_index_exports={summary.get('exports', 0)}."
        )
        trace_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="trace_result",
            payload=artifact_payload,
        )
        result.notes.append(f"Saved trace artifact: {trace_artifact_path}")
        if resolved_handlers_payload is not None:
            resolved_handlers_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="resolved_handlers",
                payload=resolved_handlers_payload,
            )
            result.notes.append(
                f"Saved resolved handlers artifact: {resolved_handlers_artifact_path}"
            )
        if handler_body_slices_payload is not None:
            handler_body_slices_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="handler_body_slices",
                payload=handler_body_slices_payload,
            )
            result.notes.append(
                f"Saved handler body slices artifact: {handler_body_slices_artifact_path}"
            )
        if layered_trace_expansion_payload is not None:
            layered_trace_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="layered_trace_expansion",
                payload=layered_trace_expansion_payload,
            )
            result.notes.append(
                f"Saved layered trace expansion artifact: {layered_trace_artifact_path}"
            )

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
        handler_symbol_index = None

    if output_format == "json":
        rendered = render_json(result)
    else:
        display_result = result
        if not verbose:
            display_result = result.model_copy(deep=True)
            display_result.notes = _concise_terminal_notes(result.notes)
        rendered = render_terminal(display_result)
    typer.echo(rendered)
    if output is not None:
        try:
            if output_format == "json":
                _write_trace_json_outputs(
                    output,
                    rendered,
                    result=result,
                    flow_expansion=flow_expansion,
                    handler_symbol_index=handler_symbol_index,
                    resolved_handlers=resolved_handlers_payload,
                    handler_body_slices=handler_body_slices_payload,
                    layered_trace_expansion=layered_trace_expansion_payload,
                )
            else:
                resolved = resolve_output_file_path(output, default_filename="trace.txt")
                _write_output(resolved, rendered)
        except (OSError, ValueError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
