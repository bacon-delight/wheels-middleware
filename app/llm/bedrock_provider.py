"""Bedrock Converse provider (primary, in ap-south-2)."""

from __future__ import annotations

from typing import Any

from .base import LLMProvider, LLMResult, Tool


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
        max_tokens: int = 8000,
        temperature: float = 0.0,
    ) -> LLMResult:
        client = self._get_client()
        resp = client.converse(
            modelId=self.model_id,
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user_text}]}],
            toolConfig={
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
            },
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        data = _extract_tool_input(resp, tool.name)
        usage = resp.get("usage", {})
        return LLMResult(
            data=data,
            input_tokens=usage.get("inputTokens"),
            output_tokens=usage.get("outputTokens"),
            model=self.model_id,
        )


def _extract_tool_input(resp: dict[str, Any], tool_name: str) -> dict[str, Any]:
    content = resp.get("output", {}).get("message", {}).get("content", [])
    for block in content:
        tu = block.get("toolUse")
        if tu and tu.get("name") == tool_name:
            return tu.get("input", {})
    raise ValueError("Bedrock response contained no matching toolUse block")
