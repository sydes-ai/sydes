"""Tests for conservative trace and test-matrix confidence scoring."""

from sydes.core.confidence import compute_test_matrix_confidence, compute_trace_confidence
from sydes.core.models import (
    EndpointCandidate,
    EvidenceRef,
    FlowExpansionResult,
    GraphEdge,
    GraphNode,
    TestMatrix as MatrixModel,
    TestMatrixGroup as MatrixGroupModel,
    IntegrationTestSuggestion,
    TraceResult,
    TraceSummary,
    TraceStep,
    TargetSpec,
)


def test_trace_confidence_is_higher_with_grounded_endpoint_and_steps() -> None:
    """Exact endpoint match + file/handler/symbol evidence should score high."""
    endpoint = EndpointCandidate(
        method="GET",
        path="/books/{id}",
        handler="get_book",
        file="src/routes.py",
        repo="api",
        confidence=0.9,
    )
    flow = FlowExpansionResult(
        steps=[
            TraceStep(
                kind="handler",
                name="get_book",
                file="src/routes.py",
                repo="api",
                symbol="get_book",
                evidence=[EvidenceRef(file="src/routes.py", symbol="get_book", label="handler")],
            )
        ]
    )

    score = compute_trace_confidence(
        selected_endpoint=endpoint,
        flow_expansion=flow,
        nodes_count=2,
        edges_count=1,
    )
    assert score >= 0.8


def test_cross_repo_link_increases_trace_confidence() -> None:
    """Cross-repo linkage evidence should increase trace confidence."""
    endpoint = EndpointCandidate(method="GET", path="/books", file="src/routes.py", repo="service2", confidence=0.8)
    flow = FlowExpansionResult(steps=[TraceStep(kind="step", name="call downstream", repo="service2", file="src/client.py")])

    no_link = compute_trace_confidence(
        selected_endpoint=endpoint,
        flow_expansion=flow,
        nodes_count=3,
        edges_count=2,
    )
    with_link = compute_trace_confidence(
        selected_endpoint=endpoint,
        flow_expansion=flow,
        nodes_count=3,
        edges_count=4,
    )
    assert with_link > no_link


def test_inferred_only_flow_reduces_trace_confidence() -> None:
    """Weak inferred-only steps should lower confidence compared with grounded steps."""
    endpoint = EndpointCandidate(method="POST", path="/users", file="src/routes.py", repo="api", confidence=0.8)
    weak = FlowExpansionResult(steps=[TraceStep(kind="step", name="process", status="inferred")])
    strong = FlowExpansionResult(
        steps=[TraceStep(kind="step", name="db.add", repo="api", file="src/repo.py", symbol="add")]
    )

    weak_score = compute_trace_confidence(
        selected_endpoint=endpoint,
        flow_expansion=weak,
        nodes_count=2,
        edges_count=1,
    )
    strong_score = compute_trace_confidence(
        selected_endpoint=endpoint,
        flow_expansion=strong,
        nodes_count=2,
        edges_count=1,
    )
    assert weak_score < strong_score


def test_specific_sink_and_link_matrix_cases_increase_matrix_confidence() -> None:
    """Specific downstream/sink-aware matrix cases should score above generic fallback."""
    specific_trace = TraceResult(
        target=TargetSpec(path="/goodreads/books", method="GET"),
        nodes=[
            GraphNode(id="s", type="api_endpoint", name="/goodreads/books", repo="service2"),
            GraphNode(id="x", type="external_api", name="service1 /db/books", metadata={"action": "read"}, repo="service2"),
        ],
        edges=[GraphEdge(id="e", source="s", target="s", type="CALLS_API")],
        summary=TraceSummary(confidence=0.9, trace_confidence=0.9),
    )
    specific_matrix = MatrixModel(
        groups=[
            MatrixGroupModel(
                category="happy_path",
                tests=[
                    IntegrationTestSuggestion(
                        name="get_goodreads_books_proxies_downstream_response",
                        route="/goodreads/books",
                        method="GET",
                    )
                ],
            ),
            MatrixGroupModel(
                category="downstream_failure",
                tests=[IntegrationTestSuggestion(name="get_goodreads_books_downstream_timeout", route="/goodreads/books", method="GET")],
            ),
        ]
    )

    generic_trace = TraceResult(
        target=TargetSpec(path="/status", method="GET"),
        summary=TraceSummary(confidence=0.6, trace_confidence=0.6),
    )
    generic_matrix = MatrixModel(
        groups=[
            MatrixGroupModel(
                category="happy_path",
                tests=[IntegrationTestSuggestion(name="get_status_returns_entity_or_list", route="/status", method="GET")],
            )
        ]
    )

    specific_score = compute_test_matrix_confidence(specific_trace, specific_matrix)
    generic_score = compute_test_matrix_confidence(generic_trace, generic_matrix)

    assert specific_score is not None and generic_score is not None
    assert specific_score > generic_score
