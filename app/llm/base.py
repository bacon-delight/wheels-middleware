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

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


class LLMProvider(ABC):
    name: str

    @abstractmethod
    def call_tool(
        self,
        *,
        system: str,
        user_text: str,
        tool: Tool,
        max_tokens: int = 8000,
        temperature: float = 0.0,
    ) -> LLMResult:
        """Run one turn that forces `tool`, returning the structured tool input."""
