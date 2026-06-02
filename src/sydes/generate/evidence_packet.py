"""Build compact graph-grounded evidence packets for future LLM extraction."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from sydes.core.models import (
    ApiContractArtifact,
    EvidenceEndpoint,
    EvidencePacket,
    EvidencePacketLimits,
    EvidenceSink,
    EvidenceSourceWindow,
    EvidenceTraceEdge,
    EvidenceTraceNode,
    GraphEdge,
    GraphNode,
    TestMatrix,
    TraceResult,
)
from sydes.generate.tests import match_route_contract


def build_evidence_packet_for_route(
    trace_result: TraceResult,
    api_contract: ApiContractArtifact | None = None,
    test_matrix: TestMatrix | None = None,
    repo_roots: dict[str, str] | None = None,
    max_source_chars: int = 8000,
    max_nodes: int = 40,
    max_edges: int = 60,
) -> EvidencePacket:
    """Build a compact evidence packet for a selected trace result."""

    limits = EvidencePacketLimits(
        max_source_chars=max_source_chars,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    notes: list[str] = []
    endpoint = _endpoint_from_trace(trace_result)

    trace_nodes = [
        _compact_node(node)
        for node in trace_result.nodes[:max_nodes]
    ]
    if len(trace_result.nodes) > max_nodes:
        notes.append(f"Trace nodes truncated: kept {max_nodes} of {len(trace_result.nodes)}.")

    trace_edges = [
        _compact_edge(edge)
        for edge in trace_result.edges[:max_edges]
    ]
    if len(trace_result.edges) > max_edges:
        notes.append(f"Trace edges truncated: kept {max_edges} of {len(trace_result.edges)}.")

    sinks = _collect_sinks(trace_result)
    current_contract = _selected_contract_payload(
        api_contract,
        method=endpoint.method,
        path=endpoint.path,
    )
    current_test_matrix_summary = _summarize_test_matrix(
        test_matrix or trace_result.test_matrix,
        limits.max_test_scenarios,
    )

    source_windows = _source_windows_for_trace(
        trace_result=trace_result,
        endpoint=endpoint,
        repo_roots=repo_roots or {repo.name: repo.root for repo in trace_result.repos},
        max_source_chars=max_source_chars,
        notes=notes,
    )

    return EvidencePacket(
        endpoint=endpoint,
        source_windows=source_windows,
        trace_nodes=trace_nodes,
        trace_edges=trace_edges,
        sinks=sinks,
        current_contract=current_contract,
        current_test_matrix_summary=current_test_matrix_summary,
        notes=notes,
        limits=limits,
    )


def render_evidence_packet_json(packet: EvidencePacket) -> str:
    """Serialize an evidence packet using stable JSON formatting."""

    return packet.model_dump_json(indent=2)


def _endpoint_from_trace(trace_result: TraceResult) -> EvidenceEndpoint:
    matched = trace_result.matched_endpoint
    method = trace_result.target.method or (matched.method if matched else None) or "ANY"
    return EvidenceEndpoint(
        method=method,
        path=trace_result.target.path,
        repo=matched.repo if matched else None,
        handler=matched.handler if matched else None,
        file=matched.file if matched else None,
    )


def _compact_node(node: GraphNode) -> EvidenceTraceNode:
    kind = _node_kind(node)
    return EvidenceTraceNode(
        id=node.id,
        type=node.type,
        name=node.name,
        kind=kind,
        repo=node.repo,
        file=node.file,
        symbol=node.symbol,
        snippet=_best_snippet(node),
        confidence=node.confidence,
        status=node.status,
    )


def _compact_edge(edge: GraphEdge) -> EvidenceTraceEdge:
    return EvidenceTraceEdge(
        id=edge.id,
        source=edge.source,
        target=edge.target,
        type=edge.type,
        snippet=_best_snippet(edge),
        confidence=edge.confidence,
    )


def _node_kind(node: GraphNode) -> str | None:
    for key in ("step_kind", "kind", "layer", "action"):
        value = node.metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return node.type


def _best_snippet(item: GraphNode | GraphEdge) -> str | None:
    for evidence in item.evidence:
        if evidence.snippet:
            return _truncate_one_line(evidence.snippet, 600)
        if evidence.label:
            return _truncate_one_line(evidence.label, 600)
    metadata = getattr(item, "metadata", {})
    if isinstance(metadata, dict):
        for key in ("snippet", "expression", "detail", "operation"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_one_line(value, 600)
    return None


def _collect_sinks(trace_result: TraceResult) -> list[EvidenceSink]:
    sinks: list[EvidenceSink] = []
    for sink in trace_result.sinks:
        if not isinstance(sink, dict):
            continue
        name = sink.get("name") or sink.get("target") or sink.get("operation") or "sink"
        sinks.append(
            EvidenceSink(
                name=str(name),
                kind=str(sink.get("kind") or sink.get("type") or "") or None,
                repo=_optional_str(sink.get("repo")),
                file=_optional_str(sink.get("file")),
                symbol=_optional_str(sink.get("symbol")),
                snippet=_snippet_from_payload(sink),
                confidence=_optional_float(sink.get("confidence")),
            )
        )

    for node in trace_result.nodes:
        kind = _node_kind(node)
        if kind in {"store_write", "database_write", "database_read"} or node.type in {
            "database",
            "external_api",
            "queue",
            "file_sink",
        }:
            sinks.append(
                EvidenceSink(
                    name=node.name,
                    kind=kind or node.type,
                    repo=node.repo,
                    file=node.file,
                    symbol=node.symbol,
                    snippet=_best_snippet(node),
                    confidence=node.confidence,
                )
            )
    return _dedupe_sinks(sinks)


def _dedupe_sinks(sinks: list[EvidenceSink]) -> list[EvidenceSink]:
    seen: set[tuple[str, str | None, str | None]] = set()
    out: list[EvidenceSink] = []
    for sink in sinks:
        key = (sink.name, sink.kind, sink.file)
        if key in seen:
            continue
        seen.add(key)
        out.append(sink)
    return out


def _selected_contract_payload(
    api_contract: ApiContractArtifact | None,
    method: str,
    path: str,
) -> dict[str, Any] | None:
    if api_contract is None:
        return None
    route_contract = match_route_contract(api_contract, method=method, path=path)
    if route_contract is None:
        return None
    return route_contract.model_dump(mode="json", exclude_none=True)


def _summarize_test_matrix(
    test_matrix: TestMatrix | None,
    max_scenarios: int,
) -> dict[str, Any] | None:
    if test_matrix is None:
        return None
    scenarios: list[dict[str, Any]] = []
    for group in test_matrix.groups:
        for test in group.tests:
            scenarios.append(
                {
                    "name": test.name,
                    "category": test.category or group.category,
                    "priority": test.priority,
                    "expected_status": (test.expected or {}).get("status")
                    if isinstance(test.expected, dict)
                    else None,
                    "contract_refs": list(test.contract_refs),
                }
            )
            if len(scenarios) >= max_scenarios:
                break
        if len(scenarios) >= max_scenarios:
            break
    return {
        "group_count": len(test_matrix.groups),
        "scenario_count": sum(len(group.tests) for group in test_matrix.groups),
        "scenarios": scenarios,
        "coverage": test_matrix.coverage,
        "confidence": test_matrix.confidence,
    }


def _source_windows_for_trace(
    trace_result: TraceResult,
    endpoint: EvidenceEndpoint,
    repo_roots: dict[str, str],
    max_source_chars: int,
    notes: list[str],
) -> list[EvidenceSourceWindow]:
    candidates: list[tuple[str | None, str | None, str | None]] = []
    if endpoint.file:
        candidates.append((endpoint.repo, endpoint.file, endpoint.handler))
    for node in trace_result.nodes:
        if node.file:
            candidates.append((node.repo, node.file, node.symbol))

    windows: list[EvidenceSourceWindow] = []
    seen_files: set[tuple[str | None, str]] = set()
    remaining = max_source_chars
    for repo, file_name, symbol in candidates:
        key = (repo, file_name or "")
        if not file_name or key in seen_files or remaining <= 0:
            continue
        seen_files.add(key)
        window = _source_window(
            repo=repo,
            file=file_name,
            symbol=symbol,
            repo_roots=repo_roots,
            max_chars=remaining,
            notes=notes,
        )
        if window is None:
            continue
        windows.append(window)
        remaining -= len(window.code)
    return windows


def _source_window(
    repo: str | None,
    file: str,
    symbol: str | None,
    repo_roots: dict[str, str],
    max_chars: int,
    notes: list[str],
) -> EvidenceSourceWindow | None:
    source_path = _resolve_source_path(repo, file, repo_roots)
    if source_path is None:
        notes.append(f"Source unavailable for {file}: repo root not found.")
        return None
    try:
        text = source_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        notes.append(f"Source unavailable for {file}: {exc}.")
        return None

    lines = text.splitlines()
    start, end = (
        _python_symbol_range(text, symbol)
        if source_path.suffix == ".py"
        else (None, None)
    )
    if start is None or end is None:
        start, end = _fallback_window(lines)
    excerpt_lines = lines[start - 1 : end]
    code = "\n".join(excerpt_lines)
    truncated = start > 1 or end < len(lines)
    if len(code) > max_chars:
        code = code[:max_chars]
        truncated = True
    return EvidenceSourceWindow(
        repo=repo,
        file=file,
        symbol=symbol,
        start_line=start,
        end_line=end,
        code=code,
        truncated=truncated,
    )


def _resolve_source_path(
    repo: str | None,
    file: str,
    repo_roots: dict[str, str],
) -> Path | None:
    candidate = Path(file).expanduser()
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    if repo and repo in repo_roots:
        candidate = Path(repo_roots[repo]).expanduser() / file
        if candidate.is_file():
            return candidate
    for root in repo_roots.values():
        candidate = Path(root).expanduser() / file
        if candidate.is_file():
            return candidate
    return None


def _python_symbol_range(text: str, symbol: str | None) -> tuple[int | None, int | None]:
    if not symbol:
        return None, None
    symbol_name = symbol.split(".")[-1]
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None, None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol_name:
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if start is not None and end is not None:
                return start, end
    return None, None


def _fallback_window(lines: list[str], window_lines: int = 80) -> tuple[int, int]:
    if not lines:
        return 1, 1
    return 1, min(len(lines), window_lines)


def _truncate_one_line(value: str, max_chars: int) -> str:
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max(0, max_chars - 3)].rstrip() + "..."


def _snippet_from_payload(payload: dict[str, Any]) -> str | None:
    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict):
                snippet = item.get("snippet") or item.get("label")
                if isinstance(snippet, str) and snippet.strip():
                    return _truncate_one_line(snippet, 600)
    for key in ("snippet", "detail", "operation", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return _truncate_one_line(value, 600)
    return None


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None
