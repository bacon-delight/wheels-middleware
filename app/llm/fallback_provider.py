"""Provider chain: try each provider in order, fall through on failure.

Used to run a strong in-region Bedrock model as primary and fall back to the Anthropic
API (e.g. Sonnet 5) when Bedrock errors — access not yet granted, throttling, or an outage.
The fallback provider is constructed lazily, so a missing ANTHROPIC_API_KEY only matters if
the primary actually fails.
"""

from __future__ import annotations

import logging

from .base import LLMProvider, LLMResult, Tool

log = logging.getLogger(__name__)


class FallbackProvider(LLMProvider):
    name = "fallback"

    def __init__(self, providers: list[LLMProvider]):
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self.providers = providers

    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool: Tool,
        max_tokens: int = 8000,
        temperature: float = 0.0,
    ) -> LLMResult:
        errors: list[str] = []
        for i, provider in enumerate(self.providers):
            try:
                result = provider.call_tool(
                    system=system,
                    user_text=user_text,
                    tool=tool,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                if i > 0:
                    log.warning("LLM primary failed; served by fallback %s", provider.name)
                return result
            except Exception as e:  # noqa: BLE001 - deliberately fall through on any failure
                errors.append(f"{provider.name}: {type(e).__name__}: {e}")
                log.warning("LLM provider %s failed, trying next: %s", provider.name, e)
        raise RuntimeError("All LLM providers failed: " + " | ".join(errors))
