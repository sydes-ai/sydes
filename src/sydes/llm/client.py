"""Provider-neutral LLM client abstraction with Ollama implementation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib import error, request
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


class LLMClientError(RuntimeError):
    """Raised when an LLM provider request fails."""


@dataclass(frozen=True)
class LLMSettings:
    """Minimal runtime settings for selecting and configuring a provider."""

    provider: str = "ollama"
    model: str = "llama3.1:8b"
    base_url: str = "http://localhost:11434"
    timeout_seconds: float = 90.0
    keep_alive: str = "10m"


class OllamaClient:
    """Local Ollama-backed text generation client."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        keep_alive: str = "10m",
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive

    def generate(self, request_data: LLMRequest) -> LLMResponse:
        """Generate text using Ollama's non-streaming generate endpoint."""
        payload: dict[str, object] = {
            "model": self.model,
            "prompt": request_data.prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
        }
        if request_data.system:
            payload["system"] = request_data.system
        if request_data.temperature is not None:
            payload["options"] = {"temperature": request_data.temperature}

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=f"{self.base_url}/api/generate",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                raise LLMClientError(
                    f"Ollama model '{self.model}' not found. Pull it first with: "
                    f"`ollama pull {self.model}`."
                ) from exc
            raise LLMClientError(
                f"Ollama request failed ({exc.code}). {detail.strip() or 'No details.'}"
            ) from exc
        except error.URLError as exc:
            raise LLMClientError(
                f"Ollama unavailable at {self.base_url}. "
                "Start it with `ollama serve` and ensure the URL is reachable."
            ) from exc
        except TimeoutError as exc:
            raise LLMClientError(
                f"Ollama request timed out for model '{self.model}' "
                f"after {self.timeout_seconds:.0f}s."
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMClientError("Ollama returned non-JSON response.") from exc

        text = data.get("response")
        if not isinstance(text, str):
            raise LLMClientError("Ollama response missing 'response' text.")
        return LLMResponse(text=text)


def load_llm_settings_from_env() -> LLMSettings:
    """Load minimal LLM settings from environment variables."""
    provider = os.getenv("SYDES_LLM_PROVIDER", "ollama").strip().lower() or "ollama"
    model = os.getenv("SYDES_LLM_MODEL", "llama3.1:8b").strip() or "llama3.1:8b"
    base_url = os.getenv("SYDES_LLM_BASE_URL", "http://localhost:11434").strip()
    base_url = base_url or "http://localhost:11434"
    timeout_raw = os.getenv("SYDES_LLM_TIMEOUT_SECONDS", "90").strip()
    keep_alive = os.getenv("SYDES_LLM_KEEP_ALIVE", "10m").strip() or "10m"
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError:
        timeout_seconds = 90.0
    if timeout_seconds <= 0:
        timeout_seconds = 90.0
    return LLMSettings(
        provider=provider,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        keep_alive=keep_alive,
    )


def create_default_llm_client() -> LLMClient:
    """Create the default LLM client from environment configuration."""
    settings = load_llm_settings_from_env()
    if settings.provider == "ollama":
        return OllamaClient(
            model=settings.model,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            keep_alive=settings.keep_alive,
        )
    raise LLMClientError(
        f"Unsupported SYDES_LLM_PROVIDER '{settings.provider}'. Supported: ollama."
    )


def ollama_connectivity_check(client: OllamaClient | None = None) -> tuple[bool, str]:
    """Run a tiny non-streaming generation to validate local Ollama connectivity."""
    active_client = client or create_default_llm_client()
    if not isinstance(active_client, OllamaClient):
        return False, "Connectivity check currently supports Ollama client only."
    try:
        response = active_client.generate(
            LLMRequest(prompt='Return JSON: {"endpoints":[]}', temperature=0)
        )
    except LLMClientError as exc:
        return False, str(exc)
    return bool(response.text.strip()), "ok"
