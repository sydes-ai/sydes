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


def render_terminal(result: TraceResult) -> str:
    """Build a target-grounded terminal summary for a trace result."""
    method = result.target.method or "ANY"
    lines = [
        "Sydes Trace Target Resolution",
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
        flow = None
        if result.summary.key_flow_id is not None:
            flow = next((item for item in result.flows if item.id == result.summary.key_flow_id), None)
        if flow is None:
            flow = result.flows[0]

        for index, step in enumerate(flow.steps, start=1):
            node = node_by_id.get(step.node_id)
            if node is None:
                lines.append(f"  {index}. step: {step.node_id}")
                continue
            label = "step"
            if step.kind == "endpoint":
                label = "endpoint"
            elif step.kind.startswith("sink:"):
                label = "sink"
            display_name = _normalize_step_label_for_terminal(node.name) if label == "step" else node.name
            lines.append(f"  {index}. {label}: {display_name}")
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

    if result.tests:
        lines.append("Suggested Tests:")
        for suggestion in result.tests:
            lines.append(f"  - {suggestion.name}")
            if suggestion.summary:
                lines.append(f"    {suggestion.summary}")
            rendered_expectations = suggestion.expectations[:3]
            for expectation in rendered_expectations:
                lines.append(f"    expects: {expectation.description}")

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

    if result.summary.confidence is not None:
        lines.append(f"Confidence: {result.summary.confidence:.2f}")

    unmatched = [item for item in result.unknowns if item.kind == "unmatched_target"]
    if unmatched:
        lines.append("No endpoint match found for the requested target.")
    lines.append("Downstream flow expansion is heuristic and may be partial.")

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
