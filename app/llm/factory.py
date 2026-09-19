"""Select the configured LLM provider."""

from __future__ import annotations

from ..config import Settings, get_settings
from .base import LLMProvider


def _bedrock(settings: Settings):
    from .bedrock_provider import BedrockProvider

    # A model and the region that serves it are one choice. Amazon Nova is not offered in
    # ap-south-2 in any form, so a Nova id with the core region is not a slow path or a
    # degraded one — it fails outright, and the fallback chain then quietly re-reads the whole
    # contract on Claude at twenty times the price.
    region = settings.extract_region or settings.core_region
    model = settings.extract_model
    if "amazon.nova" in model and region == "ap-south-2":
        raise ValueError(
            f"{model} cannot be served from {region}; set EXTRACT_REGION=ap-south-1"
        )
    return BedrockProvider(model_id=model, region=region)


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
