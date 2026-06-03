"""Trace command plumbing with target grounding against discovered endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import asdict
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
    EvidenceRef,
    EndpointCandidate,
    FlowExpansionResult,
    FlowStep,
    Flow,
    GraphEdge,
    GraphNode,
    RoutesResult,
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
from sydes.generate.contracts import build_api_contract_from_routes
from sydes.generate.contract_llm_refinement import refine_api_contract_with_evidence_packet
from sydes.generate.evidence_packet import build_evidence_packet_for_route
from sydes.generate.test_llm_generation import generate_test_matrix_with_evidence_packet
from sydes.generate.tests import clean_test_matrix, generate_test_matrix, generate_test_suggestions, match_route_contract
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
from sydes.trace.trace_llm_summarizer import run_trace_llm_summarizer
from sydes.trace.layered_contract import build_layered_trace_contract

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
    route_contract = None
    try:
        repo_roots = {item.name: item.root for item in routes.repos}
        contract_artifact = build_api_contract_from_routes(routes, repo_roots=repo_roots)
        route_contract = match_route_contract(
            contract_artifact,
            method=target.method,
            path=target.path,
        )
    except Exception:  # noqa: BLE001
        route_contract = None
    try:
        result.test_matrix = generate_test_matrix(result, route_contract=route_contract)
    except TypeError:
        # Compatibility for patched/mocked single-arg test helpers.
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
    trace_llm_summary: dict | None = None,
    layered_trace_contract: dict | None = None,
    api_contract: dict | None = None,
    evidence_packet: dict | None = None,
    llm_test_generation: dict | None = None,
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
    if trace_llm_summary is not None:
        _write_output(
            target.path / "trace_llm_summary.json",
            json.dumps(trace_llm_summary, indent=2),
        )
    if layered_trace_contract is not None:
        _write_output(
            target.path / "layered_trace_contract.json",
            json.dumps(layered_trace_contract, indent=2),
        )
    if api_contract is not None:
        _write_output(
            target.path / "api_contract.json",
            json.dumps(api_contract, indent=2),
        )
    if evidence_packet is not None:
        _write_output(
            target.path / "evidence_packet.json",
            json.dumps(evidence_packet, indent=2),
        )
    if llm_test_generation is not None:
        _write_output(
            target.path / "llm_test_generation.json",
            json.dumps(llm_test_generation, indent=2),
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
    trace_llm_policy: Annotated[
        Literal["auto", "always", "never"], typer.Option("--trace-llm-policy")
    ] = "auto",
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
    trace_llm_summary_payload: dict | None = None
    layered_contract_payload: dict | None = None
    api_contract_payload: dict | None = None
    api_contract_model = None
    route_contract_model = None
    evidence_packet_payload: dict | None = None
    llm_contract_refinement_payload: dict | None = None
    llm_test_generation_payload: dict | None = None
    artifact_index: dict[str, str] = {}

    if matched_endpoint is not None:
        try:
            contract_result = build_api_contract_from_routes(
                RoutesResult(repos=result.repos, routes=[matched_endpoint]),
                repo_roots={item.name: item.root for item in result.repos},
            )
            api_contract_model = contract_result
            route_contract_model = match_route_contract(
                contract_result,
                method=result.target.method,
                path=result.target.path,
            )
            api_contract_payload = contract_result.model_dump()
        except Exception:  # noqa: BLE001
            api_contract_model = None
            route_contract_model = None
            api_contract_payload = None

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
                    unresolved = resolution.get("unresolved_handlers", [])
                    if unresolved and isinstance(unresolved[0], dict):
                        diag = unresolved[0].get("diagnostics", {})
                        if isinstance(diag, dict):
                            result.notes.append(
                                "Handler resolution failed: "
                                f"normalized={diag.get('normalized')}, "
                                f"route_file={diag.get('route_file')}, "
                                f"resolved_file={diag.get('resolved_file')}, "
                                f"file_indexed={diag.get('file_indexed')}, "
                                f"class_candidates={diag.get('class_candidates')}, "
                                f"method_candidates={diag.get('method_candidates')}"
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
                    try:
                        trace_llm_summary_payload = run_trace_llm_summarizer(
                            model_spec=model,
                            route={"matched_endpoint": matched_endpoint.model_dump()},
                            resolved_handlers=resolved_handlers_payload,
                            primary_slice=primary_slice,
                            layered_trace_expansion=layered_trace_expansion_payload,
                            policy=trace_llm_policy,
                            budgets=asdict(budgets) if trace_depth >= 2 else {"max_depth": trace_depth},
                        )
                        if trace_llm_summary_payload.get("skipped"):
                            result.notes.append(
                                "trace_llm_summarizer=skipped"
                            )
                        else:
                            result.notes.append(
                                "trace_llm_summarizer=ran"
                            )
                            llm_result = trace_llm_summary_payload.get("result") or {}
                            summary_text = llm_result.get("summary")
                            if isinstance(summary_text, str) and summary_text.strip():
                                result.notes.append(f"Trace summary: {summary_text.strip()}")
                            for warning in trace_llm_summary_payload.get("warnings", []):
                                if isinstance(warning, str) and warning.strip():
                                    result.notes.append(f"Trace LLM summary warning: {warning}")
                    except LLMClientError as exc:
                        if trace_llm_policy == "always":
                            result.notes.append(f"Trace LLM summarizer failed: {exc}")
                        else:
                            result.diagnostics.append(f"trace_llm_summarizer_failed={exc}")
                    artifact_names = {}
                    layered_contract_payload = build_layered_trace_contract(
                        matched_endpoint=matched_endpoint.model_dump(),
                        primary_slice=primary_slice,
                        resolved_handlers=resolved_handlers_payload,
                        layered_trace_expansion=layered_trace_expansion_payload,
                        llm_summary=trace_llm_summary_payload,
                        budgets=asdict(budgets) if trace_depth >= 2 else {"max_depth": trace_depth},
                        artifact_paths=artifact_names,
                    )
                    result.matched_endpoint = matched_endpoint
                    result.flow = layered_contract_payload.get("flow")
                    result.layers = layered_contract_payload.get("layers", [])
                    result.sinks = layered_contract_payload.get("sinks", [])
                    result.resolved_handlers = layered_contract_payload.get("resolved_handlers", [])
                    result.budgets = layered_contract_payload.get("budgets")
                    result.diagnostics = layered_contract_payload.get("diagnostics", [])
                    if isinstance(layered_contract_payload.get("summary"), str) and layered_contract_payload.get("summary"):
                        result.summary.text = layered_contract_payload.get("summary")

                    # Add layered steps to graph in source order for UI-friendly trace graph.
                    steps = (result.flow or {}).get("steps", []) if isinstance(result.flow, dict) else []
                    if steps:
                        flow_steps: list[FlowStep] = []
                        prev_node_id: str | None = None
                        for idx, step in enumerate(steps, start=1):
                            if not isinstance(step, dict):
                                continue
                            node_id = f"layered:{idx}"
                            evidence = []
                            for ref in step.get("evidence", []):
                                if isinstance(ref, dict) and isinstance(ref.get("file"), str):
                                    evidence.append(
                                        EvidenceRef(
                                            file=ref.get("file"),
                                            symbol=ref.get("symbol"),
                                            label=ref.get("label"),
                                            snippet=ref.get("snippet"),
                                        )
                                    )
                            result.nodes.append(
                                GraphNode(
                                    id=node_id,
                                    type=str(step.get("kind") or "step"),
                                    name=str(step.get("name") or step.get("detail") or "step"),
                                    repo=step.get("repo"),
                                    file=step.get("file"),
                                    symbol=step.get("symbol"),
                                    metadata={
                                        "detail": step.get("detail"),
                                        "depth": step.get("depth"),
                                        "layer": step.get("layer"),
                                        "line_start": step.get("line_start"),
                                        "line_end": step.get("line_end"),
                                    },
                                    evidence=evidence,
                                    confidence=step.get("confidence"),
                                    status=step.get("status"),
                                )
                            )
                            flow_steps.append(
                                FlowStep(node_id=node_id, kind=str(step.get("kind") or "unknown_important"))
                            )
                            if prev_node_id is not None:
                                result.edges.append(
                                    GraphEdge(
                                        id=f"layered-edge:{idx-1}:{idx}",
                                        source=prev_node_id,
                                        target=node_id,
                                        type="NEXT_STEP",
                                        repo=step.get("repo"),
                                        status="grounded",
                                        confidence=step.get("confidence"),
                                    )
                                )
                            prev_node_id = node_id
                        if flow_steps:
                            layered_flow = Flow(
                                id="flow:layered",
                                name="layered_handler_trace",
                                entry_node=flow_steps[0].node_id,
                                steps=flow_steps,
                                summary=result.summary.text,
                                confidence=result.summary.trace_confidence,
                            )
                            result.flows.insert(0, layered_flow)
                            result.summary.key_flow_id = layered_flow.id
        try:
            packet = build_evidence_packet_for_route(
                trace_result=result,
                api_contract=api_contract_model,
                test_matrix=result.test_matrix,
                repo_roots={item.name: item.root for item in result.repos},
            )
            should_refine_contract = (
                trace_llm_policy == "always"
                or (
                    trace_llm_policy == "auto"
                    and model is not None
                    and bool(packet.source_windows)
                )
            )
            if should_refine_contract:
                refinement = refine_api_contract_with_evidence_packet(
                    evidence_packet=packet,
                    current_contract=route_contract_model,
                    model_spec=model,
                )
                llm_contract_refinement_payload = {
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "target": result.target.model_dump(),
                    "ok": refinement.ok,
                    "raw_output": refinement.raw_output,
                    "parsed_output": refinement.parsed_output,
                    "warnings": refinement.warnings,
                    "error": refinement.error,
                }
                if refinement.ok and refinement.refined_contract is not None:
                    result.notes.append("LLM contract refinement applied from evidence packet.")
                    route_contract_model = refinement.refined_contract
                    if api_contract_model is not None:
                        replaced = False
                        for idx, route_item in enumerate(api_contract_model.routes):
                            if (
                                (route_item.method or "").upper()
                                == (result.target.method or "").upper()
                                and route_item.path == result.target.path
                            ):
                                api_contract_model.routes[idx] = refinement.refined_contract
                                replaced = True
                                break
                        if not replaced:
                            api_contract_model.routes.append(refinement.refined_contract)
                        api_contract_payload = api_contract_model.model_dump()
                    else:
                        api_contract_payload = {
                            "version": "v1",
                            "routes": [refinement.refined_contract.model_dump(mode="json")],
                            "notes": ["Refined from graph-grounded evidence packet."],
                            "confidence": None,
                        }
                    packet.current_contract = refinement.refined_contract.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                elif refinement.error:
                    result.diagnostics.append(
                        f"llm_contract_refinement_failed={refinement.error}"
                    )
                    if trace_llm_policy == "always":
                        result.notes.append(
                            f"LLM contract refinement failed: {refinement.error}"
                        )
            should_generate_tests = (
                trace_llm_policy == "always"
                or (
                    trace_llm_policy == "auto"
                    and model is not None
                    and bool(packet.source_windows)
                )
            )
            if should_generate_tests and result.test_matrix is not None:
                generation = generate_test_matrix_with_evidence_packet(
                    evidence_packet=packet,
                    api_contract=route_contract_model,
                    current_test_matrix=result.test_matrix,
                    model_spec=model,
                )
                accepted_scenarios = (
                    sum(len(group.tests) for group in generation.test_matrix.groups)
                    if generation.test_matrix is not None
                    else 0
                )
                llm_test_generation_payload = {
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "target": result.target.model_dump(),
                    "ok": generation.ok,
                    "raw_output": generation.raw_output,
                    "parsed_output": generation.parsed_output,
                    "warnings": generation.warnings,
                    "error": generation.error,
                    "accepted_scenarios": accepted_scenarios,
                    "model": model,
                    "policy": trace_llm_policy,
                }
                if generation.ok and generation.test_matrix is not None:
                    result.test_matrix = generation.test_matrix
                    result.notes.append("LLM test generation applied from evidence packet.")
                elif generation.error:
                    result.diagnostics.append(f"llm_test_generation_failed={generation.error}")
                    if trace_llm_policy == "always":
                        result.notes.append(f"LLM test generation failed: {generation.error}")
                for warning in generation.warnings:
                    if trace_llm_policy == "always":
                        result.notes.append(f"LLM test generation warning: {warning}")
                    else:
                        result.diagnostics.append(f"llm_test_generation_warning={warning}")
            if result.test_matrix is not None:
                cleaned_matrix = clean_test_matrix(
                    result.test_matrix,
                    api_contract=route_contract_model or api_contract_model,
                    trace_result=result,
                )
                result.test_matrix = cleaned_matrix
                matrix_coverage = compute_test_matrix_coverage(result, result.test_matrix)
                if matrix_coverage is not None:
                    result.summary.test_matrix_coverage = matrix_coverage
                    result.summary.test_matrix_confidence = matrix_coverage
                    result.test_matrix.coverage = matrix_coverage
                    result.test_matrix.confidence = matrix_coverage
            evidence_packet_payload = packet.model_dump(mode="json", exclude_none=True)
        except Exception as exc:  # noqa: BLE001
            result.notes.append(f"Could not build evidence packet: {exc}")
            evidence_packet_payload = None
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
        artifact_index["handler_symbol_index"] = str(handler_symbol_artifact_path)
        summary = handler_symbol_index.get("summary", {})
        result.notes.append(
            "Handler symbol index summary: "
            f"handler_symbol_index_files={summary.get('files_indexed', 0)}, "
            f"handler_symbol_index_symbols={summary.get('symbols', 0)}, "
            f"handler_symbol_index_imports={summary.get('imports', 0)}, "
            f"handler_symbol_index_exports={summary.get('exports', 0)}."
        )
        artifact_payload["result"] = result.model_dump()
        trace_artifact_path = save_run_artifact(
            workspace_id=workspace_id,
            run_id=run_id,
            artifact_name="trace_result",
            payload=artifact_payload,
        )
        result.notes.append(f"Saved trace artifact: {trace_artifact_path}")
        artifact_index["trace_result"] = str(trace_artifact_path)
        if api_contract_payload is not None:
            api_contract_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="api_contract",
                payload={
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "repo_inputs": [item.model_dump() for item in result.repos],
                    "target": result.target.model_dump(),
                    "contract": api_contract_payload,
                },
            )
            result.notes.append(f"Saved API contract artifact: {api_contract_artifact_path}")
            artifact_index["api_contract"] = str(api_contract_artifact_path)
        if evidence_packet_payload is not None:
            evidence_packet_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="evidence_packet",
                payload=evidence_packet_payload,
            )
            result.notes.append(f"Saved evidence packet artifact: {evidence_packet_artifact_path}")
            artifact_index["evidence_packet"] = str(evidence_packet_artifact_path)
        if llm_contract_refinement_payload is not None:
            llm_contract_refinement_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="llm_contract_refinement",
                payload=llm_contract_refinement_payload,
            )
            result.notes.append(
                f"Saved LLM contract refinement artifact: {llm_contract_refinement_artifact_path}"
            )
            artifact_index["llm_contract_refinement"] = str(llm_contract_refinement_artifact_path)
        if llm_test_generation_payload is not None:
            llm_test_generation_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="llm_test_generation",
                payload=llm_test_generation_payload,
            )
            result.notes.append(
                f"Saved LLM test generation artifact: {llm_test_generation_artifact_path}"
            )
            artifact_index["llm_test_generation"] = str(llm_test_generation_artifact_path)
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
            artifact_index["resolved_handlers"] = str(resolved_handlers_artifact_path)
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
            artifact_index["handler_body_slices"] = str(handler_body_slices_artifact_path)
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
            artifact_index["layered_trace_expansion"] = str(layered_trace_artifact_path)
        if trace_llm_summary_payload is not None:
            trace_llm_summary_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="trace_llm_summary",
                payload=trace_llm_summary_payload,
            )
            result.notes.append(
                f"Saved trace LLM summary artifact: {trace_llm_summary_artifact_path}"
            )
            artifact_index["trace_llm_summary"] = str(trace_llm_summary_artifact_path)
        if layered_contract_payload is not None:
            contract_artifact_path = save_run_artifact(
                workspace_id=workspace_id,
                run_id=run_id,
                artifact_name="layered_trace_contract",
                payload=layered_contract_payload,
            )
            result.notes.append(
                f"Saved layered trace contract artifact: {contract_artifact_path}"
            )
            artifact_index["layered_trace_contract"] = str(contract_artifact_path)

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
            artifact_index["flow_expansion"] = str(expansion_artifact_path)

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
            artifact_index["trace_graph"] = str(graph_artifact_path)
        if artifact_index:
            result.artifacts = artifact_index
            if layered_contract_payload is not None:
                layered_contract_payload["artifacts"] = dict(artifact_index)
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
                    trace_llm_summary=trace_llm_summary_payload,
                    layered_trace_contract=layered_contract_payload,
                    api_contract=api_contract_payload,
                    evidence_packet=evidence_packet_payload,
                    llm_test_generation=llm_test_generation_payload,
                )
            else:
                resolved = resolve_output_file_path(output, default_filename="trace.txt")
                _write_output(resolved, rendered)
        except (OSError, ValueError) as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
