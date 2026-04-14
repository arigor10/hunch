"""Capture: writes the replay buffer from the live transcript.

The replay buffer is Hunch's source of truth for the Critic's input.
Capture owns the write side: it polls the Claude Code transcript,
parses new events, snapshots artifact contents into a stable location,
and appends to the append-only JSONL files.

See `docs/framework_v0.md` §1 for the contract and the schemas in
Appendix A for the file layout.
"""

from hunch.capture.writer import ReplayBufferWriter, poll_once

__all__ = ["ReplayBufferWriter", "poll_once"]
