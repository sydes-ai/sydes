"""Tests for the first end-to-end endpoint discovery pipeline behavior."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from sydes.core.models import EndpointCandidate, RepoRef
from sydes.discover.endpoints import (
    _select_route_discovery_llm_candidates,
    discover_endpoints,
    is_high_quality_route_evidence,
    merge_route_candidates,
    run_llm_endpoint_discovery,
)
from sydes.llm.client import LLMRequest, LLMResponse
from sydes.llm.client import LLMClientError


@dataclass
class _FakeEndpointClient:
    """Fake client returning deterministic endpoint extraction output."""

    payload: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "Task: extract likely HTTP API route declarations" in request.prompt
        return LLMResponse(text=self.payload)


class _FailingEndpointClient:
    """Fake client that fails to simulate strict-mode parse/runtime errors."""

    def __init__(self, message: str) -> None:
        self.message = message

    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMClientError(self.message)


def test_discover_endpoints_fallback_without_llm(tmp_path: Path) -> None:
    """Pipeline should still return deterministic declarations when LLM is unavailable."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "routes.py").write_text(
        "router.post('/checkout', checkout_handler)\n",
        encoding="utf-8",
    )

    result = discover_endpoints(
        [RepoRef(name="api", root=str(repo_root))],
        llm_client=_FailingEndpointClient("mock unavailable"),
    )

    assert result.repos[0].name == "api"
    assert result.candidate_files >= 1
    assert result.files_examined >= 1
    assert len(result.routes) == 1
    assert result.routes[0].method == "POST"
    assert result.routes[0].path == "/checkout"
    assert result.routes[0].file == "src/routes.py"
    assert any("candidate_roles:" in note for note in result.notes)


def test_discover_endpoints_strict_keeps_deterministic_routes_when_llm_fails(tmp_path: Path) -> None:
    """Strict discovery should still succeed with deterministic routes when LLM parsing/runtime fails."""
    repo_root = tmp_path / "flask"
    repo_root.mkdir()
    (repo_root / "app").mkdir()
    (repo_root / "app" / "routes.py").write_text(
        "\n".join(
            [
                "@app.route('/items', methods=['GET'])",
                "def get_items():",
                "    return []",
            ]
        ),
        encoding="utf-8",
    )

    result = discover_endpoints(
        [RepoRef(name="flask", root=str(repo_root))],
        llm_client=_FailingEndpointClient("model output parse failure: malformed JSON"),
        strict_llm=True,
    )

    assert result.routes
    assert any(item.method == "GET" and item.path == "/items" for item in result.routes)
    assert any("LLM discovery failed; using deterministic routes only" in note for note in result.notes)


def test_discover_endpoints_strict_raises_when_no_deterministic_routes_and_llm_fails(tmp_path: Path) -> None:
    """Strict discovery should fail when both deterministic and LLM extraction are unavailable."""
    repo_root = tmp_path / "empty"
    repo_root.mkdir()
    (repo_root / "README.md").write_text("# no routes here\n", encoding="utf-8")

    with pytest.raises(LLMClientError, match="model output parse failure"):
        discover_endpoints(
            [RepoRef(name="empty", root=str(repo_root))],
            llm_client=_FailingEndpointClient("model output parse failure: malformed JSON"),
            strict_llm=True,
        )


def test_discover_endpoints_deterministic_scans_full_file_not_discovery_snippet(tmp_path: Path) -> None:
    """Deterministic extraction should read full source files and not inherit discovery snippet truncation."""
    repo_root = tmp_path / "stress-fastapi-100"
    repo_root.mkdir()
    lines = ["from fastapi import FastAPI", "", "app = FastAPI()", ""]
    for index in range(1, 101):
        lines.extend(
            [
                f"@app.get('/api/v1/resource{index}')",
                f"def resource_{index}():",
                f"    return {{'id': {index}}}",
                "",
            ]
        )
    (repo_root / "main.py").write_text("\n".join(lines), encoding="utf-8")

    result = discover_endpoints(
        [RepoRef(name="stress", root=str(repo_root))],
        llm_client=_FailingEndpointClient("model output parse failure: malformed JSON"),
        strict_llm=True,
    )

    routes = {(item.method, item.path, item.file) for item in result.routes}
    assert len(result.routes) == 100
    assert ("GET", "/api/v1/resource1", "main.py") in routes
    assert ("GET", "/api/v1/resource100", "main.py") in routes
    assert any("deterministic_routes_found=100" in note for note in result.notes)
    assert any("deterministic_files_scanned=1" in note for note in result.notes)
    assert any("deterministic_scan_truncated_files=0" in note for note in result.notes)


def test_run_llm_endpoint_discovery_normalizes_and_dedupes() -> None:
    """LLM discovery should normalize soft outputs and dedupe obvious duplicates."""
    client = _FakeEndpointClient(
        payload=(
            '{"endpoints": ['
            '{"method":"post","path":"/checkout","handler":"checkout","file":"src/routes.py","repo":"api","confidence":0.7,"evidence":[{"file":"src/routes.py","symbol":"checkout","label":"route-call"}]},'
            '{"method":"POST","path":"/checkout","handler":"checkout","file":"src/routes.py","repo":"api","confidence":0.5,"evidence":[{"file":"src/routes.py","symbol":"checkout","label":"duplicate"}]},'
            '{"path":"/status","file":"src/routes.py","repo":"api","evidence":[{"file":"src/routes.py","label":"partial"}]}'
            '], "notes":["model-note"]}'
        )
    )

    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.post('/checkout', checkout)",
                line_count=1,
                char_count=34,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    first = next(item for item in result.endpoints if item.path == "/checkout")
    assert first.method == "POST"
    assert first.confidence == 0.7
    assert len(first.evidence) >= 2
    assert "model-note" in result.notes
    assert any("Dropped endpoint" in note for note in result.notes)


def test_run_llm_endpoint_discovery_accepts_markdown_fenced_json() -> None:
    """Discovery parser should handle markdown-fenced JSON from local models."""
    client = _FakeEndpointClient(
        payload=(
            "```json\n"
            '{"endpoints":[{"method":"GET","path":"/status","file":"src/routes.py","repo":"api"}]}\n'
            "```"
        )
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.get('/status', status)",
                line_count=1,
                char_count=29,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].path == "/status"


def test_run_llm_endpoint_discovery_accepts_top_level_list() -> None:
    """Discovery parser should accept top-level list payloads."""
    client = _FakeEndpointClient(
        payload='[{"method":"GET","path":"/health","file":"app.py","repo":"api"}]'
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="app.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="app.py",
                text="@app.get('/health')",
                line_count=1,
                char_count=18,
            ),
        )
    ]
    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].method == "GET"
    assert result.endpoints[0].path == "/health"


def test_run_llm_endpoint_discovery_normalizes_fields_and_filters_weak_candidates() -> None:
    """Discovery should normalize method/path and drop weak ungrounded partials."""
    client = _FakeEndpointClient(
        payload=(
            '{"endpoints":['
            '{"method":" post ","path":"checkout/","handler":" checkout_handler ","file":"src\\\\routes.py","repo":" api ","evidence":[{"file":"src/routes.py","label":"route"}]},'
            '{"file":"src/unknown.py","repo":"api","evidence":[{"file":"src/unknown.py","label":"maybe"}]}'
            ']}'
        )
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.post('/checkout', checkout_handler)",
                line_count=1,
                char_count=42,
            ),
        )
    ]
    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    endpoint = result.endpoints[0]
    assert endpoint.method == "POST"
    assert endpoint.path == "/checkout"
    assert endpoint.handler == "checkout_handler"
    assert endpoint.file == "src/routes.py"
    assert endpoint.repo == "api"
    assert any("Dropped endpoint" in note for note in result.notes)


def test_run_llm_endpoint_discovery_drops_weak_root_candidate() -> None:
    """Weak candidates like missing-method root routes should be filtered."""
    client = _FakeEndpointClient(
        payload=(
            '{"endpoints":['
            '{"path":"/","file":"src/routes.py","repo":"api","evidence":[{"file":"src/routes.py","label":"maybe"}]},'
            '{"method":"GET","path":"/users","handler":"list_users","file":"src/routes.py","repo":"api"}'
            ']}'
        )
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.get('/users', list_users)",
                line_count=1,
                char_count=32,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].path == "/users"
    assert any("root path with missing method" in note for note in result.notes)


def test_run_llm_endpoint_discovery_keeps_strong_evidence_partial_candidate() -> None:
    """Partial endpoints without path/handler may be kept with strong evidence."""
    client = _FakeEndpointClient(
        payload=(
            '{"endpoints":['
            '{"file":"src/routes.py","repo":"api","confidence":0.9,"evidence":[{"file":"src/routes.py","label":"route registration"}]}'
            ']}'
        )
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                text="router.use(routes)",
                line_count=1,
                char_count=18,
            ),
        )
    ]
    result = run_llm_endpoint_discovery(candidates, llm_client=client)

    assert len(result.endpoints) == 1
    assert result.endpoints[0].path is None
    assert result.endpoints[0].handler is None


def test_run_llm_endpoint_discovery_rejects_test_file_route_declaration() -> None:
    """Routes declared from test files should be rejected post-LLM."""
    client = _FakeEndpointClient(
        payload='{"endpoints":[{"method":"GET","path":"/items/0","file":"tests/test_app.py","repo":"api"}]}'
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="tests/test_app.py",
            role="test_usage_candidate",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="tests/test_app.py",
                text="response = client.get('/items/0')",
                line_count=1,
                char_count=33,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)
    assert result.endpoints == []
    assert any("route_declared_in_test_file" in note for note in result.notes)


def test_run_llm_endpoint_discovery_rejects_invocation_evidence() -> None:
    """Invocation-like evidence should be rejected as non-declaration route sources."""
    client = _FakeEndpointClient(
        payload=(
            '{"endpoints":[{"method":"GET","path":"/items/0","file":"src/client_calls.py","repo":"api",'
            '"evidence":[{"file":"src/client_calls.py","label":"client.get(\\"/items/0\\")"}]}]}'
        )
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/client_calls.py",
            role="source_route_candidate",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/client_calls.py",
                text='client.get("/items/0")',
                line_count=1,
                char_count=22,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)
    assert result.endpoints == []
    assert any("route_invocation_not_declaration" in note for note in result.notes)


def test_run_llm_endpoint_discovery_accepts_declaration_evidence_for_source_file() -> None:
    """Declaration-like evidence in source files should remain accepted."""
    client = _FakeEndpointClient(
        payload=(
            '{"endpoints":['
            '{"method":"POST","path":"/items","file":"app/routes.py","repo":"api",'
            '"evidence":[{"file":"app/routes.py","label":"@app.route(\\"/items\\", methods=[\\"POST\\"])"},'
            '{"file":"app/routes.py","label":"router.post(\\"/items\\", handler)"}]}'
            ']}'
        )
    )
    from sydes.core.models import CandidateFileRead, ReadFileSnippet

    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="app/routes.py",
            role="source_route_candidate",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="app/routes.py",
                text='@app.route("/items", methods=["POST"])',
                line_count=1,
                char_count=37,
            ),
        )
    ]

    result = run_llm_endpoint_discovery(candidates, llm_client=client)
    assert len(result.endpoints) == 1
    assert result.endpoints[0].path == "/items"


def test_select_route_discovery_candidates_excludes_tests_when_source_exists() -> None:
    """Route-declaration selection should avoid test/docs when source candidates exist."""
    from sydes.core.models import CandidateFileRead

    reads = [
        CandidateFileRead(repo="api", relative_path="app/routes.py", role="source_route_candidate"),
        CandidateFileRead(repo="api", relative_path="main.py", role="source_route_candidate"),
        CandidateFileRead(repo="api", relative_path="tests/test_app.py", role="test_usage_candidate"),
        CandidateFileRead(repo="api", relative_path="README.md", role="docs_candidate"),
    ]
    selected = _select_route_discovery_llm_candidates(reads, files_to_llm=3)
    selected_paths = [item.relative_path for item in selected]
    assert "app/routes.py" in selected_paths
    assert "main.py" in selected_paths
    assert "tests/test_app.py" not in selected_paths
    assert "README.md" not in selected_paths


def test_select_route_discovery_candidates_returns_none_for_test_docs_only() -> None:
    """Test/docs-only repositories should not send declaration candidates in this phase."""
    from sydes.core.models import CandidateFileRead

    reads = [
        CandidateFileRead(repo="api", relative_path="tests/test_app.py", role="test_usage_candidate"),
        CandidateFileRead(repo="api", relative_path="README.md", role="docs_candidate"),
    ]
    selected = _select_route_discovery_llm_candidates(reads, files_to_llm=5)
    assert selected == []


def test_merge_route_candidates_prefers_deterministic_and_dedupes_identity() -> None:
    """Same repo+method+path should merge into one canonical deterministic route."""
    det = EndpointCandidate(
        method="GET",
        path="/users/",
        handler="get_users",
        file="app/routes.py",
        repo="api",
        confidence=1.0,
        status="deterministic",
        evidence=[{"file": "app/routes.py", "label": "@app.get('/users/')"}],
    )
    llm = EndpointCandidate(
        method="get",
        path="/users",
        handler="users_handler",
        file="tests/test_app.py",
        repo="api",
        confidence=0.5,
        status="inferred",
        evidence=[{"file": "tests/test_app.py", "label": "client.get('/users')"}],
    )
    merged, notes = merge_route_candidates([det], [llm])
    assert len(merged) == 1
    assert merged[0].file == "app/routes.py"
    assert merged[0].handler == "get_users"
    assert merged[0].path == "/users"
    assert merged[0].method == "GET"
    assert notes


def test_merge_route_candidates_normalizes_parameter_syntax_and_keeps_one() -> None:
    """Flask/FastAPI/Express param syntax variants should collapse to one identity."""
    det = EndpointCandidate(
        method="GET",
        path="/items/{item_id}",
        handler="get_item",
        file="app/routes.py",
        repo="api",
        confidence=1.0,
        status="deterministic",
    )
    llm_flask = EndpointCandidate(
        method="GET",
        path="/items/<int:item_id>",
        handler="getItem",
        file="app/routes.py",
        repo="api",
        confidence=0.7,
        status="inferred",
    )
    llm_express = EndpointCandidate(
        method="GET",
        path="/items/:item_id",
        handler="getItem",
        file="src/routes.ts",
        repo="api",
        confidence=0.7,
        status="inferred",
    )
    merged, _ = merge_route_candidates([det], [llm_flask, llm_express])
    assert len(merged) == 1
    assert merged[0].path == "/items/{item_id}"


def test_merge_route_candidates_keeps_llm_only_valid_source_route() -> None:
    """LLM-only source route should be preserved when deterministic route is absent."""
    llm = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="checkout",
        file="src/routes.py",
        repo="api",
        confidence=0.8,
        status="inferred",
    )
    merged, _ = merge_route_candidates([], [llm])
    assert len(merged) == 1
    assert merged[0].file == "src/routes.py"
    assert merged[0].method == "POST"
    assert merged[0].path == "/checkout"


def test_merge_route_candidates_prefers_high_quality_deterministic_evidence() -> None:
    """Weak LLM evidence should not replace deterministic declaration evidence."""
    det = EndpointCandidate(
        method="POST",
        path="/items",
        handler="add_item",
        file="app/routes.py",
        repo="api",
        confidence=1.0,
        status="deterministic",
        evidence=[
            {
                "file": "app/routes.py",
                "symbol": "add_item",
                "label": "@bp.route('/items', methods=['POST'])",
                "snippet": "@bp.route('/items', methods=['POST'])\ndef add_item():",
            }
        ],
    )
    llm = EndpointCandidate(
        method="POST",
        path="/items/",
        handler="create_item",
        file="app/routes.py",
        repo="api",
        confidence=0.7,
        status="inferred",
        evidence=[
            {
                "file": "app/routes.py",
                "label": "# app/routes.py",
                "snippet": None,
            }
        ],
    )
    merged, _ = merge_route_candidates([det], [llm])
    assert len(merged) == 1
    assert merged[0].evidence
    assert is_high_quality_route_evidence(merged[0].evidence[0])
    assert "# app/routes.py" not in (merged[0].evidence[0].label or "")
