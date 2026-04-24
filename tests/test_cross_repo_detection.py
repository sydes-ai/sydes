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
    assert first.evidence and first.evidence[0].label.startswith("chain_extraction:")


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


def test_detect_cross_repo_call_candidates_normalizes_webclient_uri_chain() -> None:
    """WebClient-style get().uri('/path') chains should keep normalized method/path."""
    context = _context_with_file(
        "src/books_client.py",
        (
            "def fetch_books():\n"
            "    return client.get()\n"
            "        .uri(\"/db/books\")\n"
            "        .retrieve()\n"
            "        .bodyToFlux(Book.class)\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path == "/db/books"
    assert first.normalized_target_method == "GET"
    assert first.normalized_target_path == "/db/books"
    assert first.raw_call_text is not None and "uri(\"/db/books\")" in first.raw_call_text
    assert first.status == "extracted_from_chain"
    assert first.evidence and first.evidence[0].label.startswith("multiline_chain:")


def test_detect_cross_repo_call_candidates_extracts_post_uri_chain() -> None:
    """POST chained call with uri(...) should produce strong method+path candidate."""
    context = _context_with_file(
        "src/payments_client.py",
        (
            "def charge(payload):\n"
            "    return webClient.post()\n"
            "        .uri('/charge')\n"
            "        .retrieve()\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "POST"
    assert first.target_path == "/charge"
    assert first.status == "extracted_from_chain"


def test_detect_cross_repo_call_candidates_keeps_method_only_chain_as_partial() -> None:
    """Method-only chained call without uri(...) should remain partial."""
    context = _context_with_file(
        "src/books_client.py",
        (
            "def fetch_books():\n"
            "    return client.get()\n"
            "        .retrieve()\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path is None
    assert first.status == "partial"
    assert first.evidence and first.evidence[0].label.startswith("multiline_chain_partial:")


def test_detect_cross_repo_call_candidates_extracts_uri_with_inner_spacing() -> None:
    """Spacing inside uri(...) should still normalize to the same path."""
    context = _context_with_file(
        "src/books_client.py",
        (
            "def fetch_books():\n"
            "    return client.get().uri( \"/db/books\" ).retrieve().bodyToFlux(Book.class)\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path == "/db/books"
    assert first.normalized_target_path == "/db/books"


def test_detect_cross_repo_call_candidates_single_line_chain_still_works() -> None:
    """Single-line client chain extraction should remain supported."""
    context = _context_with_file(
        "src/books_client.py",
        (
            "def fetch_books():\n"
            "    return client.get().uri('/db/books').retrieve()\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path == "/db/books"
    assert first.evidence and first.evidence[0].label.startswith("chain_extraction:")


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
    assert partial.evidence and partial.evidence[0].label.startswith("partial_extraction:")


def test_detect_cross_repo_call_candidates_does_not_merge_unrelated_neighbor_lines() -> None:
    """Unrelated adjacent lines should not be grouped into one chain candidate."""
    context = _context_with_file(
        "src/books_client.py",
        (
            "def fetch_books():\n"
            "    return client.get()\n"
            "    logger.info('not part of chain')\n"
            "    .uri('/db/books')\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path is None
    assert first.status == "partial"
    assert first.raw_call_text is not None and "logger.info" not in first.raw_call_text


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


def test_detect_cross_repo_call_candidates_ignores_python_route_decorator() -> None:
    """Route decorators should not be treated as outbound API calls."""
    context = _context_with_file(
        "src/main.py",
        (
            "@app.get('/users/')\n"
            "def list_users():\n"
            "    return []\n"
        ),
        repo="api",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates == []


def test_detect_cross_repo_call_candidates_ignores_router_route_decorator() -> None:
    """Router decorator declarations should not produce cross-repo candidates."""
    context = _context_with_file(
        "src/routes.py",
        (
            "@router.post('/users/')\n"
            "def create_user(payload):\n"
            "    return payload\n"
        ),
        repo="api",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates == []


def test_detect_cross_repo_call_candidates_ignores_spring_route_annotations() -> None:
    """Spring mapping annotations should not be treated as outbound HTTP calls."""
    context = _context_with_file(
        "src/BooksController.java",
        (
            "@GetMapping(\"/db/books\")\n"
            "public Flux<Book> listBooks() {\n"
            "  return service.list();\n"
            "}\n"
        ),
        repo="service1",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates == []


def test_detect_cross_repo_call_candidates_extracts_requests_get_call() -> None:
    """Real outbound requests.get call should still produce candidate."""
    context = _context_with_file(
        "src/client.py",
        (
            "def fetch_users():\n"
            "    return requests.get('/users')\n"
        ),
        repo="gateway",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path == "/users"


def test_detect_cross_repo_call_candidates_extracts_httpx_post_call() -> None:
    """Real outbound httpx.post call should still produce candidate."""
    context = _context_with_file(
        "src/client.py",
        (
            "def create_user(payload):\n"
            "    return httpx.post('/users', json=payload)\n"
        ),
        repo="gateway",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "POST"
    assert first.target_path == "/users"


def test_detect_cross_repo_call_candidates_fastapi_declarations_regression() -> None:
    """Single-repo FastAPI route declarations should yield zero call candidates."""
    context = _context_with_file(
        "src/main.py",
        (
            "@app.get('/users/')\n"
            "def list_users():\n"
            "    return []\n\n"
            "@app.post('/users/')\n"
            "def create_user(payload):\n"
            "    return payload\n\n"
            "@app.put('/users/{user_id}')\n"
            "def update_user(user_id, payload):\n"
            "    return payload\n\n"
            "@app.delete('/users/{user_id}')\n"
            "def delete_user(user_id):\n"
            "    return {'ok': True}\n"
        ),
        repo="api",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates == []


def test_detect_cross_repo_call_candidates_multiline_java_webclient_chain_regression() -> None:
    """Multiline Java WebClient chains should still extract method/path."""
    context = _context_with_file(
        "src/BooksClient.java",
        (
            "public Flux<Book> listBooks() {\n"
            "  return webClient.get()\n"
            "    .uri(\"/db/books\")\n"
            "    .retrieve()\n"
            "    .bodyToFlux(Book.class);\n"
            "}\n"
        ),
        repo="service2",
    )

    candidates = detect_cross_repo_call_candidates(context)

    assert candidates
    first = candidates[0]
    assert first.target_method == "GET"
    assert first.target_path == "/db/books"
