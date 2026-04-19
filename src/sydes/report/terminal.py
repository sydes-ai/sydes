"""Terminal rendering for human-readable Sydes summaries."""

from collections import defaultdict

from sydes.core.models import RoutesResult, TraceResult


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
    lines.append("Downstream flow tracing is planned for the next phase.")

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
    lines.append(f"Routes discovered: {len(result.routes)}")

    if result.confidence_summary and result.confidence_summary.average is not None:
        lines.append(f"Average confidence: {result.confidence_summary.average:.2f}")

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
                entry = f"    - {method} {path}".strip()
                lines.append(entry)
                details: list[str] = []
                if route.handler:
                    details.append(f"handler={route.handler}")
                if route.file:
                    details.append(f"file={route.file}")
                if route.confidence is not None:
                    details.append(f"confidence={route.confidence:.2f}")
                if route.status:
                    details.append(f"status={route.status}")
                if details:
                    lines.append(f"      ({', '.join(details)})")
    else:
        lines.append("No routes discovered yet")

    if result.notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in result.notes)

    return "\n".join(lines)
