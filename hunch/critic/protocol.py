"""The Critic interface and the value shapes that flow through it.

The Critic is deliberately a black box. The framework knows only:

  - How to start one (`init`).
  - How to ask it for hunches given a replay-buffer bookmark (`tick`).
  - How to stop it (`shutdown`).

`Hunch` is the shape defined in `docs/critic_v0.md` §Output schema:
  smell, description, triggering_refs — nothing else.

`hunch_emit_record` formats a `Hunch` into the `hunches.jsonl` emit-event
shape from framework_v0.md Appendix A (event-sourced, append-only).

v0 runs the Critic in-process; the stdio JSON wire format is reserved
for future implementations and documented alongside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Value shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TriggeringRefs:
    """Machine-readable citations for a hunch.

    `chunks` and `artifacts` point into the replay buffer so the surface
    (and future analytics) can highlight the originating evidence.
    """
    chunks: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, list[str]]:
        return {"chunks": list(self.chunks), "artifacts": list(self.artifacts)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TriggeringRefs:
        return cls(
            chunks=list(d.get("chunks") or []),
            artifacts=list(d.get("artifacts") or []),
        )


@dataclass(frozen=True)
class Hunch:
    """A single nose-firing moment the Critic chose to surface.

    Fields mirror `docs/critic_v0.md` §Output schema exactly:
      - `smell` — ≤80-char headline, the *claim*.
      - `description` — 2–4 sentences with specific citations.
      - `triggering_refs` — machine-readable citations.

    `hunch_id` is deliberately NOT part of this shape. The framework
    assigns ids when it writes the hunch to `hunches.jsonl` — keeping
    the Critic free of id-allocation concerns and letting the framework
    enforce monotonicity / uniqueness centrally.
    """
    smell: str
    description: str
    triggering_refs: TriggeringRefs = field(default_factory=TriggeringRefs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "smell": self.smell,
            "description": self.description,
            "triggering_refs": self.triggering_refs.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Hunch:
        return cls(
            smell=d["smell"],
            description=d["description"],
            triggering_refs=TriggeringRefs.from_dict(d.get("triggering_refs") or {}),
        )


def hunch_emit_record(
    hunch: Hunch,
    hunch_id: str,
    ts: str,
    emitted_by_tick: int,
) -> dict[str, Any]:
    """Format a Hunch as a `hunches.jsonl` emit event.

    See framework_v0.md Appendix A. The file is event-sourced and
    strictly append-only; this helper produces the `"type": "emit"`
    variant. Status-change events are written separately (by the
    surface / hook / framework) and have their own shape.
    """
    return {
        "type": "emit",
        "hunch_id": hunch_id,
        "ts": ts,
        "emitted_by_tick": emitted_by_tick,
        "smell": hunch.smell,
        "description": hunch.description,
        "triggering_refs": hunch.triggering_refs.to_dict(),
    }


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class Critic(Protocol):
    """The contract every Critic implementation must satisfy.

    v0 calls these methods in-process on a Python object. Future
    subprocess / remote implementations wrap the same calls over the
    stdio JSON protocol (framework_v0.md Appendix B) — the framework
    side of that wrapper is a thin translator, not a new interface.

    Methods:
      - `init(config)` — called once before the first tick. Use this
        to open the replay buffer, load prompts, warm caches, etc.
      - `tick(...)` — called per trigger firing. The Critic pulls
        whatever subset of the replay buffer it wants from `replay_dir`
        using the bookmarks as a hint for "what's new". Returns a
        (possibly empty) list of Hunches; the framework assigns ids
        and writes `hunches.jsonl`.
      - `shutdown()` — called on framework exit. Flush, close
        connections, etc.
    """

    def init(self, config: dict[str, Any]) -> None:
        ...

    def tick(
        self,
        tick_id: str,
        bookmark_prev: int,
        bookmark_now: int,
    ) -> list[Hunch]:
        ...

    def shutdown(self) -> None:
        ...
