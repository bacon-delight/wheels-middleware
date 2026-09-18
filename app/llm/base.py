"""Provider-neutral tool-use interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tool:
    """A forced tool the model must call, defining the output JSON schema."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class LLMResult:
    data: dict[str, Any]  # the tool_use input the model produced
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None
    # Why the model stopped. "max_tokens" means the tool input JSON is truncated, which looks
    # exactly like a clean extraction that found less — the most dangerous failure this system
    # has, because it is invisible without this field.
    stop_reason: str | None = None
    # Prompt-cache accounting. A cache that silently stops working costs money and nothing
    # else changes, so the numbers are carried through to where a person can see them.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    provider: str | None = None
    latency_ms: int | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"


class LLMProvider(ABC):
    name: str

    @abstractmethod
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
        """Run one turn that forces `tool`, returning the structured tool input.

        `cache_prefix` is large, unchanging content — the contract text — placed before the
        cache breakpoint so repeated calls over the same document read it at a fraction of the
        price. Providers that cannot cache must still honour the content.
        """
