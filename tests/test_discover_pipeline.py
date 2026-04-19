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
