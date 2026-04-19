"""Tests for Ollama client configuration wiring."""

import pytest

from sydes.llm.client import (
    LLMClientError,
    OllamaClient,
    create_default_llm_client,
    load_llm_settings_from_env,
)


def test_load_llm_settings_from_env_defaults(monkeypatch) -> None:
    """Settings loader should use Ollama local defaults."""
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
    """Default client creation should respect configured model/url."""
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


def test_create_default_llm_client_rejects_unknown_provider(monkeypatch) -> None:
    """Unsupported providers should raise a clear error."""
    monkeypatch.setenv("SYDES_LLM_PROVIDER", "unknown")

    with pytest.raises(LLMClientError, match="Unsupported SYDES_LLM_PROVIDER"):
        create_default_llm_client()
