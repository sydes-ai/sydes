"""Tests for deterministic cross-repo endpoint indexing and lookup helpers."""

from sydes.core.models import CrossRepoCallCandidate, EndpointCandidate
from sydes.trace.cross_repo import (
    index_discovered_endpoints,
    lookup_candidate_endpoints_by_path,
    lookup_candidate_endpoints_by_service_path,
    resolve_cross_repo_call_targets,
)


def test_lookup_candidate_endpoints_by_path_prefers_method_exact_match() -> None:
    """Method+path lookup should prefer exact method match before path-only fallback."""
    endpoints = [
        EndpointCandidate(method="GET", path="/users", file="src/routes.py", repo="gateway", service="edge"),
        EndpointCandidate(method="POST", path="/users", file="src/users.py", repo="api", service="users"),
    ]
    index = index_discovered_endpoints(endpoints)

    matches = lookup_candidate_endpoints_by_path(index, path="/users", method="post")
    assert len(matches) == 1
    assert matches[0].repo == "api"
    assert matches[0].method == "POST"

    fallback_matches = lookup_candidate_endpoints_by_path(index, path="users")
    assert len(fallback_matches) == 2


def test_lookup_candidate_endpoints_by_service_path_uses_service_hint_when_available() -> None:
    """Service/path lookup should return service-scoped candidates when possible."""
    endpoints = [
        EndpointCandidate(method="POST", path="/orders", file="src/orders.py", repo="orders", service="orders"),
        EndpointCandidate(method="POST", path="/orders", file="src/orders.py", repo="gateway", service="edge"),
    ]
    index = index_discovered_endpoints(endpoints)

    matches = lookup_candidate_endpoints_by_service_path(
        index,
        service_hint="ORDERS",
        path="/orders",
        method="POST",
    )
    assert len(matches) == 1
    assert matches[0].repo == "orders"


def test_resolve_cross_repo_call_targets_prefers_method_path_priority() -> None:
    """Resolver should match by method+path first even with a mismatched service hint."""
    endpoints = [
        EndpointCandidate(method="POST", path="/checkout", file="src/routes.py", repo="api", service="orders")
    ]
    index = index_discovered_endpoints(endpoints)
    call = CrossRepoCallCandidate(
        source_repo="gateway",
        source_file="src/clients.py",
        source_symbol="checkout_client",
        target_path="/checkout",
        target_method="POST",
        target_service_hint="payments",
        raw_call_text="client.post('/checkout')",
    )

    matches, notes = resolve_cross_repo_call_targets(call, index)

    assert len(matches) == 1
    assert matches[0].repo == "api"
    assert not any("no endpoint candidates" in note.lower() for note in notes)
