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
    # cached_tokens = cache_read + cache_creation (total tokens that touched
    # the cache, read OR written). cache_read_tokens = the cheap reads only.
    # They differ on cache *writes* (creation) — billed at a premium and NOT a
    # hit — so keep them separate; "% hit" should reflect real reads.
    cached_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None


class Backend(Protocol):
    """Minimal interface for a model backend.

    Each implementation owns its own caching strategy, retry logic,
    and authentication. The engine only sees call().

    cache_break: optional character offset into prompt. Everything
    before this index is a stable prefix (cacheable); everything
    from this index onward is new content. Backends that support
    explicit cache_control use this to split the prompt.
    """
    def call(
        self,
        prompt: str,
        cache_break: int | None = None,
        suppress_cache_check: bool = False,
    ) -> ModelResponse: ...
