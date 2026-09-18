"""Anthropic API provider (fallback/dev; uses ANTHROPIC_API_KEY)."""

from __future__ import annotations

import logging
import os
import time

from .base import LLMProvider, LLMResult, Tool

log = logging.getLogger(__name__)


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
        cache_prefix: str | None = None,
        max_tokens: int = 16000,
        temperature: float = 0.0,
    ) -> LLMResult:
        client = self._get_client()

        # The breakpoint goes at the end of the last unchanging block, so the tool schema, the
        # system prompt and the whole contract are cached together and only the per-call
        # instruction is re-read.
        system_blocks: list[dict] = [{"type": "text", "text": system}]
        if cache_prefix:
            system_blocks.append(
                {
                    "type": "text",
                    "text": cache_prefix,
                    "cache_control": {"type": "ephemeral"},
                }
            )

        started = time.monotonic()
        resp = client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_blocks,
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
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = resp.usage
        for block in resp.content:
            if block.type == "tool_use" and block.name == tool.name:
                return LLMResult(
                    data=block.input,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    model=self.model,
                    stop_reason=resp.stop_reason,
                    cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
                    cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
                    provider=self.name,
                    latency_ms=latency_ms,
                )
        raise ValueError("Anthropic response contained no matching tool_use block")
