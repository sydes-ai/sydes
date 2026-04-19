"""Endpoint discovery interfaces over bounded candidate file reads."""

from sydes.core.models import CandidateFileRead, EndpointCandidate, RepoRef
from sydes.llm.client import LLMClient, LLMRequest
from sydes.llm.prompts import build_endpoint_discovery_prompt


def discover_endpoints(repos: list[RepoRef]) -> list[EndpointCandidate]:
    """Return discovered endpoint candidates for the provided repositories.

    V1 placeholder behavior: returns no endpoints until parser-based discovery is added.
    """
    _ = repos
    return []


def discover_endpoints_from_candidates(
    candidates: list[CandidateFileRead],
    *,
    llm_client: LLMClient | None = None,
    target_hint: str | None = None,
    method_hint: str | None = None,
) -> list[EndpointCandidate]:
    """Discover endpoint candidates from bounded file reads.

    This phase defines the contract and prompt shape only.
    Provider-backed parsing/validation is added in later phases.
    """
    if llm_client is None:
        return []

    prompt = build_endpoint_discovery_prompt(
        candidates,
        target_hint=target_hint,
        method_hint=method_hint,
    )
    _ = llm_client.generate(LLMRequest(prompt=prompt))
    return []
