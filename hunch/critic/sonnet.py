"""Sonnet-backed Accumulating Critic (v0.1).

Backward-compatible wrapper around CriticEngine. Constructs an
AnthropicSdkBackend (when ``client`` is injected) or ClaudeCliBackend
(default) and delegates all Critic protocol calls to the engine.

For new code that uses TOML config files, use CriticEngine directly
with load_backend().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.backend.anthropic_sdk import AnthropicSdkBackend
from hunch.backend.claude_cli import ClaudeCliBackend
from hunch.critic.engine import CriticEngine, CriticEngineConfig, parse_response
from hunch.critic.protocol import Hunch, TriggeringRefs


DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


@dataclass(frozen=True)
class SonnetCriticConfig:
    """Runtime config for the v0.1 Sonnet Critic.

    Watermark defaults match the sim driver's settings that shipped
    hunches end-to-end on AR in the private repo. Lower them for
    tests or when using a smaller-context model.
    """
    model: str = DEFAULT_MODEL
    prompt_path: Path | None = None           # None → packaged nose_v1.md
    cli_timeout_s: float = 600.0
    low_watermark: int = 140_000
    high_watermark: int = 180_000
    max_consecutive_failures: int = 3
    dry_run: bool = False


CallableClient = Any


@dataclass
class SonnetCritic:
    """Accumulating Sonnet-backed Critic (critic v0.1).

    Construct fresh per session. Framework calls init() once, then
    tick() per trigger firing, then shutdown() on exit.

    ``client`` is an optional Anthropic SDK-shaped object; tests inject
    a fake, users with API credits can pass a real one. When None
    (default), we shell out to ``claude --print`` and piggyback on the
    caller's Claude Code session.

    Delegates to CriticEngine internally.
    """
    config: SonnetCriticConfig = field(default_factory=SonnetCriticConfig)
    client: CallableClient | None = None
    log: Callable[[str], None] | None = None

    _engine: CriticEngine | None = field(default=None, init=False, repr=False)

    @property
    def _stream(self):
        if self._engine is None:
            return None
        return self._engine._stream

    @property
    def _initialized(self):
        if self._engine is None:
            return False
        return self._engine._initialized

    # ------------------------------------------------------------------
    # Critic protocol — delegate to engine
    # ------------------------------------------------------------------

    def init(self, config: dict[str, Any]) -> None:
        if self._engine is not None and self._engine._initialized:
            raise RuntimeError("SonnetCritic.init called twice")
        backend = self._make_backend()
        engine_config = CriticEngineConfig(
            prompt_path=self.config.prompt_path,
            low_watermark=self.config.low_watermark,
            high_watermark=self.config.high_watermark,
            max_consecutive_failures=self.config.max_consecutive_failures,
            dry_run=self.config.dry_run,
        )
        self._engine = CriticEngine(
            backend=backend,
            config=engine_config,
            log=self.log,
        )
        self._engine.init(config)

    def tick(
        self,
        tick_id: str,
        bookmark_prev: int,
        bookmark_now: int,
    ) -> list[Hunch]:
        if self._engine is None:
            raise RuntimeError("SonnetCritic.tick called before init")
        return self._engine.tick(tick_id, bookmark_prev, bookmark_now)

    def shutdown(self) -> None:
        if self._engine is not None:
            self._engine.shutdown()
            self._engine = None

    # ------------------------------------------------------------------
    # Backend construction
    # ------------------------------------------------------------------

    def _make_backend(self):
        if self.client is not None:
            return AnthropicSdkBackend(
                model=self.config.model,
                max_tokens=1024,
                temperature=0.0,
                client=self.client,
            )
        return ClaudeCliBackend(
            model=self.config.model,
            timeout_s=self.config.cli_timeout_s,
        )
