"""Terminal rendering for human-readable Sydes summaries."""

from collections import defaultdict

from sydes.core.models import RoutesResult, TraceResult


def render_terminal(result: TraceResult) -> str:
    """Build a minimal terminal summary for a trace result."""
    method = result.target.method or "ANY"
    lines = [
        "Sydes Trace (V1 Placeholder)",
        f"Target: {method} {result.target.path}",
        "Repos:",
    ]

    if result.repos:
        lines.extend(f"  - {repo.name}: {repo.root}" for repo in result.repos)
    else:
        lines.append("  - (none)")

    if result.summary.confidence is not None:
        lines.append(f"Confidence: {result.summary.confidence:.2f}")

    lines.append("No flow discovered yet")
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
        grouped: dict[str, list] = defaultdict(list)
        for route in result.routes:
            grouped[route.repo].append(route)

        lines.append("Discovered routes by repo:")
        for repo_name in sorted(grouped):
            lines.append(f"  {repo_name}:")
            for route in grouped[repo_name]:
                method = route.method or "?"
                path = route.path or "?"
                entry = f"    - {method} {path}".strip()
                lines.append(entry)
                details: list[str] = []
                if route.handler:
                    details.append(f"handler={route.handler}")
                if route.file:
                    details.append(f"file={route.file}")
                if details:
                    lines.append(f"      ({', '.join(details)})")
    else:
        lines.append("No routes discovered yet")

    if result.notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in result.notes)

    return "\n".join(lines)
