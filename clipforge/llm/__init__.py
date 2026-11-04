"""Pluggable LLM providers. `get_provider(cfg)` returns the one configured in config.yaml."""

from __future__ import annotations

from clipforge.config import LLMConfig, load_secrets
from clipforge.llm.base import LLMError, LLMProvider


def get_provider(cfg: LLMConfig, provider: str | None = None, model: str | None = None) -> LLMProvider:
    name = (provider or cfg.provider).lower()
    # cfg.model belongs to cfg.provider; switching provider without a model means "auto"
    model = model or (cfg.model if name == cfg.provider else "auto")
    secrets = load_secrets()
    if name == "ollama":
        from clipforge.llm.ollama_provider import OllamaProvider

        return OllamaProvider(model, cfg.temperature, cfg.timeout_sec, cfg.ollama_url, cfg.max_tokens, cfg.keep_alive)
    if name == "gemini":
        from clipforge.llm.gemini_provider import GeminiProvider

        return GeminiProvider(secrets.gemini_api_key, model, cfg.temperature)
    if name == "groq":
        from clipforge.llm.groq_provider import GroqProvider

        return GroqProvider(secrets.groq_api_key, model, cfg.temperature)
    if name == "mock":
        from clipforge.llm.mock_provider import MockProvider

        return MockProvider()
    raise LLMError(f"Unknown LLM provider '{name}'. Use one of: ollama, gemini, groq, mock")


__all__ = ["get_provider", "LLMError", "LLMProvider"]
