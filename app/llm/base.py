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


class OutputOverflow(Exception):
    """The model ran out of output budget part-way through its tool call.

    Anthropic reports this as `stop_reason == "max_tokens"` and hands back the partial JSON.
    Nova instead fails the request outright, so nothing comes back at all — the same condition
    in a different shape, and worth its own type because the answer is to ask for less rather
    than to give up on the model.
    """


# What a finished answer looks like across providers. Anything else is suspect by construction.
CLEAN_STOP_REASONS = {"end_turn", "tool_use", "stop_sequence", "stop", "complete"}


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
    # What was asked for, so a response that used all of it can be recognised as cut off even
    # when the provider says nothing useful about why it stopped.
    max_output_tokens: int | None = None
    # Whether the parser had to rescue records from a half-written array. Only ever true of a
    # response that was cut off, and previously invisible.
    salvaged: bool = False

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens

    @property
    def truncated(self) -> bool:
        """Did this response stop before the model had finished?

        Tested by what a *clean* finish looks like, not by matching one provider's word for
        failure. `stop_reason == "max_tokens"` is Anthropic's spelling; Nova raises an exception
        instead, and a third provider says "length". An unrecognised reason counts as suspect,
        because a truncated extraction is indistinguishable from a short contract.

        A *missing* reason does not, on its own: a provider that never reports one would then
        mark every call truncated and the flag would mean nothing. Those are caught by the
        budget test below, which needs no cooperation from the provider at all.
        """
        if self.stop_reason and self.stop_reason.lower() not in CLEAN_STOP_REASONS:
            return True
        # Budget exhausted to the last few tokens: cut off whatever the stop reason claims.
        if self.max_output_tokens and (self.output_tokens or 0) >= self.max_output_tokens - 32:
            return True
        return self.salvaged


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
