"""Anthropic SDK backend — uses ``anthropic.messages.create``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunch.backend.protocol import ModelResponse


@dataclass
class AnthropicSdkBackend:
    """Backend that calls the Anthropic Python SDK directly.

    Caching: the SDK supports cache_control blocks in messages.
    For now we send plain text prompts and let Anthropic's automatic
    prompt caching handle prefix reuse.
    """
    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 1024
    temperature: float = 0.0
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic()

    def call(self, prompt: str) -> ModelResponse:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        content = getattr(response, "content", None) or []
        text = ""
        if content:
            first = content[0]
            t = getattr(first, "text", None)
            if isinstance(t, str):
                text = t
            elif isinstance(first, dict):
                text = str(first.get("text", ""))

        usage = getattr(response, "usage", None)
        input_tokens: int | None = None
        if usage is not None:
            input_tokens = (
                getattr(usage, "input_tokens", 0)
                + getattr(usage, "cache_read_input_tokens", 0)
                + getattr(usage, "cache_creation_input_tokens", 0)
            )
        return ModelResponse(text=text, input_tokens=input_tokens)
