"""LLM provider abstraction.

Primary: Bedrock (in ap-south-2, IAM-authed, no external key, data stays in-region).
Fallback/dev: Anthropic API (uses ANTHROPIC_API_KEY; lets us run without AWS creds).
Both are driven through a single forced tool-use call so output shape is guaranteed.
"""

from .base import LLMProvider, Tool
from .factory import get_provider

__all__ = ["LLMProvider", "Tool", "get_provider"]
