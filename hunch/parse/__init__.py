"""Transcript parsing: Claude Code .jsonl → event stream.

Designed for both offline (whole-file) and online (incremental poll) use.
The online framework polls a live transcript; the offline miner reads a
completed session. Both go through the same primitives.
"""

from hunch.parse.transcript import (
    Event,
    ParserState,
    poll_new_events,
    parse_whole_file,
    detect_project_roots,
)

__all__ = [
    "Event",
    "ParserState",
    "poll_new_events",
    "parse_whole_file",
    "detect_project_roots",
]
