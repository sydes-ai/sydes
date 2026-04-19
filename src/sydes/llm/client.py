"""Provider-neutral LLM client abstraction for discovery workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMRequest:
    """Simple request payload for text generation calls."""

    prompt: str
    system: str | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class LLMResponse:
    """Simple response payload for text generation calls."""

    text: str


class LLMClient(Protocol):
    """Minimal protocol for provider implementations."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        """Generate a text response for a prompt request."""
