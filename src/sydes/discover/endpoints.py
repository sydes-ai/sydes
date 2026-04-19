"""Endpoint discovery placeholders for framework-specific parser integration."""

from sydes.core.models import EndpointCandidate, RepoRef


def discover_endpoints(repos: list[RepoRef]) -> list[EndpointCandidate]:
    """Return discovered endpoint candidates for the provided repositories.

    V1 placeholder behavior: returns no endpoints until parser-based discovery is added.
    """
    _ = repos
    return []
