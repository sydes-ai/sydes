"""Tests for LLM client configuration and provider factory wiring."""

import pytest

from sydes.llm.client import (
    AnthropicClient,
    LLMClientError,
    OllamaClient,
    OpenAIClient,
    create_default_llm_client,
    load_llm_settings_from_env,
    parse_model_spec,
)


def test_parse_model_spec_openai() -> None:
    """Provider-prefixed OpenAI model specs should parse cleanly."""
    provider, model = parse_model_spec("openai:gpt-4.1-mini")
    assert provider == "openai"
    assert model == "gpt-4.1-mini"


def test_parse_model_spec_anthropic() -> None:
    """Provider-prefixed Anthropic model specs should parse cleanly."""
    provider, model = parse_model_spec("anthropic:claude-3-5-sonnet-latest")
    assert provider == "anthropic"
    assert model == "claude-3-5-sonnet-latest"


def test_parse_model_spec_ollama_keeps_inner_colon() -> None:
    """Ollama models with colons must preserve the full model suffix."""
    provider, model = parse_model_spec("ollama:llama3.1:8b")
    assert provider == "ollama"
    assert model == "llama3.1:8b"


def test_load_llm_settings_from_env_defaults(monkeypatch) -> None:
    """Settings loader should use local Ollama defaults."""
    monkeypatch.delenv("SYDES_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SYDES_LLM_MODEL", raising=False)
    monkeypatch.delenv("SYDES_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SYDES_LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("SYDES_LLM_KEEP_ALIVE", raising=False)

    settings = load_llm_settings_from_env()

    assert settings.provider == "ollama"
    assert settings.model == "llama3.1:8b"
    assert settings.base_url == "http://localhost:11434"
    assert settings.timeout_seconds == 90.0
    assert settings.keep_alive == "10m"


def test_create_default_llm_client_uses_env(monkeypatch) -> None:
    """Default client creation should respect configured Ollama values."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SYDES_LLM_MODEL", "qwen2.5:7b")
    monkeypatch.setenv("SYDES_LLM_BASE_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("SYDES_LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("SYDES_LLM_KEEP_ALIVE", "30m")

    client = create_default_llm_client()

    assert isinstance(client, OllamaClient)
    assert client.model == "qwen2.5:7b"
    assert client.base_url == "http://127.0.0.1:11434"
    assert client.timeout_seconds == 120
    assert client.keep_alive == "30m"


def test_create_default_llm_client_uses_model_spec_override(monkeypatch) -> None:
    """Model-spec override should pick provider+model independent of env model."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SYDES_LLM_MODEL", "llama3.1:8b")
    monkeypatch.setenv("SYDES_LLM_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    client = create_default_llm_client(model_spec="openai:gpt-4.1-mini")

    assert isinstance(client, OpenAIClient)
    assert client.model == "gpt-4.1-mini"


def test_create_default_llm_client_supports_anthropic_from_model_spec(monkeypatch) -> None:
    """Model-spec override should support Anthropic provider selection."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("SYDES_LLM_MODEL", "llama3.1:8b")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    client = create_default_llm_client(model_spec="anthropic:claude-3-5-sonnet-latest")

    assert isinstance(client, AnthropicClient)
    assert client.model == "claude-3-5-sonnet-latest"


def test_create_default_llm_client_rejects_unknown_provider(monkeypatch) -> None:
    """Unsupported providers should raise the requested error message."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "unknown")

    with pytest.raises(LLMClientError, match="Unsupported LLM provider: unknown"):
        create_default_llm_client()


def test_create_default_llm_client_requires_openai_key(monkeypatch) -> None:
    """OpenAI provider should fail clearly when API key is missing."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "openai")
    monkeypatch.setenv("SYDES_LLM_MODEL", "gpt-4.1-mini")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMClientError, match="OPENAI_API_KEY is required"):
        create_default_llm_client()
