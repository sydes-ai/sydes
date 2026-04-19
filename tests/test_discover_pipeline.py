"""Tests for the first end-to-end endpoint discovery pipeline behavior."""

from dataclasses import dataclass
from pathlib import Path

from sydes.core.models import RepoRef
from sydes.discover.endpoints import discover_endpoints, run_llm_endpoint_discovery
from sydes.llm.client import LLMRequest, LLMResponse


@dataclass
class _FakeEndpointClient:
    """Fake client returning deterministic endpoint extraction output."""

    payload: str

    def generate(self, request: LLMRequest) -> LLMResponse:
        assert "extracting likely HTTP API endpoint candidates" in request.prompt
        return LLMResponse(text=self.payload)


def test_discover_endpoints_fallback_without_llm(tmp_path: Path) -> None:
    """Pipeline should degrade gracefully when no LLM client is configured."""
    repo_root = tmp_path / "api"
    repo_root.mkdir()
    (repo_root / "src").mkdir()
    (repo_root / "src" / "routes.py").write_text(
        "router.post('/checkout', checkout_handler)\n",
        encoding="utf-8",
    )

    result = discover_endpoints([RepoRef(name="api", root=str(repo_root))])

    assert result.repos[0].name == "api"
    assert result.candidate_files >= 1
    assert result.files_examined >= 1
    assert result.routes == []
    assert any("LLM discovery unavailable" in note for note in result.notes)


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

    assert len(result.endpoints) == 2
    first = next(item for item in result.endpoints if item.path == "/checkout")
    assert first.method == "POST"
    assert first.confidence == 0.7
    assert len(first.evidence) >= 2
    assert "model-note" in result.notes


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
