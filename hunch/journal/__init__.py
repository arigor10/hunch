"""Journal: the hunches + feedback append-only logs.

`.hunch/replay/hunches.jsonl` and `.hunch/replay/feedback.jsonl` are
event-sourced JSONL files, strictly append-only, read by the side
panel, the UserPromptSubmit hook, future agentic critics, and any
downstream analytics. This package owns the write side (emit events,
status-change events, feedback events) and the fold-on-read side
(current hunch state derived by replaying events in timestamp order).

See `docs/framework_v0.md` §Invariant 4 and Appendix A for the
contract this module implements.
"""

from hunch.journal.append import append_json_line
from hunch.journal.hunches import (
    HunchRecord,
    HunchesWriter,
    read_current_hunches,
)
from hunch.journal.feedback import FeedbackWriter

__all__ = [
    "HunchRecord",
    "HunchesWriter",
    "FeedbackWriter",
    "append_json_line",
    "read_current_hunches",
]
