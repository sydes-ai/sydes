"""Tests for LLM-facing endpoint discovery contract scaffolding."""

from dataclasses import dataclass

from sydes.core.models import CandidateFileRead, ReadFileSnippet
from sydes.discover.endpoints import discover_endpoints_from_candidates
from sydes.llm.client import LLMRequest, LLMResponse
from sydes.llm.prompts import build_endpoint_discovery_prompt


def test_build_endpoint_discovery_prompt_encodes_rules_and_grounding() -> None:
    """Prompt should include uncertainty/grounding instructions and candidate content."""
    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            snippet=ReadFileSnippet(
                repo="api",
                relative_path="src/routes.py",
                truncated=False,
                text="router.post('/checkout', checkout_handler)",
                line_count=1,
                char_count=40,
            ),
        )
    ]

    prompt = build_endpoint_discovery_prompt(
        candidates,
        target_hint="/checkout",
        method_hint="POST",
    )

    assert "Only use evidence present" in prompt
    assert "set it to null instead of guessing" in prompt
    assert "Return JSON only" in prompt
    assert '"target_hint": "/checkout"' in prompt
    assert "src/routes.py" in prompt


@dataclass
class _FakeClient:
    """Simple fake LLM client for discovery contract tests."""

    call_count: int = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        assert "Response format:" in request.prompt
        return LLMResponse(text='{"endpoints":[],"notes":[]}')


def test_discover_endpoints_from_candidates_calls_client_when_provided() -> None:
    """Discovery contract should call the provider and return placeholder output."""
    candidates = [
        CandidateFileRead(
            repo="api",
            relative_path="src/routes.py",
            skipped=True,
            skip_reason="file_too_large",
        )
    ]
    client = _FakeClient()

    result = discover_endpoints_from_candidates(candidates, llm_client=client)

    assert client.call_count == 1
    assert result == []
