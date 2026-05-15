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

    if result.flows:
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
            label = "endpoint" if step.kind == "endpoint" else "step"
            display_name = _normalize_step_label_for_terminal(node.name) if label == "step" else node.name
            display_index += 1
            lines.append(f"  {display_index}. {label}: {display_name}")
            details: list[str] = []
            if node.file:
                details.append(f"file={node.file}")
            if node.symbol:
                details.append(f"symbol={node.symbol}")
            if node.repo:
                details.append(f"repo={node.repo}")
            if details:
                lines.append(f"     ({', '.join(details)})")

    sink_types = {"database", "external_api", "queue", "file_sink", "sink"}
    sink_nodes = [node for node in result.nodes if node.type in sink_types]
    if sink_nodes:
        lines.append("Sinks:")
        for sink in sink_nodes:
            action = sink.metadata.get("action") if isinstance(sink.metadata, dict) else None
            action_value = f"{action} " if isinstance(action, str) and action else ""
            lines.append(f"  - {sink.type}: {action_value}{sink.name}")
            details: list[str] = []
            if sink.file:
                details.append(f"file={sink.file}")
            if sink.symbol:
                details.append(f"symbol={sink.symbol}")
            if sink.repo:
                details.append(f"repo={sink.repo}")
            if details:
                lines.append(f"    ({', '.join(details)})")

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
    if result.summary.test_matrix_confidence is not None:
        lines.append(f"Test Matrix Confidence: {result.summary.test_matrix_confidence:.2f}")

    unmatched = [item for item in result.unknowns if item.kind == "unmatched_target"]
    if unmatched:
        lines.append("No endpoint match found for the requested target.")
    lines.append("Trace is inferred from code and may be partial.")

    if result.notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in result.notes)
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
