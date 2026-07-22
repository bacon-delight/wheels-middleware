"""Anthropic API provider (fallback/dev; uses ANTHROPIC_API_KEY)."""

from __future__ import annotations

import os

from .base import LLMProvider, LLMResult, Tool


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic  # lazy import

            if not self._api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool: Tool,
        max_tokens: int = 8000,
        temperature: float = 0.0,
    ) -> LLMResult:
        client = self._get_client()
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            tools=[
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                }
            ],
            tool_choice={"type": "tool", "name": tool.name},
            messages=[{"role": "user", "content": user_text}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool.name:
                return LLMResult(
                    data=block.input,
                    input_tokens=resp.usage.input_tokens,
                    output_tokens=resp.usage.output_tokens,
                    model=self.model,
                )
        raise ValueError("Anthropic response contained no matching tool_use block")
