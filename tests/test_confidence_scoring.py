"""Tests for conservative trace confidence and test-matrix coverage scoring."""

from sydes.core.confidence import compute_test_matrix_coverage, compute_trace_confidence
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


def test_cross_repo_external_api_grounding_has_high_trace_confidence() -> None:
    """Strong cross-repo proxy traces should stay in high-confidence band."""
    endpoint = EndpointCandidate(
        method="GET",
        path="/goodreads/books",
        handler="getBooks",
        file="src/Service2Application.java",
        repo="service2",
        confidence=0.9,
    )
    flow = FlowExpansionResult(
        steps=[
            TraceStep(
                kind="handler",
                name="getBooks calls downstream service",
                repo="service2",
                file="src/Service2Application.java",
                symbol="getBooks",
                evidence=[EvidenceRef(file="src/Service2Application.java", symbol="getBooks", label="handler")],
            ),
            TraceStep(
                kind="external_api_call",
                name='WebClient uri("/db/books")',
                repo="service2",
                file="src/Service2Application.java",
                evidence=[EvidenceRef(file="src/Service2Application.java", label="webclient-call")],
            ),
        ],
        confidence=0.9,
    )

    score = compute_trace_confidence(
        selected_endpoint=endpoint,
        flow_expansion=flow,
        nodes_count=5,
        edges_count=6,
    )
    assert score >= 0.88


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


def test_specific_sink_and_link_matrix_cases_increase_matrix_coverage() -> None:
    """Specific downstream/sink-aware matrix cases should cover more risk surfaces."""
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
            MatrixGroupModel(
                category="data_shape",
                tests=[IntegrationTestSuggestion(name="get_goodreads_books_downstream_empty_payload_handled", route="/goodreads/books", method="GET")],
            ),
            MatrixGroupModel(
                category="cross_service_contract",
                tests=[IntegrationTestSuggestion(name="get_goodreads_books_cross_service_contract_compatible", route="/goodreads/books", method="GET")],
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

    specific_score = compute_test_matrix_coverage(specific_trace, specific_matrix)
    generic_score = compute_test_matrix_coverage(generic_trace, generic_matrix)

    assert specific_score is not None and generic_score is not None
    assert specific_score > generic_score


def test_full_detected_surface_coverage_can_reach_one() -> None:
    """Coverage should be 1.00 when all detected surfaces are represented."""
    trace = TraceResult(
        target=TargetSpec(path="/goodreads/books", method="GET"),
        nodes=[
            GraphNode(id="src", type="api_endpoint", name="/goodreads/books", repo="service2"),
            GraphNode(id="sink", type="external_api", name="service1 /db/books", metadata={"action": "read"}, repo="service2"),
        ],
        edges=[GraphEdge(id="e1", source="src", target="src", type="CALLS_API")],
        summary=TraceSummary(confidence=0.92, trace_confidence=0.92),
    )
    matrix = MatrixModel(
        groups=[
            MatrixGroupModel(category="happy_path", tests=[IntegrationTestSuggestion(name="get_goodreads_books_proxies_downstream_response", route="/goodreads/books", method="GET")]),
            MatrixGroupModel(category="downstream_failure", tests=[IntegrationTestSuggestion(name="get_goodreads_books_downstream_timeout", route="/goodreads/books", method="GET")]),
            MatrixGroupModel(category="data_shape", tests=[IntegrationTestSuggestion(name="get_goodreads_books_downstream_empty_payload_handled", route="/goodreads/books", method="GET"), IntegrationTestSuggestion(name="get_goodreads_books_returns_expected_response_shape", route="/goodreads/books", method="GET")]),
            MatrixGroupModel(category="cross_service_contract", tests=[IntegrationTestSuggestion(name="get_goodreads_books_cross_service_contract_compatible", route="/goodreads/books", method="GET")]),
        ]
    )
    score = compute_test_matrix_coverage(trace, matrix)
    assert score == 1.0


def test_missing_one_detected_surface_lowers_coverage() -> None:
    """Coverage should drop when one detected risk surface is not represented."""
    trace = TraceResult(
        target=TargetSpec(path="/goodreads/books", method="GET"),
        nodes=[
            GraphNode(id="src", type="api_endpoint", name="/goodreads/books", repo="service2"),
            GraphNode(id="sink", type="external_api", name="service1 /db/books", metadata={"action": "read"}, repo="service2"),
        ],
        edges=[GraphEdge(id="e1", source="src", target="src", type="CALLS_API")],
        summary=TraceSummary(confidence=0.92, trace_confidence=0.92),
    )
    matrix = MatrixModel(
        groups=[
            MatrixGroupModel(category="happy_path", tests=[IntegrationTestSuggestion(name="get_goodreads_books_proxies_downstream_response", route="/goodreads/books", method="GET")]),
            MatrixGroupModel(category="downstream_failure", tests=[IntegrationTestSuggestion(name="get_goodreads_books_downstream_timeout", route="/goodreads/books", method="GET")]),
            MatrixGroupModel(category="cross_service_contract", tests=[IntegrationTestSuggestion(name="get_goodreads_books_cross_service_contract_compatible", route="/goodreads/books", method="GET")]),
        ]
    )
    score = compute_test_matrix_coverage(trace, matrix)
    assert score is not None
    assert score < 1.0


def test_generic_fallback_matrix_coverage_range() -> None:
    """Generic no-sink matrix should stay in lower fallback coverage range."""
    trace = TraceResult(
        target=TargetSpec(path="/health", method="GET"),
        summary=TraceSummary(confidence=0.9, trace_confidence=0.9),
    )
    matrix = MatrixModel(
        groups=[
            MatrixGroupModel(
                category="happy_path",
                tests=[IntegrationTestSuggestion(name="get_health_returns_entity_or_list", route="/health", method="GET")],
            )
        ]
    )
    score = compute_test_matrix_coverage(trace, matrix)
    assert score is not None
    assert 0.45 <= score <= 0.60


def test_sink_aware_matrix_coverage_range() -> None:
    """Sink-aware matrix should stay in medium-high coverage range."""
    trace = TraceResult(
        target=TargetSpec(path="/users", method="POST"),
        nodes=[GraphNode(id="db", type="database", name="users_db", metadata={"action": "write"})],
        summary=TraceSummary(confidence=0.88, trace_confidence=0.88),
    )
    matrix = MatrixModel(
        groups=[
            MatrixGroupModel(
                category="side_effects",
                tests=[IntegrationTestSuggestion(name="post_users_writes_to_database", route="/users", method="POST")],
            )
        ]
    )
    score = compute_test_matrix_coverage(trace, matrix)
    assert score is not None
    assert 0.75 <= score <= 0.95


def test_sink_and_cross_repo_matrix_coverage_high_when_fully_covered() -> None:
    """Sink+cross-repo+shape coverage should be high when surfaces are all represented."""
    trace = TraceResult(
        target=TargetSpec(path="/goodreads/books", method="GET"),
        nodes=[
            GraphNode(id="s", type="api_endpoint", name="/goodreads/books", repo="service2"),
            GraphNode(id="x", type="external_api", name="service1 /db/books", metadata={"action": "read"}, repo="service2"),
        ],
        edges=[GraphEdge(id="e1", source="s", target="s", type="CALLS_API")],
        summary=TraceSummary(confidence=0.88, trace_confidence=0.88),
    )
    matrix = MatrixModel(
        groups=[
            MatrixGroupModel(
                category="happy_path",
                tests=[IntegrationTestSuggestion(name="get_goodreads_books_proxies_downstream_response", route="/goodreads/books", method="GET")],
            ),
            MatrixGroupModel(
                category="cross_service_contract",
                tests=[IntegrationTestSuggestion(name="get_goodreads_books_cross_service_contract_compatible", route="/goodreads/books", method="GET")],
            ),
            MatrixGroupModel(
                category="downstream_failure",
                tests=[IntegrationTestSuggestion(name="get_goodreads_books_downstream_unavailable", route="/goodreads/books", method="GET")],
            ),
            MatrixGroupModel(
                category="data_shape",
                tests=[
                    IntegrationTestSuggestion(name="get_goodreads_books_returns_expected_response_shape", route="/goodreads/books", method="GET"),
                    IntegrationTestSuggestion(name="get_goodreads_books_downstream_empty_payload_handled", route="/goodreads/books", method="GET"),
                ],
            ),
        ]
    )
    score = compute_test_matrix_coverage(trace, matrix)
    assert score is not None
    assert 0.95 <= score <= 1.0
