"""Tests for trace target-to-endpoint resolution behavior."""

from sydes.core.models import EndpointCandidate, EvidenceRef
from sydes.discover.target_match import resolve_trace_target


def test_resolve_trace_target_exact_method_and_path_match() -> None:
    """Resolver should select the exact method+path endpoint when unique."""
    endpoints = [
        EndpointCandidate(
            method="POST",
            path="/checkout",
            file="src/routes.py",
            repo="api",
            evidence=[EvidenceRef(file="src/routes.py", label="route")],
            confidence=0.8,
        ),
        EndpointCandidate(
            method="GET",
            path="/checkout",
            file="src/routes.py",
            repo="api",
        ),
    ]

    result = resolve_trace_target(endpoints, path="/checkout", method="POST")

    assert result.selected is not None
    assert result.selected.method == "POST"
    assert result.alternatives == []


def test_resolve_trace_target_returns_alternatives_when_ambiguous() -> None:
    """Resolver should return alternatives for ambiguous path-only matches."""
    endpoints = [
        EndpointCandidate(method="GET", path="/status", file="a.py", repo="svc", confidence=0.4),
        EndpointCandidate(method="POST", path="/status", file="b.py", repo="svc", confidence=0.9),
        EndpointCandidate(method="PUT", path="/other", file="c.py", repo="svc"),
    ]

    result = resolve_trace_target(endpoints, path="/status")

    assert result.selected is not None
    assert result.selected.file == "b.py"
    assert len(result.alternatives) == 1
    assert any("Multiple candidate endpoints matched target" in note for note in result.notes)
