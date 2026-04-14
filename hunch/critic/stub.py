"""A no-op Critic for wiring up the framework.

`StubCritic` exists so the framework's trigger → Critic → hunches.jsonl
pipeline can be built and integration-tested before the real Sonnet-
backed Critic lands. It satisfies the `Critic` protocol and records
every call it receives, so tests can assert that the framework is
speaking to it correctly.

Once `hunch/critic/stateless_sonnet.py` lands (per critic_v0.md), the
framework swaps this out via config. Keeping the stub checked in is
also useful for offline development, CI, and demos where hitting the
Anthropic API is undesirable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hunch.critic.protocol import Hunch


@dataclass
class StubCritic:
    """No-op Critic. Returns an empty hunch list for every tick.

    Records the tick arguments in `tick_log` so tests can verify the
    framework is driving the Critic with the expected bookmarks. Also
    tracks whether `init` and `shutdown` fired — a common wiring bug.
    """
    initialized: bool = False
    shutdown_called: bool = False
    tick_log: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    def init(self, config: dict[str, Any]) -> None:
        if self.initialized:
            raise RuntimeError("StubCritic.init called twice")
        self.config = dict(config)
        self.initialized = True

    def tick(
        self,
        tick_id: str,
        bookmark_prev: int,
        bookmark_now: int,
    ) -> list[Hunch]:
        if not self.initialized:
            raise RuntimeError("StubCritic.tick called before init")
        if self.shutdown_called:
            raise RuntimeError("StubCritic.tick called after shutdown")
        if bookmark_now < bookmark_prev:
            # The framework should never walk the bookmark backwards;
            # catching this here surfaces wiring bugs early.
            raise ValueError(
                f"bookmark_now ({bookmark_now}) < bookmark_prev ({bookmark_prev})"
            )
        self.tick_log.append(
            {
                "tick_id": tick_id,
                "bookmark_prev": bookmark_prev,
                "bookmark_now": bookmark_now,
            }
        )
        return []

    def shutdown(self) -> None:
        self.shutdown_called = True
