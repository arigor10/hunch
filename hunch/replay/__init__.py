"""Offline replay driver.

Feed a historical event stream through the same Trigger + Critic the
live framework uses. Kills the live/offline skew that motivated
`docs/unified_replay_mode.md` (agentic_research_critic repo).
"""

from __future__ import annotations

from hunch.replay.driver import (
    ReplayResult,
    run_replay,
    run_replay_from_claude_log,
    run_replay_from_dir,
)
from hunch.replay.loader import TriggerEvent, load_trigger_events

__all__ = [
    "ReplayResult",
    "TriggerEvent",
    "load_trigger_events",
    "run_replay",
    "run_replay_from_claude_log",
    "run_replay_from_dir",
]
