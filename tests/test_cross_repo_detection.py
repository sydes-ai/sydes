"""Tests for cross-repo call-candidate detection from bounded flow context files."""

from sydes.core.models import CandidateFileRead, FlowExpansionContext, ExpansionContextFile, ReadFileSnippet
from sydes.trace.cross_repo import detect_cross_repo_call_candidates


def _context_with_file(path: str, text: str, *, repo: str = "gateway") -> FlowExpansionContext:
    """Build a minimal readable flow context fixture."""
    return FlowExpansionContext(
        anchor_repo=repo,
        anchor_file=path,
        files=[
            ExpansionContextFile(
                repo=repo,
                file=path,
                selection_reasons=["anchor_endpoint_file"],
                read=CandidateFileRead(
                    repo=repo,
                    relative_path=path,
                    snippet=ReadFileSnippet(
                        repo=repo,
                        relative_path=path,
                        text=text,
                        truncated=False,
                        line_count=len(text.splitlines()),
                        char_count=len(text),
                    ),
                ),
            )
        ],
    )


def test_detect_cross_repo_call_candidates_extracts_method_and_path_from_http_call() -> None:
    """HTTP client call forms should produce grounded method/path call candidates."""
    context = _context_with_file(
        "src/users_service.py",
        (
            "def create_user(payload):\n"
            "    response = requests.post('/payments/charge', json=payload)\n"
            "    return response\n"
        ),
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.source_repo == "gateway"
    assert first.source_file == "src/users_service.py"
    assert first.source_symbol == "create_user"
    assert first.target_method == "POST"
    assert first.target_path == "/payments/charge"
    assert first.raw_call_text is not None and "requests.post" in first.raw_call_text
    assert first.evidence and first.evidence[0].label == "http_client_call"


def test_detect_cross_repo_call_candidates_extracts_service_hint_from_url_calls() -> None:
    """Absolute URL calls should preserve route path and infer service hint."""
    context = _context_with_file(
        "src/orders_client.py",
        (
            "def load_orders():\n"
            "    return httpx.get('https://orders.internal/v1/orders')\n"
        ),
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path == "/v1/orders"
    assert first.target_service_hint == "orders"


def test_detect_cross_repo_call_candidates_keeps_partial_path_only_candidates() -> None:
    """Client-context path literals should still produce partial call candidates."""
    context = _context_with_file(
        "src/reservation.py",
        (
            "def submit(payload):\n"
            "    client.request('/inventory/reserve', json=payload)\n"
        ),
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    assert any(item.target_path == "/inventory/reserve" for item in candidates)
    partial = next(item for item in candidates if item.target_path == "/inventory/reserve")
    assert partial.target_method is None
    assert partial.evidence and partial.evidence[0].label == "route_literal_in_client_context"


def test_detect_cross_repo_call_candidates_dedupes_repeated_same_call_shape() -> None:
    """Repeated identical calls in one file should collapse to one candidate."""
    context = _context_with_file(
        "src/payments.py",
        (
            "def sync_payments(payload):\n"
            "    requests.post('/payments/charge', json=payload)\n"
            "    requests.post('/payments/charge', json=payload)\n"
        ),
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert len(candidates) == 1
    assert candidates[0].target_method == "POST"
    assert candidates[0].target_path == "/payments/charge"
