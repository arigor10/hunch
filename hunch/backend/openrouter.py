"""OpenRouter backend — OpenAI SDK pointed at openrouter.ai."""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

from hunch.backend.protocol import ModelResponse


@dataclass
class OpenRouterBackend:
    """Backend that calls models via OpenRouter (OpenAI-compatible API).

    Supports DeepSeek V4 Pro, Gemma 4, and any model available on
    OpenRouter. Uses the OpenAI Python SDK with a custom base_url.

    Caching: transparent prefix caching on supported providers
    (e.g., SiliconFlow). No action needed from our side.
    """
    model: str = "deepseek/deepseek-v4-pro"
    max_tokens: int = 8192
    temperature: float = 0.0
    timeout_s: float = 600.0
    max_retries: int = 5
    initial_backoff_s: float = 5.0
    api_key: str | None = None
    require_cache: bool = False
    cache_warmup_ticks: int = 2
    provider_order: list[str] | None = None
    log: Any = field(default=None, repr=False)

    _client: Any = field(default=None, init=False, repr=False)
    _call_count: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.api_key is None:
            self.api_key = os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OPENROUTER_API_KEY not set. "
                "Set it as an environment variable or in the config file."
            )
        from openai import OpenAI
        self._client = OpenAI(
            api_key=self.api_key,
            base_url="https://openrouter.ai/api/v1",
        )

    def call(self, prompt: str) -> ModelResponse:
        last_err: Exception | None = None
        backoff = self.initial_backoff_s

        for attempt in range(1, self.max_retries + 1):
            try:
                extra_body = {}
                if self.provider_order:
                    extra_body["provider"] = {
                        "order": self.provider_order,
                        "allow_fallbacks": False,
                    }
                completion = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    timeout=self.timeout_s,
                    extra_body=extra_body or None,
                )
                text = completion.choices[0].message.content or ""
                usage = getattr(completion, "usage", None)
                input_tokens: int | None = None
                cached_tokens: int | None = None
                if usage is not None:
                    input_tokens = getattr(usage, "prompt_tokens", None)
                    details = getattr(usage, "prompt_tokens_details", None)
                    if details is not None:
                        cached_tokens = getattr(details, "cached_tokens", None)

                self._call_count += 1
                if self.log:
                    self.log(
                        f"[openrouter] prompt_tokens={input_tokens} "
                        f"cached_tokens={cached_tokens}"
                        + (f" (attempt {attempt})" if attempt > 1 else "")
                    )

                # Only enforce cache on first-attempt successes. Retries
                # push elapsed time past the provider's cache TTL, so a
                # miss after retries is expected, not a config problem.
                if (self.require_cache
                        and attempt == 1
                        and self._call_count > self.cache_warmup_ticks
                        and (cached_tokens is None or cached_tokens == 0)):
                    raise RuntimeError(
                        f"Cache miss on call {self._call_count} "
                        f"(prompt_tokens={input_tokens}, cached_tokens={cached_tokens}). "
                        f"require_cache is on — aborting to avoid silent cost blowup. "
                        f"Check provider cache TTL and tick interval."
                    )

                return ModelResponse(text=text, input_tokens=input_tokens)
            except RuntimeError:
                raise
            except Exception as e:
                last_err = e
                if attempt < self.max_retries:
                    jitter = backoff * (0.5 + random.random())
                    if self.log:
                        self.log(
                            f"[openrouter] attempt {attempt}/{self.max_retries} "
                            f"failed: {type(e).__name__}: {str(e)[:120]} "
                            f"— sleeping {jitter:.0f}s"
                        )
                    time.sleep(jitter)
                    backoff *= 2

        raise RuntimeError(
            f"OpenRouter call failed after {self.max_retries} attempts: {last_err}"
        )
