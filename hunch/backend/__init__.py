"""Pluggable model backends for the Hunch critic engine."""

from __future__ import annotations

from hunch.backend.config import BackendConfig, FullConfig, load_config
from hunch.backend.protocol import Backend, ModelResponse

__all__ = [
    "Backend",
    "BackendConfig",
    "FullConfig",
    "ModelResponse",
    "load_backend",
    "load_config",
]


def load_backend(config: BackendConfig, log=None) -> Backend:
    """Create a Backend instance from a BackendConfig."""
    if not config.model:
        raise ValueError(
            f"Backend config has empty model name (type={config.type!r})"
        )
    if config.type == "claude_cli":
        from hunch.backend.claude_cli import ClaudeCliBackend
        return ClaudeCliBackend(
            model=config.model,
            timeout_s=config.timeout_s,
        )

    if config.type == "anthropic_sdk":
        from hunch.backend.anthropic_sdk import AnthropicSdkBackend
        return AnthropicSdkBackend(
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )

    if config.type == "openrouter":
        from hunch.backend.openrouter import OpenRouterBackend
        return OpenRouterBackend(
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            initial_backoff_s=config.initial_backoff_s,
            api_key=config.api_key,
            require_cache=config.extra.get("require_cache", False),
            cache_warmup_ticks=config.extra.get("cache_warmup_ticks", 2),
            provider_order=config.extra.get("provider_order"),
            use_cache_control=config.extra.get("use_cache_control", False),
            cache_min_tokens=config.extra.get("cache_min_tokens", 0),
            log=log,
        )

    raise ValueError(f"Unknown backend type: {config.type!r}")
