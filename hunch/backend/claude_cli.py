"""Claude CLI backend — shells out to ``claude --print``."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from hunch.backend.protocol import ModelResponse


@dataclass
class ClaudeCliBackend:
    """Backend that calls ``claude --print --output-format json``.

    No API key needed — piggybacks on the caller's Claude Code session.
    Caching is automatic (Claude CLI's own prompt caching).
    """
    model: str = "claude-sonnet-4-5-20250929"
    timeout_s: float = 600.0

    def call(self, prompt: str, cache_break: int | None = None,
             suppress_cache_check: bool = False) -> ModelResponse:
        result = subprocess.run(
            [
                "claude",
                "--print",
                "--model", self.model,
                "--output-format", "json",
            ],
            input=prompt,
            cwd="/tmp",
            capture_output=True,
            text=True,
            timeout=self.timeout_s,
        )
        if result.returncode != 0:
            stderr_tail = (result.stderr or "").strip()[-400:]
            raise RuntimeError(
                f"claude CLI exited {result.returncode}: {stderr_tail}"
            )
        try:
            envelope = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"claude CLI returned non-JSON output: {exc}\n"
                f"stdout (last 400 chars): {result.stdout[-400:]}"
            )

        text = envelope.get("result", "")
        cost_usd = envelope.get("total_cost_usd")
        if cost_usd is not None:
            cost_usd = float(cost_usd)
        usage = envelope.get("usage") or {}
        input_tokens: int | None = None
        output_tokens: int | None = None
        cached_tokens: int | None = None
        cache_read_tokens: int | None = None
        if usage:
            cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            cached_tokens = (
                cache_read_tokens
                + usage.get("cache_creation_input_tokens", 0)
            )
            input_tokens = usage.get("input_tokens", 0) + cached_tokens
            output_tokens = usage.get("output_tokens", None)
        return ModelResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
        )
