"""Critic: the black box behind the Critic protocol.

The framework speaks to the Critic through a narrow interface (see
`protocol.py`). v0 runs the Critic in-process as a Python object;
future versions may swap in a subprocess or remote agent without the
framework caring — the wire format is defined in
`docs/framework_v0.md` Appendix B.

This package exposes:
  - `Critic` — the abstract interface the framework calls against.
  - `Hunch`, `TriggeringRefs` — the value shapes that flow back.
  - `StubCritic` — a no-op implementation for testing and CI.
"""

from hunch.critic.protocol import (
    Critic,
    Hunch,
    TriggeringRefs,
    hunch_emit_record,
)
from hunch.critic.stub import StubCritic

__all__ = [
    "Critic",
    "Hunch",
    "TriggeringRefs",
    "StubCritic",
    "hunch_emit_record",
]
