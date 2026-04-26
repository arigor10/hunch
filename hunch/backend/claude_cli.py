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

    def call(self, prompt: str) -> ModelResponse:
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
        except json.JSONDecodeError:
            return ModelResponse(text=result.stdout, input_tokens=None)

        text = envelope.get("result", "")
        usage = envelope.get("usage") or {}
        input_tokens: int | None = None
        if usage:
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )
        return ModelResponse(text=text, input_tokens=input_tokens)
