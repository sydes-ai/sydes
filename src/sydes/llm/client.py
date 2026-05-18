"""Provider-neutral LLM client abstraction with Ollama/OpenAI/Anthropic implementations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from urllib import error, request
from typing import Protocol

from anthropic import Anthropic
from anthropic import AnthropicError
from openai import OpenAI
from openai import OpenAIError

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


def classify_llm_error(message: str) -> str:
    """Classify raw LLM errors into user-facing failure categories."""
    lowered = message.lower()
    if "api key is not configured" in lowered or "api_key" in lowered or "not set" in lowered:
        return f"auth/config failure: {message}"
    if "model not available" in lowered or ("model '" in lowered and "not found" in lowered):
        return f"model unavailable: {message}"
    if (
        "unavailable at" in lowered
        or "timed out" in lowered
        or "reachable" in lowered
        or "connection" in lowered
        or "network" in lowered
    ):
        return f"network/connectivity failure: {message}"
    if (
        "not valid json" in lowered
        or "non-json" in lowered
        or "missing completion text" in lowered
        or "missing 'response' text" in lowered
        or "parse" in lowered
    ):
        return f"model output parse failure: {message}"
    return f"llm failure: {message}"


@dataclass(frozen=True)
class LLMValidationResult:
    """Provider-neutral preflight validation result for LLM availability."""

    ok: bool
    provider: str
    model: str
    base_url: str | None = None
    reason: str | None = None
    available_models: tuple[str, ...] = ()


@dataclass(frozen=True)
class LLMSettings:
    """Minimal runtime settings for selecting and configuring a provider."""

    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_OLLAMA_BASE_URL
    timeout_seconds: float = 90.0
    keep_alive: str = "10m"
    temperature: float = 0.0


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
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.keep_alive = keep_alive
        self.temperature = temperature

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
        resolved_temperature = (
            request_data.temperature
            if request_data.temperature is not None
            else self.temperature
        )
        if resolved_temperature is not None:
            payload["options"] = {"temperature": resolved_temperature}

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
    """OpenAI text generation client via official OpenAI Python SDK."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self._client = OpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    def generate(self, request_data: LLMRequest) -> LLMResponse:
        """Generate text using OpenAI chat completions API."""
        messages: list[dict[str, str]] = []
        if request_data.system:
            messages.append({"role": "system", "content": request_data.system})
        messages.append({"role": "user", "content": request_data.prompt})

        resolved_temperature = (
            request_data.temperature
            if request_data.temperature is not None
            else self.temperature
        )
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=resolved_temperature,
            )
        except OpenAIError as exc:
            status_code = getattr(exc, "status_code", None)
            if status_code == 404:
                raise LLMClientError(
                    "OpenAI request returned 404. Check that the OpenAI provider is not using an "
                    "Ollama/custom base URL. SYDES_LLM_BASE_URL is only for Ollama; use "
                    "OPENAI_BASE_URL only if you intentionally need a custom OpenAI-compatible endpoint."
                ) from exc
            raise LLMClientError(
                f"OpenAI request failed for model '{self.model}': {exc}"
            ) from exc
        except TimeoutError as exc:
            raise LLMClientError(
                f"OpenAI request timed out for model '{self.model}' after {self.timeout_seconds:.0f}s."
            ) from exc

        choices = getattr(response, "choices", None)
        if choices:
            first_choice = choices[0]
            message = getattr(first_choice, "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if isinstance(content, str):
                return LLMResponse(text=content)
        raise LLMClientError("OpenAI response missing completion text.")


class AnthropicClient:
    """Anthropic text generation client via official Anthropic Python SDK."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 90.0,
        temperature: float = 0.0,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = 4096
        self._client = Anthropic(
            api_key=api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )

    def generate(self, request_data: LLMRequest) -> LLMResponse:
        """Generate text using Anthropic messages API."""
        resolved_temperature = (
            request_data.temperature
            if request_data.temperature is not None
            else self.temperature
        )
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": request_data.prompt}],
                system=request_data.system,
                temperature=resolved_temperature,
            )
        except AnthropicError as exc:
            raise LLMClientError(
                f"Anthropic request failed for model '{self.model}': {exc}"
            ) from exc
        except TimeoutError as exc:
            raise LLMClientError(
                f"Anthropic request timed out for model '{self.model}' after {self.timeout_seconds:.0f}s."
            ) from exc

        blocks = getattr(response, "content", None)
        if not isinstance(blocks, list):
            raise LLMClientError("Anthropic response missing completion text.")

        text_parts: list[str] = []
        for block in blocks:
            block_type = getattr(block, "type", None)
            if block_type != "text":
                continue
            text_value = getattr(block, "text", None)
            if isinstance(text_value, str) and text_value:
                text_parts.append(text_value)
        if not text_parts:
            raise LLMClientError("Anthropic response missing completion text.")
        return LLMResponse(text="\n".join(text_parts))


def load_llm_settings_from_env() -> LLMSettings:
    """Load minimal LLM settings from environment variables."""
    provider = os.getenv("SYDES_LLM_PROVIDER", DEFAULT_PROVIDER).strip().lower() or DEFAULT_PROVIDER
    model = os.getenv("SYDES_LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    base_url = os.getenv("SYDES_LLM_BASE_URL", DEFAULT_OLLAMA_BASE_URL).strip() or DEFAULT_OLLAMA_BASE_URL
    timeout_raw = os.getenv("SYDES_LLM_TIMEOUT_SECONDS", "90").strip()
    temperature_raw = os.getenv("SYDES_LLM_TEMPERATURE", "0").strip()
    keep_alive = os.getenv("SYDES_LLM_KEEP_ALIVE", "10m").strip() or "10m"
    try:
        timeout_seconds = float(timeout_raw)
    except ValueError:
        timeout_seconds = 90.0
    if timeout_seconds <= 0:
        timeout_seconds = 90.0
    try:
        temperature = float(temperature_raw)
    except ValueError:
        temperature = 0.0
    if temperature < 0:
        temperature = 0.0
    if temperature > 2:
        temperature = 2.0
    return LLMSettings(
        provider=provider,
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
        keep_alive=keep_alive,
        temperature=temperature,
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
            temperature=settings.temperature,
        )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise LLMClientError(
                "OpenAI provider selected, but OPENAI_API_KEY is not set.\n\n"
                "Set it with:\n"
                "  export OPENAI_API_KEY=...\n\n"
                "Or choose another provider:\n"
                '  sydes trace "/checkout" --method POST --model ollama:llama3.1:8b'
            )
        openai_base_url = os.getenv("OPENAI_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL
        return OpenAIClient(
            model=model,
            api_key=api_key,
            base_url=openai_base_url,
            timeout_seconds=settings.timeout_seconds,
            temperature=settings.temperature,
        )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise LLMClientError(
                "Anthropic provider selected, but ANTHROPIC_API_KEY is not set.\n\n"
                "Set it with:\n"
                "  export ANTHROPIC_API_KEY=...\n\n"
                "Or choose another provider:\n"
                '  sydes trace "/checkout" --method POST --model ollama:llama3.1:8b'
            )
        anthropic_base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or DEFAULT_ANTHROPIC_BASE_URL
        return AnthropicClient(
            model=model,
            api_key=api_key,
            base_url=anthropic_base_url,
            timeout_seconds=settings.timeout_seconds,
            temperature=settings.temperature,
        )

    raise LLMClientError(
        f"Unsupported LLM provider: {provider}\n"
        "Supported providers: ollama, openai, anthropic."
    )


def _resolve_provider_and_model(model_spec: str | None) -> tuple[str, str, LLMSettings]:
    """Resolve provider/model from model spec override plus environment defaults."""
    settings = load_llm_settings_from_env()
    provider = settings.provider
    model = settings.model
    if model_spec:
        provider, model = parse_model_spec(model_spec)
    return provider, model, settings


def _validate_ollama_available(model: str, settings: LLMSettings) -> LLMValidationResult:
    """Validate Ollama base URL reachability and model availability."""
    base_url = settings.base_url.rstrip("/")
    tags_url = f"{base_url}/api/tags"
    available_models: list[str] = []
    try:
        with request.urlopen(tags_url, timeout=settings.timeout_seconds) as resp:
            raw = resp.read().decode("utf-8")
        payload = json.loads(raw)
        models = payload.get("models")
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str) and name.strip():
                    available_models.append(name.strip())
    except error.URLError:
        return LLMValidationResult(
            ok=False,
            provider="ollama",
            model=model,
            base_url=base_url,
            reason=f"Ollama unavailable at {base_url}. Start it with `ollama serve`.",
        )
    except (error.HTTPError, json.JSONDecodeError, TimeoutError):
        # Fall back to tiny generate probe if tags endpoint is unavailable.
        probe = OllamaClient(
            model=model,
            base_url=base_url,
            timeout_seconds=settings.timeout_seconds,
            keep_alive=settings.keep_alive,
            temperature=settings.temperature,
        )
        try:
            probe.generate(LLMRequest(prompt='Return JSON: {"ok":true}', temperature=0))
            return LLMValidationResult(
                ok=True,
                provider="ollama",
                model=model,
                base_url=base_url,
            )
        except LLMClientError as exc:
            return LLMValidationResult(
                ok=False,
                provider="ollama",
                model=model,
                base_url=base_url,
                reason=str(exc),
            )

    if model not in available_models:
        preview = ", ".join(available_models[:8]) if available_models else "(none found)"
        return LLMValidationResult(
            ok=False,
            provider="ollama",
            model=model,
            base_url=base_url,
            reason=(
                f"LLM model not available: {model}. "
                f"Available local models include: {preview}"
            ),
            available_models=tuple(available_models),
        )

    return LLMValidationResult(
        ok=True,
        provider="ollama",
        model=model,
        base_url=base_url,
        available_models=tuple(available_models),
    )


def validate_llm_available(model_spec: str | None = None) -> LLMValidationResult:
    """Validate configured LLM availability before running discovery/trace."""
    provider, model, settings = _resolve_provider_and_model(model_spec)

    if provider == "ollama":
        return _validate_ollama_available(model, settings)

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        base_url = os.getenv("OPENAI_BASE_URL", "").strip() or DEFAULT_OPENAI_BASE_URL
        if not api_key:
            return LLMValidationResult(
                ok=False,
                provider="openai",
                model=model,
                base_url=base_url,
                reason="OpenAI API key is not configured.",
            )
        return LLMValidationResult(
            ok=True,
            provider="openai",
            model=model,
            base_url=base_url,
        )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip() or DEFAULT_ANTHROPIC_BASE_URL
        if not api_key:
            return LLMValidationResult(
                ok=False,
                provider="anthropic",
                model=model,
                base_url=base_url,
                reason="Anthropic API key is not configured.",
            )
        return LLMValidationResult(
            ok=True,
            provider="anthropic",
            model=model,
            base_url=base_url,
        )

    return LLMValidationResult(
        ok=False,
        provider=provider,
        model=model,
        reason=f"Unsupported LLM provider: {provider}",
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
