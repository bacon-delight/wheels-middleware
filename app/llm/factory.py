"""Select the configured LLM provider."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMProvider


def _bedrock(settings: Settings):
    from .bedrock_provider import BedrockProvider

    return BedrockProvider(model_id=settings.extract_model, region=settings.core_region)


def _anthropic(settings: Settings):
    from .anthropic_provider import AnthropicProvider

    return AnthropicProvider(model=settings.anthropic_extract_model)


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm_provider == "anthropic":
        return _anthropic(settings)
    if settings.llm_provider == "bedrock":
        return _bedrock(settings)
    if settings.llm_provider == "fallback":
        # Strong in-region Bedrock model primary; Anthropic API (Sonnet 5) as fallback.
        from .fallback_provider import FallbackProvider

        return FallbackProvider([_bedrock(settings), _anthropic(settings)])
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
