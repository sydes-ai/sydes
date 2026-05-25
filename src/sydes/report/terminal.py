"""Terminal rendering for human-readable Sydes summaries."""

from collections import defaultdict

from sydes.core.models import RoutesResult, TraceResult


def _normalize_step_label_for_terminal(label: str) -> str:
    """Lightly normalize step label display for readability in terminal output."""
    normalized = " ".join(label.strip().split())
    if "." in normalized:
        return normalized
    normalized = normalized.replace("_", " ")
    return " ".join(normalized.split())


def _format_matrix_group_title(category: str, title: str | None = None) -> str:
    """Format test-matrix group heading for compact terminal display."""
    if title:
        return title.strip()
    mapped = category.replace("_", " ").strip()
    return mapped.title()


def _classify_trace_note(note: str) -> str:
    """Classify one trace note for Evidence/Diagnostics/Artifacts sections."""
    lowered = note.lower()
    if "saved " in lowered and "artifact" in lowered:
        return "artifacts"

    diagnostic_markers = (
        "candidate_files=",
        "files_sent_to_llm=",
        "prompt_chars=",
        "flow expansion context files selected:",
        "flow expansion prompt chars:",
        "flow expansion timeout:",
        "using anchor file:",
        "anchor file appears sufficient",
        "selected ",
        "included ",
        "truncated",
        "llm timeout",
        "no nearby related files were selected",
        "contextual file",
        "discovery unavailable",
        "unavailable:",
    )
    if any(marker in lowered for marker in diagnostic_markers):
        return "diagnostics"

    return "evidence"


def _format_flow_step_headline(step_kind: str, node_name: str, metadata: dict) -> str:
    """Format flow step headline with concrete operation labels when available."""
    kind = (step_kind or "").strip().lower()
    if kind == "dependency":
        dep_name = node_name
        if dep_name.startswith("Depends(") and dep_name.endswith(")"):
            dep_name = dep_name[len("Depends(") : -1]
        return f"dependency: {dep_name}"
    if kind == "db_read":
        target = metadata.get("target_entity") if isinstance(metadata, dict) else None
        return f"database read: {target or node_name}"
    if kind == "db_write":
        target = metadata.get("target_entity") if isinstance(metadata, dict) else None
        return f"database write: {target or node_name}"
    if kind == "external_api_call":
        return f"external call: {node_name}"
    if kind == "input_model":
        return f"input model: {node_name.replace('input model: ', '')}"
    return f"step: {_normalize_step_label_for_terminal(node_name)}"


def _extract_evidence_expression(node) -> str | None:
    """Extract one-line concrete expression evidence from node metadata/evidence."""
    if isinstance(node.metadata, dict):
        expression = node.metadata.get("expression")
        if isinstance(expression, str) and expression.strip():
            return expression.strip()
    for ref in node.evidence:
        label = ref.label or ""
        for prefix in (
            "deterministic:db_read:",
            "deterministic:db_write:",
            "deterministic:external_call:",
            "deterministic:dependency:",
        ):
            if label.startswith(prefix):
                expression = label[len(prefix) :].strip()
                if expression:
                    return expression
    return None


def _extract_evidence_snippet(node) -> str | None:
    """Extract one-line code snippet evidence when available."""
    for ref in node.evidence:
        if isinstance(ref.snippet, str) and ref.snippet.strip():
            snippet = " ".join(ref.snippet.strip().split())
            if len(snippet) > 180:
                return snippet[:177] + "..."
            return snippet
    return None


def render_terminal(result: TraceResult) -> str:
    """Build a target-grounded terminal summary for a trace result."""
    method = result.target.method or "ANY"
    lines = [
        "Sydes API Flow Trace",
        f"Target: {method} {result.target.path}",
        "Repos:",
    ]

    if result.repos:
        lines.extend(f"  - {repo.name}: {repo.root}" for repo in result.repos)
    else:
        lines.append("  - (none)")

    if result.nodes:
        endpoint_nodes = [node for node in result.nodes if node.type == "api_endpoint"]
        if endpoint_nodes:
            lines.append("Matched endpoint:")
            for node in endpoint_nodes:
                path_value = node.path or "?"
                method_value = node.method or "?"
                lines.append(f"  - {method_value} {path_value}")
                details: list[str] = []
                if node.repo:
                    details.append(f"repo={node.repo}")
                if node.service:
                    details.append(f"service={node.service}")
                if node.symbol:
                    details.append(f"handler={node.symbol}")
                if node.file:
                    details.append(f"file={node.file}")
                if details:
                    lines.append(f"    ({', '.join(details)})")

    if result.summary.text:
        lines.append("Summary:")
        lines.append(f"  {result.summary.text}")

    layered_steps = []
    if isinstance(result.flow, dict):
        layered_steps = [item for item in result.flow.get("steps", []) if isinstance(item, dict)]

    if layered_steps:
        lines.append("Flow:")
        for idx, step in enumerate(layered_steps, start=1):
            kind = (step.get("kind") or "step").replace("_", " ")
            detail = step.get("detail") or step.get("name") or "step"
            lines.append(f"  {idx}. {kind}: {detail}")
            details: list[str] = []
            if step.get("file"):
                details.append(f"file={step.get('file')}")
            if step.get("symbol"):
                details.append(f"symbol={step.get('symbol')}")
            if step.get("repo"):
                details.append(f"repo={step.get('repo')}")
            if details:
                lines.append(f"     ({', '.join(details)})")
            evidence = step.get("evidence") or []
            if evidence and isinstance(evidence[0], dict):
                snippet = evidence[0].get("snippet")
                if isinstance(snippet, str) and snippet.strip():
                    compact = " ".join(snippet.split())
                    if len(compact) > 140:
                        compact = compact[:137] + "..."
                    lines.append(f"     evidence: {compact}")
    elif result.flows:
        lines.append("Flow:")
        node_by_id = {node.id: node for node in result.nodes}
        sink_types = {"database", "external_api", "queue", "file_sink", "sink"}
        flow = None
        if result.summary.key_flow_id is not None:
            flow = next((item for item in result.flows if item.id == result.summary.key_flow_id), None)
        if flow is None:
            flow = result.flows[0]

        display_index = 0
        for step in flow.steps:
            node = node_by_id.get(step.node_id)
            if node is None:
                display_index += 1
                lines.append(f"  {display_index}. step: {step.node_id}")
                continue
            if step.kind.startswith("sink:") or node.type in sink_types:
                continue
            display_index += 1
            if step.kind == "endpoint":
                lines.append(f"  {display_index}. endpoint: {node.name}")
            else:
                lines.append(
                    f"  {display_index}. {_format_flow_step_headline(step.kind, node.name, node.metadata)}"
                )
            details: list[str] = []
            if node.file:
                details.append(f"file={node.file}")
            if node.symbol:
                details.append(f"symbol={node.symbol}")
            if node.repo:
                details.append(f"repo={node.repo}")
            if details:
                lines.append(f"     ({', '.join(details)})")
            expression = _extract_evidence_expression(node)
            if expression:
                lines.append(f"     evidence: {expression}")

    sink_types = {"database", "external_api", "queue", "file_sink", "sink"}
    sink_nodes = [node for node in result.nodes if node.type in sink_types]
    layered_sinks = result.sinks or []
    if sink_nodes:
        lines.append("Sinks:")
        for sink in sink_nodes:
            action = sink.metadata.get("action") if isinstance(sink.metadata, dict) else None
            target = sink.metadata.get("target_entity") if isinstance(sink.metadata, dict) else None
            operation = sink.metadata.get("operation") if isinstance(sink.metadata, dict) else None
            target_path = sink.metadata.get("target_path") if isinstance(sink.metadata, dict) else None
            action_value = f"{action} " if isinstance(action, str) and action else ""
            sink_name = target_path or target or sink.name
            lines.append(f"  - {sink.type}: {action_value}{sink_name}")
            if isinstance(operation, str) and operation.strip():
                lines.append(f"    operation: {operation.strip()}")
            snippet = _extract_evidence_snippet(sink)
            if snippet:
                lines.append(f"    evidence: {snippet}")
            details: list[str] = []
            if sink.file:
                details.append(f"file={sink.file}")
            if sink.symbol:
                details.append(f"symbol={sink.symbol}")
            if sink.repo:
                details.append(f"repo={sink.repo}")
            if details:
                lines.append(f"    ({', '.join(details)})")
    elif layered_sinks:
        lines.append("Sinks:")
        for sink in layered_sinks:
            action = sink.get("operation") or sink.get("action") or ""
            name = sink.get("name") or sink.get("kind") or "sink"
            lines.append(f"  - {sink.get('kind', 'sink')}: {action} {name}".strip())
            evidence = sink.get("evidence") or []
            if evidence and isinstance(evidence[0], dict):
                snippet = evidence[0].get("snippet")
                if isinstance(snippet, str) and snippet.strip():
                    compact = " ".join(snippet.split())
                    if len(compact) > 140:
                        compact = compact[:137] + "..."
                    lines.append(f"    evidence: {compact}")

    cross_repo_edges = [edge for edge in result.edges if edge.type == "CALLS_API"]
    unmatched_cross_repo_notes = [
        note for note in result.notes if note.startswith("Unmatched cross-repo candidate:")
    ]
    if cross_repo_edges or unmatched_cross_repo_notes:
        lines.append("Cross-Repo Links:")
        node_by_id = {node.id: node for node in result.nodes}
        seen_links: set[tuple[str, str, str, str]] = set()
        for edge in cross_repo_edges:
            source = node_by_id.get(edge.source)
            target = node_by_id.get(edge.target)
            if source is None or target is None:
                continue
            source_repo = source.repo or "unknown"
            target_repo = target.repo or "unknown"
            target_method = target.method or "?"
            target_path = target.path or target.name
            dedupe_key = (source_repo, target_repo, target_method, target_path)
            if dedupe_key in seen_links:
                continue
            seen_links.add(dedupe_key)
            lines.append(f"  - {source_repo} -> {target_repo}::{target_method} {target_path}")
        if not cross_repo_edges:
            lines.append("  - none")
        for note in unmatched_cross_repo_notes:
            lines.append(f"  - {note}")

    if result.test_matrix and result.test_matrix.groups:
        rendered_group = False
        for group in result.test_matrix.groups:
            group_tests = group.tests[:2]
            if not group_tests:
                continue
            if not rendered_group:
                lines.append("Test Matrix:")
                rendered_group = True
            title = _format_matrix_group_title(group.category, group.title)
            lines.append(f"  {title}:")
            for suggestion in group_tests:
                lines.append(f"    - {suggestion.name}")
                summary = suggestion.summary
                if not summary and suggestion.expectations:
                    summary = suggestion.expectations[0].description
                if summary:
                    lines.append(f"      {summary}")

    ambiguous = [item for item in result.unknowns if item.kind == "ambiguous_target_candidate"]
    if ambiguous:
        lines.append("Alternatives:")
        for item in ambiguous:
            lines.append(f"  - {item.description}")
            details: list[str] = []
            if item.repo:
                details.append(f"repo={item.repo}")
            if item.service:
                details.append(f"service={item.service}")
            if item.file:
                details.append(f"file={item.file}")
            if item.symbol:
                details.append(f"handler={item.symbol}")
            if details:
                lines.append(f"    ({', '.join(details)})")

    trace_conf = (
        result.summary.trace_confidence
        if result.summary.trace_confidence is not None
        else result.summary.confidence
    )
    if trace_conf is not None:
        lines.append(f"Trace Confidence: {trace_conf:.2f}")
    matrix_coverage = (
        result.summary.test_matrix_coverage
        if result.summary.test_matrix_coverage is not None
        else result.summary.test_matrix_confidence
    )
    if matrix_coverage is not None:
        lines.append(f"Test Matrix Coverage: {matrix_coverage:.2f}")

    unmatched = [item for item in result.unknowns if item.kind == "unmatched_target"]
    if unmatched:
        lines.append("No endpoint match found for the requested target.")
    lines.append(
        "Trace is inferred from static code context and may miss runtime configuration or dynamic behavior."
    )

    if result.notes:
        evidence_notes: list[str] = []
        diagnostics_notes: list[str] = []
        artifact_notes: list[str] = []
        for note in result.notes:
            category = _classify_trace_note(note)
            if category == "artifacts":
                artifact_notes.append(note)
            elif category == "diagnostics":
                diagnostics_notes.append(note)
            else:
                evidence_notes.append(note)

        if evidence_notes:
            lines.append("Evidence:")
            lines.extend(f"  - {note}" for note in evidence_notes)
        if diagnostics_notes:
            lines.append("Diagnostics:")
            lines.extend(f"  - {note}" for note in diagnostics_notes)
        if artifact_notes:
            lines.append("Artifacts:")
            lines.extend(f"  - {note}" for note in artifact_notes)
    if result.diagnostics:
        lines.append("Diagnostics:")
        lines.extend(f"  - {item}" for item in result.diagnostics)
    return "\n".join(lines)


def render_routes_terminal(result: RoutesResult) -> str:
    """Build a grouped terminal summary for routes discovery."""
    lines = [
        "Sydes Routes Discovery",
        "Repos:",
    ]

    if result.repos:
        lines.extend(f"  - {repo.name}: {repo.root}" for repo in result.repos)
    else:
        lines.append("  - (none)")

    lines.append(f"Candidate files considered: {result.candidate_files}")
    lines.append(f"Files examined: {result.files_examined}")
    lines.append(f"Files sent to LLM: {result.files_sent_to_llm}")
    lines.append(f"Routes discovered: {len(result.routes)}")

    if result.confidence_summary and result.confidence_summary.average is not None:
        lines.append(f"Average confidence: {result.confidence_summary.average:.2f}")
    if result.prompt_chars:
        lines.append(f"Prompt chars (total): {result.prompt_chars}")
    if result.timeout_seconds is not None:
        lines.append(f"LLM timeout: {result.timeout_seconds:.0f}s")
    if result.truncated_files:
        lines.append(f"Truncated files sent: {result.truncated_files}")

    if result.routes:
        grouped: dict[tuple[str, str], list] = defaultdict(list)
        for route in result.routes:
            grouped[(route.repo, route.service or "(unknown-service)")].append(route)

        lines.append("Discovered routes by repo/service:")
        for repo_name, service_name in sorted(grouped):
            lines.append(f"  {repo_name} / {service_name}:")
            for route in grouped[(repo_name, service_name)]:
                method = route.method or "?"
                path = route.path or "?"
                lines.append(f"    - {method} {path}")
                if route.handler:
                    lines.append(f"      handler={route.handler}")
                if route.file:
                    lines.append(f"      file={route.file}")
                if route.confidence is not None:
                    lines.append(f"      confidence={route.confidence:.2f}")
                if route.status:
                    lines.append(f"      status={route.status}")
    else:
        lines.append("No routes discovered yet")

    if result.notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in result.notes)

    return "\n".join(lines)
