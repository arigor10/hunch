"""Backend protocol for model-agnostic critic engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelResponse:
    """Response from a model backend call."""
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None


class Backend(Protocol):
    """Minimal interface for a model backend.

    Each implementation owns its own caching strategy, retry logic,
    and authentication. The engine only sees call().
    """
    def call(self, prompt: str) -> ModelResponse: ...
