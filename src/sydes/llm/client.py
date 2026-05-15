"""Provider-neutral LLM client abstraction with Ollama/OpenAI/Anthropic implementations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib import error, request
from typing import Protocol

DEFAULT_PROVIDER = "ollama"
DEFAULT_MODEL = "llama3.1:8b"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
SUPPORTED_PROVIDERS = ("ollama", "openai", "anthropic")


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

    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    timeout_seconds: float = 90.0
    keep_alive: str = "10m"


def parse_model_spec(model_spec: str) -> tuple[str, str]:
    """Parse provider-prefixed model spec using split(':', 1)."""
    value = model_spec.strip()
    if not value:
        raise LLMClientError("Model spec cannot be empty.")
    if ":" not in value:
        return DEFAULT_PROVIDER, value
    provider, model = value.split(":", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise LLMClientError(f"Invalid model spec '{model_spec}'. Expected <provider>:<model>.")
    return provider, model


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


class OpenAIClient:
    """OpenAI text generation client via HTTPS API."""

    def __init__(self, *, model: str, api_key: str, base_url: str, timeout_seconds: float = 90.0) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def generate(self, request_data: LLMRequest) -> LLMResponse:
        """Generate text using OpenAI chat completions API."""
        messages: list[dict[str, str]] = []
        if request_data.system:
            messages.append({"role": "system", "content": request_data.system})
        messages.append({"role": "user", "content": request_data.prompt})

        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
        }
        if request_data.temperature is not None:
            payload["temperature"] = request_data.temperature

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=f"{self.base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMClientError(
                f"OpenAI request failed ({exc.code}). {detail.strip() or 'No details.'}"
            ) from exc
        except error.URLError as exc:
            raise LLMClientError(f"OpenAI unavailable at {self.base_url}.") from exc
        except TimeoutError as exc:
            raise LLMClientError(
                f"OpenAI request timed out for model '{self.model}' after {self.timeout_seconds:.0f}s."
            ) from exc

        try:
            data = json.loads(raw)
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        return LLMResponse(text=content)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise LLMClientError("OpenAI response missing completion text.")


class AnthropicClient:
    """Anthropic text generation client via HTTPS API."""

    def __init__(self, *, model: str, api_key: str, base_url: str, timeout_seconds: float = 90.0) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def generate(self, request_data: LLMRequest) -> LLMResponse:
        """Generate text using Anthropic messages API."""
        payload: dict[str, object] = {
            "model": self.model,
            "max_tokens": 1200,
            "messages": [{"role": "user", "content": request_data.prompt}],
        }
        if request_data.system:
            payload["system"] = request_data.system
        if request_data.temperature is not None:
            payload["temperature"] = request_data.temperature

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=f"{self.base_url}/messages",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMClientError(
                f"Anthropic request failed ({exc.code}). {detail.strip() or 'No details.'}"
            ) from exc
        except error.URLError as exc:
            raise LLMClientError(f"Anthropic unavailable at {self.base_url}.") from exc
        except TimeoutError as exc:
            raise LLMClientError(
                f"Anthropic request timed out for model '{self.model}' after {self.timeout_seconds:.0f}s."
            ) from exc

        try:
            data = json.loads(raw)
            content = data.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text")
                        if isinstance(text, str):
                            return LLMResponse(text=text)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise LLMClientError("Anthropic response missing completion text.")


def load_llm_settings_from_env() -> LLMSettings:
    """Load minimal LLM settings from environment variables."""
    provider = os.getenv("SYDES_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    model = os.getenv("SYDES_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    default_base_url = {
        "ollama": DEFAULT_OLLAMA_BASE_URL,
        "openai": DEFAULT_OPENAI_BASE_URL,
        "anthropic": DEFAULT_ANTHROPIC_BASE_URL,
    }.get(provider, DEFAULT_OLLAMA_BASE_URL)
    base_url = os.getenv("SYDES_LLM_BASE_URL", default_base_url).strip() or default_base_url
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


def create_default_llm_client(model_spec: str | None = None) -> LLMClient:
    """Create the default LLM client from model spec or environment configuration."""
    settings = load_llm_settings_from_env()

    provider = settings.provider
    model = settings.model
    if model_spec:
        provider, model = parse_model_spec(model_spec)

    if provider == "ollama":
        return OllamaClient(
            model=model,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
            keep_alive=settings.keep_alive,
        )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise LLMClientError("OPENAI_API_KEY is required when using provider 'openai'.")
        return OpenAIClient(
            model=model,
            api_key=api_key,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
        )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise LLMClientError("ANTHROPIC_API_KEY is required when using provider 'anthropic'.")
        return AnthropicClient(
            model=model,
            api_key=api_key,
            base_url=settings.base_url,
            timeout_seconds=settings.timeout_seconds,
        )

    raise LLMClientError(
        f"Unsupported LLM provider: {provider}\n"
        "Supported providers: ollama, openai, anthropic."
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
