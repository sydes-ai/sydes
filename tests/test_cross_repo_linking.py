"""Tests for deterministic cross-repo call candidate linking behavior."""

from sydes.core.models import CrossRepoCallCandidate, EndpointCandidate, EvidenceRef
from sydes.trace.cross_repo import index_discovered_endpoints, link_cross_repo_call_candidate


def test_link_cross_repo_call_candidate_exact_method_path_match() -> None:
    """Linker should prefer exact method+path matches."""
    endpoints = [
        EndpointCandidate(
            method="POST",
            path="/checkout",
            file="src/routes.py",
            repo="orders-api",
            service="orders",
            confidence=0.9,
            evidence=[EvidenceRef(file="src/routes.py", symbol="checkout", label="route")],
        ),
        EndpointCandidate(
            method="GET",
            path="/checkout",
            file="src/routes.py",
            repo="orders-api",
            service="orders",
        ),
    ]
    index = index_discovered_endpoints(endpoints)
    call = CrossRepoCallCandidate(
        source_repo="gateway",
        source_file="src/client.py",
        source_symbol="submit_checkout",
        target_method="POST",
        target_path="/checkout",
        raw_call_text="client.post('/checkout')",
        evidence=[EvidenceRef(file="src/client.py", symbol="submit_checkout", label="http_client_call")],
        confidence=0.8,
    )

    links = link_cross_repo_call_candidate(call, index)

    assert len(links) == 1
    assert links[0].matched_target_endpoint_id is not None
    assert links[0].link_type == "exact_method_path"
    assert links[0].confidence is not None
    assert links[0].normalized_target_method == "POST"
    assert links[0].normalized_target_path == "/checkout"
    assert links[0].evidence


def test_link_cross_repo_call_candidate_path_only_fallback() -> None:
    """Linker should fall back to path-only matching when method+path misses."""
    endpoints = [EndpointCandidate(method="GET", path="/users", file="src/users.py", repo="users-api", service="users")]
    index = index_discovered_endpoints(endpoints)
    call = CrossRepoCallCandidate(
        source_repo="gateway",
        source_file="src/users_client.py",
        target_method="PATCH",
        target_path="/users",
        evidence=[EvidenceRef(file="src/users_client.py", label="http_client_call")],
        confidence=0.6,
    )

    links = link_cross_repo_call_candidate(call, index)

    assert len(links) == 1
    assert links[0].matched_target_endpoint_id is not None
    assert links[0].link_type == "path_only"


def test_link_cross_repo_call_candidate_normalizes_method_and_trailing_slash_path() -> None:
    """Linker should normalize method aliases and trailing slash path differences."""
    endpoints = [
        EndpointCandidate(
            method="GET",
            path="/db/books/",
            file="src/routes.py",
            repo="service1",
            service="books",
            confidence=0.8,
        )
    ]
    index = index_discovered_endpoints(endpoints)
    call = CrossRepoCallCandidate(
        source_repo="service2",
        source_file="src/client.py",
        target_method="client.get",
        target_path='"  /db/books  "',
        raw_call_text='client.get().uri("/db/books").retrieve()',
        confidence=0.75,
    )

    links = link_cross_repo_call_candidate(call, index)

    assert len(links) == 1
    assert links[0].matched_target_endpoint_id is not None
    assert links[0].link_type == "exact_method_path"
    assert links[0].normalized_target_method == "GET"
    assert links[0].normalized_target_path == "/db/books"


def test_link_cross_repo_call_candidate_preserves_ambiguity_when_multiple_matches() -> None:
    """Linker should return multiple ambiguous results when no clear winner exists."""
    endpoints = [
        EndpointCandidate(method="GET", path="/orders", file="src/routes.py", repo="orders-v1", service="orders", confidence=0.62),
        EndpointCandidate(method="GET", path="/orders", file="src/routes.py", repo="orders-v2", service="orders", confidence=0.58),
    ]
    index = index_discovered_endpoints(endpoints)
    call = CrossRepoCallCandidate(
        source_repo="gateway",
        source_file="src/orders_client.py",
        target_method="GET",
        target_path="/orders",
        evidence=[EvidenceRef(file="src/orders_client.py", label="http_client_call")],
        confidence=0.7,
    )

    links = link_cross_repo_call_candidate(call, index)

    assert len(links) == 2
    assert all(item.link_type == "exact_method_path" for item in links)
    assert all(any("ambiguous endpoint link" in note.lower() for note in item.notes) for item in links)


def test_link_cross_repo_call_candidate_returns_no_match_result() -> None:
    """Linker should return a no-match result when lookup yields no endpoint."""
    endpoints = [EndpointCandidate(method="GET", path="/status", file="src/routes.py", repo="api", service="status")]
    index = index_discovered_endpoints(endpoints)
    call = CrossRepoCallCandidate(
        source_repo="gateway",
        source_file="src/checkout_client.py",
        target_method="POST",
        target_path="/checkout",
        raw_call_text="checkout_client.post('/checkout')",
        evidence=[EvidenceRef(file="src/checkout_client.py", label="http_client_call")],
        confidence=0.5,
    )

    links = link_cross_repo_call_candidate(call, index)

    assert len(links) == 1
    assert links[0].matched_target_endpoint_id is None
    assert links[0].link_type is None
    assert links[0].normalized_target_method == "POST"
    assert links[0].normalized_target_path == "/checkout"
    assert any("no endpoint candidates matched" in note.lower() for note in links[0].notes)
    assert any("raw call text" in note.lower() for note in links[0].notes)
