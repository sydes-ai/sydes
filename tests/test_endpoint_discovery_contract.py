"""Tests for LLM-facing endpoint discovery contract scaffolding."""

from dataclasses import dataclass

from sydes.core.models import (
    CandidateFileRead,
    EndpointCandidate,
    ExpansionContextFile,
    FlowExpansionContext,
    ReadFileSnippet,
)
from sydes.discover.endpoints import discover_endpoints_from_candidates
from sydes.llm.client import LLMRequest, LLMResponse
from sydes.llm.prompts import build_endpoint_discovery_prompt, build_flow_expansion_prompt
from sydes.trace.expand import build_flow_expansion_prompt_from_context


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

    assert "Task: extract likely HTTP API endpoints" in prompt
    assert "use null" in prompt
    assert "Return JSON only" in prompt
    assert '"target_hint":"/checkout"' in prompt
    assert "src/routes.py" in prompt


@dataclass
class _FakeClient:
    """Simple fake LLM client for discovery contract tests."""

    call_count: int = 0

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.call_count += 1
        assert "Return JSON only" in request.prompt
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


def test_build_flow_expansion_prompt_encodes_rules_and_compact_json_contract() -> None:
    """Flow expansion prompt should enforce grounding and JSON-only output contract."""
    endpoint = EndpointCandidate(
        method="POST",
        path="/checkout",
        handler="create_checkout",
        file="src/routes.py",
        repo="api",
    )
    context = FlowExpansionContext(
        anchor_repo="api",
        anchor_file="src/routes.py",
        files=[
            ExpansionContextFile(
                repo="api",
                file="src/routes.py",
                selection_reasons=["anchor_endpoint_file"],
                read=CandidateFileRead(
                    repo="api",
                    relative_path="src/routes.py",
                    snippet=ReadFileSnippet(
                        repo="api",
                        relative_path="src/routes.py",
                        truncated=False,
                        text="router.post('/checkout', create_checkout)",
                        line_count=1,
                        char_count=42,
                    ),
                ),
            )
        ],
    )

    prompt = build_flow_expansion_prompt(endpoint, context)

    assert "Task: infer one short happy-path flow" in prompt
    assert "Use only provided files" in prompt
    assert "short high-confidence flow" in prompt
    assert "Do not introduce clients/services unless explicitly referenced" in prompt
    assert "omit the step rather than inventing a generic abstraction" in prompt
    assert "Output JSON only" in prompt
    assert '"steps":' in prompt
    assert '"sinks":' in prompt
    assert '"path":"/checkout"' in prompt
    assert "src/routes.py" in prompt


def test_build_flow_expansion_prompt_is_compact_for_small_context() -> None:
    """Flow expansion prompt should stay compact for local model reliability."""
    endpoint = EndpointCandidate(
        method="POST",
        path="/users",
        handler="create_user",
        file="src/routes.py",
        repo="api",
    )
    context = FlowExpansionContext(
        anchor_repo="api",
        anchor_file="src/routes.py",
        files=[
            ExpansionContextFile(
                repo="api",
                file="src/routes.py",
                selection_reasons=["anchor_endpoint_file"],
                read=CandidateFileRead(
                    repo="api",
                    relative_path="src/routes.py",
                    snippet=ReadFileSnippet(
                        repo="api",
                        relative_path="src/routes.py",
                        truncated=False,
                        text=("router.post('/users', create_user)\n" * 30),
                        line_count=30,
                        char_count=900,
                    ),
                ),
            )
        ],
    )

    prompt = build_flow_expansion_prompt(endpoint, context)

    assert len(prompt) < 6000


def test_build_flow_expansion_prompt_from_context_wires_prompt_builder() -> None:
    """Trace expansion helper should delegate prompt construction for prepared context."""
    endpoint = EndpointCandidate(
        method="GET",
        path="/status",
        file="src/routes.py",
        repo="api",
    )
    context = FlowExpansionContext(
        anchor_repo="api",
        anchor_file="src/routes.py",
    )

    prompt = build_flow_expansion_prompt_from_context(endpoint, context)

    assert "happy-path flow" in prompt
    assert '"file":"src/routes.py"' in prompt
