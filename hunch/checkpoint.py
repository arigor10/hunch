"""Checkpoint: resumable state for both online and offline pipelines.

After each Critic tick, the framework writes a checkpoint recording how
far the pipeline has progressed.  On restart, it resumes from the
checkpoint rather than reprocessing the entire transcript.

The same file format is used by ``hunch run`` (online, checkpoint in
``.hunch/replay/``) and ``hunch replay-offline`` (offline, checkpoint
in ``--output-dir``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from hunch.trigger import TriggerV1State

CHECKPOINT_VERSION = 1
CHECKPOINT_FILENAME = "checkpoint.json"


@dataclass
class Checkpoint:
    version: int = CHECKPOINT_VERSION
    events_consumed: int = 0
    ticks_fired: int = 0
    hunches_emitted: int = 0
    tick_counter: int = 0
    last_tick_ts: float = 0.0
    last_tick_bookmark: int = 0
    has_ticked: bool = False
    last_sim_now: float = 0.0
    bookmark_pre_event: int = 0
    parser_line_offset: int = 0
    writer_tick_seq: int = 0
    hook_bookmark: int = 0


def write_checkpoint(path: Path, cp: Checkpoint) -> None:
    """Atomically write checkpoint via tmp+rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cp), indent=2) + "\n")
    tmp.rename(path)


def read_checkpoint(path: Path) -> Checkpoint | None:
    """Read checkpoint from disk.  Returns None if absent or malformed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        if data.get("version") != CHECKPOINT_VERSION:
            return None
        fields = {k: data[k] for k in Checkpoint.__dataclass_fields__ if k in data}
        return Checkpoint(**fields)
    except (json.JSONDecodeError, TypeError, KeyError):
        return None


def trigger_state_from_checkpoint(cp: Checkpoint) -> TriggerV1State:
    """Reconstruct TriggerV1State from a checkpoint."""
    return TriggerV1State(
        last_tick_ts=cp.last_tick_ts,
        last_tick_bookmark=cp.last_tick_bookmark,
        has_ticked=cp.has_ticked,
    )


def checkpoint_from_trigger_state(
    trigger_state: TriggerV1State,
    *,
    events_consumed: int = 0,
    ticks_fired: int = 0,
    hunches_emitted: int = 0,
    tick_counter: int = 0,
    last_sim_now: float = 0.0,
    bookmark_pre_event: int = 0,
    parser_line_offset: int = 0,
    writer_tick_seq: int = 0,
    hook_bookmark: int = 0,
) -> Checkpoint:
    """Build a Checkpoint from current pipeline state."""
    return Checkpoint(
        events_consumed=events_consumed,
        ticks_fired=ticks_fired,
        hunches_emitted=hunches_emitted,
        tick_counter=tick_counter,
        last_tick_ts=trigger_state.last_tick_ts,
        last_tick_bookmark=trigger_state.last_tick_bookmark,
        has_ticked=trigger_state.has_ticked,
        last_sim_now=last_sim_now,
        bookmark_pre_event=bookmark_pre_event,
        parser_line_offset=parser_line_offset,
        writer_tick_seq=writer_tick_seq,
        hook_bookmark=hook_bookmark,
    )
