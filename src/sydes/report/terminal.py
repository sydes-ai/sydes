"""Terminal rendering for human-readable trace summaries."""

from sydes.core.models import TraceResult


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
