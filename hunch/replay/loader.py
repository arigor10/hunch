"""Load trigger events from an already-populated replay buffer.

The offline driver has two front doors:

  1. Raw Claude log → parse → write replay buffer → drive critic.
     (`run_replay_from_claude_log`)
  2. Existing replay buffer → load events → drive critic, read-only
     on conversation.jsonl / artifacts.jsonl / artifacts/.
     (`run_replay_from_dir`, which uses this loader.)

The live framework and `scripts/parse_transcript.py` (in the critic
repo) both populate the replay buffer. Door #2 lets us run the critic
over any session that's been parsed — repeatedly, with different
prompts/configs — without re-doing the parse work.

Why not reuse the full parser event schema? Triggering and bookmarking
only need `type` + `timestamp` + `tick_seq`. The Critic itself reads
the full artifact content from the replay dir directly via its
`replay_dir` config, so the driver never needs to re-hydrate content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TriggerEvent:
    """Minimal event slice needed by the trigger + driver.

    `tick_seq` is the bookmark from conversation.jsonl — the same
    monotonic id `ReplayBufferWriter` assigns. The Critic uses it to
    request windows of the conversation on each tick.
    """
    tick_seq: int
    type: str
    timestamp: str


def load_trigger_events(replay_dir: Path) -> list[TriggerEvent]:
    """Read conversation.jsonl and return a tick_seq-ordered list of
    TriggerEvents.

    Raises:
      FileNotFoundError: if conversation.jsonl is missing.
      ValueError: if an entry is missing tick_seq / type / timestamp,
        or if tick_seq is non-monotonic (indicates a corrupted buffer).
    """
    path = Path(replay_dir) / "conversation.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"replay_dir {replay_dir} has no conversation.jsonl; "
            "populate it first via `hunch run` or "
            "`scripts/parse_transcript.py`"
        )

    events: list[TriggerEvent] = []
    last_tick_seq = 0
    with path.open() as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path}:{line_num}: invalid JSON — {e}"
                ) from e
            try:
                tick_seq = int(entry["tick_seq"])
                etype = str(entry["type"])
                ts = str(entry["timestamp"])
            except KeyError as e:
                raise ValueError(
                    f"{path}:{line_num}: missing required field {e}"
                ) from e
            if tick_seq <= last_tick_seq:
                raise ValueError(
                    f"{path}:{line_num}: tick_seq={tick_seq} is not "
                    f"strictly greater than previous {last_tick_seq}"
                )
            events.append(TriggerEvent(tick_seq=tick_seq, type=etype, timestamp=ts))
            last_tick_seq = tick_seq

    return events
