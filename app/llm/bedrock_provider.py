"""Bedrock Converse provider (primary, in ap-south-2)."""

from __future__ import annotations

import logging
import time
from typing import Any

from .base import LLMProvider, LLMResult, Tool

log = logging.getLogger(__name__)


class BedrockProvider(LLMProvider):
    name = "bedrock"

    def __init__(self, model_id: str, region: str):
        self.model_id = model_id
        self.region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3  # lazy so importing the package needs no AWS deps at import time
            from botocore.config import Config

            # A full-document extraction can generate for well over botocore's default 60s
            # read-timeout; without this the socket times out mid-generation and boto3
            # silently retries the (expensive) call. Give it room and cap retries.
            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(
                    read_timeout=300,
                    connect_timeout=10,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
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
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": {"json": tool.input_schema},
                    }
                }
            ],
            "toolChoice": {"tool": {"name": tool.name}},
        }

        def _call(with_cache: bool):
            # Converse marks a cache breakpoint with a cachePoint block, not the cache_control
            # field the Messages API uses.
            blocks: list[dict[str, Any]] = [{"text": system}]
            if cache_prefix:
                blocks.append({"text": cache_prefix})
                if with_cache:
                    blocks.append({"cachePoint": {"type": "default"}})
            return client.converse(
                modelId=self.model_id,
                system=blocks,
                messages=[{"role": "user", "content": [{"text": user_text}]}],
                toolConfig=tool_config,
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )

        started = time.monotonic()
        try:
            resp = _call(with_cache=bool(cache_prefix))
        except Exception as e:  # noqa: BLE001 - caching is an optimisation, never a failure
            if not cache_prefix:
                raise
            log.warning("Bedrock rejected the cache point (%s); retrying uncached", e)
            resp = _call(with_cache=False)
        latency_ms = int((time.monotonic() - started) * 1000)

        data = _extract_tool_input(resp, tool.name)
        usage = resp.get("usage", {})
        return LLMResult(
            data=data,
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            model=self.model_id,
            stop_reason=resp.get("stopReason"),
            cache_read_tokens=usage.get("cacheReadInputTokens"),
            cache_write_tokens=usage.get("cacheWriteInputTokens"),
            provider=self.name,
            latency_ms=latency_ms,
        )


def _extract_tool_input(resp: dict[str, Any], tool_name: str) -> dict[str, Any]:
    content = resp.get("output", {}).get("message", {}).get("content", [])
    for block in content:
        tu = block.get("toolUse")
        if tu and tu.get("name") == tool_name:
            return tu.get("input", {})
    raise ValueError("Bedrock response contained no matching toolUse block")
