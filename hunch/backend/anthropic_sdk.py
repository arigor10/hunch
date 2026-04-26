"""Anthropic SDK backend — uses ``anthropic.messages.create``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunch.backend.protocol import ModelResponse


@dataclass
class AnthropicSdkBackend:
    """Backend that calls the Anthropic Python SDK directly.

    Caching: when cache_break is provided, the prompt is split into
    two content blocks — the stable prefix with cache_control and
    the varying suffix without — for explicit prefix caching.
    """
    model: str = "claude-sonnet-4-5-20250929"
    max_tokens: int = 1024
    temperature: float = 0.0
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic()

    def call(self, prompt: str, cache_break: int | None = None) -> ModelResponse:
        if cache_break and cache_break < len(prompt):
            content: Any = [
                {"type": "text", "text": prompt[:cache_break],
                 "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": prompt[cache_break:]},
            ]
        else:
            content = prompt
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            messages=[{"role": "user", "content": content}],
        )
        resp_content = getattr(response, "content", None) or []
        text = ""
        if resp_content:
            first = resp_content[0]
            t = getattr(first, "text", None)
            if isinstance(t, str):
                text = t
            elif isinstance(first, dict):
                text = str(first.get("text", ""))

        usage = getattr(response, "usage", None)
        input_tokens: int | None = None
        output_tokens: int | None = None
        cached_tokens: int | None = None
        if usage is not None:
            cached_tokens = (
                getattr(usage, "cache_read_input_tokens", 0)
                + getattr(usage, "cache_creation_input_tokens", 0)
            )
            input_tokens = getattr(usage, "input_tokens", 0) + cached_tokens
            output_tokens = getattr(usage, "output_tokens", None)
        return ModelResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
