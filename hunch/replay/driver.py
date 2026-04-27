"""Offline replay driver.

Reads a historical Claude log (or a pre-parsed event stream), advances
a simulated clock through event timestamps, and feeds events through
the same Trigger v1 + Critic the live framework uses.

Unifies live and offline paths: `hunch run` polls Claude in real time
and feeds events into ReplayBufferWriter; this driver reads events
from a historical source and feeds them into the same writer, with
`sim_now` driven off event timestamps instead of wall clock. Same
Trigger policy, same Critic, same hunches.jsonl output.

Not a CLI on its own — wired up through `hunch replay-offline`.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.capture.writer import ReplayBufferWriter
from hunch.checkpoint import (
    CHECKPOINT_FILENAME,
    checkpoint_from_trigger_state,
    read_checkpoint,
    trigger_state_from_checkpoint,
    write_checkpoint,
)
from hunch.critic.protocol import Critic, Hunch
from hunch.filter import HunchFilter
from hunch.journal.hunches import HunchesWriter
from hunch.parse.transcript import Event, parse_whole_file
from hunch.replay.loader import load_trigger_events, synthesize_claude_stopped
from hunch.trigger import (
    TriggerV1Config,
    TriggerV1State,
    decide_tick_v1,
    mark_tick_finished_v1,
    mark_tick_started_v1,
    observe_event_v1,
)


# ---------------------------------------------------------------------------
# Timestamp parsing (graceful: bad timestamps clamp to last-known sim_now)
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> float | None:
    """Parse an ISO-8601 timestamp into epoch seconds, or None on failure.

    Accepts both `...Z` and `...+00:00` tails. `parse_whole_file` emits
    the source transcript's own strings, which are ISO-8601 UTC in
    practice but we guard for malformed strings anyway.
    """
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

@dataclass
class ReplayResult:
    events_consumed: int = 0
    ticks_fired: int = 0
    hunches_emitted: int = 0
    tick_log: list[dict[str, Any]] = field(default_factory=list)
    backward_ts_warnings: int = 0


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def run_replay(
    events: list[Event],
    project_roots: list[str],
    replay_dir: Path,
    critic: Critic,
    trigger_config: TriggerV1Config | None = None,
    critic_config: dict[str, Any] | None = None,
    on_log: Callable[[str], None] | None = None,
    max_events: int | None = None,
    hunch_filter: HunchFilter | None = None,
    output_dir: Path | None = None,
    min_tick_interval_s: float = 0.0,
) -> ReplayResult:
    """Drive a Critic through a pre-parsed event stream.

    Two-phase operation:
      1. **Ingest** — write all events to the replay buffer (fast, no
         critic calls). Produces conversation.jsonl, artifacts.jsonl,
         and artifact snapshots.
      2. **Evaluate** — drive the critic over the populated replay
         buffer via `run_replay_from_dir`.

    This separation ensures the replay buffer is complete before the
    first critic call, so the critic always sees full context.

    Most callers should prefer `run_replay_from_claude_log` (parses a
    raw Claude .jsonl first) or `run_replay_from_dir` (drives over an
    already-populated replay buffer). This entry point exists for
    synthetic event sources — tests, custom parsers, the rare case
    where events come from somewhere other than a Claude log.

    Args:
      events: flat, timestamp-ordered list of parser events.
      project_roots: absolute paths to the project root(s), for
        artifact path normalization in the replay buffer.
      replay_dir: output directory. Populated with conversation.jsonl,
        artifacts.jsonl, artifacts/ — same layout as a live `hunch run`
        produces. Must be empty (refuses if it already contains data
        from a prior run).
      critic: any `Critic` protocol implementation (stub, sonnet, …).
      trigger_config: v1 trigger knobs. Default = production cadence.
      critic_config: extra config passed to `critic.init()`. `replay_dir`
        is always injected automatically.
      on_log: optional log sink; called once per tick / purge / warning.
      max_events: cap on events consumed (for smoke tests / partial runs).
      output_dir: if set, write hunches.jsonl here instead of replay_dir.
      min_tick_interval_s: minimum wall-clock seconds between ticks.

    Returns a `ReplayResult` summarizing the run.
    """
    replay_dir = Path(replay_dir)
    replay_dir.mkdir(parents=True, exist_ok=True)
    _check_replay_dir_empty(replay_dir)

    # Phase 1: Ingest all events into the replay buffer.
    writer = ReplayBufferWriter(replay_dir=replay_dir)
    for event in events:
        writer.append_events([event], project_roots)
    writer.conversation_path.touch(exist_ok=True)

    if on_log is not None:
        on_log(f"[replay] ingested {len(events)} events into {replay_dir}")

    # Phase 2: Evaluate via the from-dir driver.
    result = run_replay_from_dir(
        replay_dir=replay_dir,
        critic=critic,
        trigger_config=trigger_config,
        critic_config=critic_config,
        on_log=on_log,
        max_events=max_events,
        hunch_filter=hunch_filter,
        output_dir=output_dir,
        min_tick_interval_s=min_tick_interval_s,
    )

    # Clean up eval checkpoint — run_replay is one-shot; callers that
    # need resume use run_replay_from_dir directly.
    cp_dir = Path(output_dir) if output_dir is not None else replay_dir
    (cp_dir / CHECKPOINT_FILENAME).unlink(missing_ok=True)

    return result


# ---------------------------------------------------------------------------
# Convenience wrapper: from a Claude log (parse + drive in one go)
# ---------------------------------------------------------------------------

def run_replay_from_claude_log(
    claude_log: Path,
    replay_dir: Path,
    critic: Critic,
    trigger_config: TriggerV1Config | None = None,
    critic_config: dict[str, Any] | None = None,
    on_log: Callable[[str], None] | None = None,
    max_events: int | None = None,
    hunch_filter: HunchFilter | None = None,
    output_dir: Path | None = None,
    min_tick_interval_s: float = 0.0,
) -> ReplayResult:
    """Parse a Claude Code `.jsonl` transcript and drive a replay from it."""
    events, project_roots = parse_whole_file(claude_log)
    return run_replay(
        events=events,
        project_roots=project_roots,
        replay_dir=replay_dir,
        critic=critic,
        trigger_config=trigger_config,
        critic_config=critic_config,
        on_log=on_log,
        max_events=max_events,
        hunch_filter=hunch_filter,
        output_dir=output_dir,
        min_tick_interval_s=min_tick_interval_s,
    )


# ---------------------------------------------------------------------------
# Drive from an already-populated replay dir (no re-parse, no re-write)
# ---------------------------------------------------------------------------

def run_replay_from_dir(
    replay_dir: Path,
    critic: Critic,
    trigger_config: TriggerV1Config | None = None,
    critic_config: dict[str, Any] | None = None,
    on_log: Callable[[str], None] | None = None,
    max_events: int | None = None,
    overwrite_hunches: bool = False,
    hunch_filter: HunchFilter | None = None,
    output_dir: Path | None = None,
    min_tick_interval_s: float = 0.0,
) -> ReplayResult:
    """Drive a Critic over an already-populated replay buffer.

    Read-only on conversation.jsonl / artifacts.jsonl / artifacts/. The
    replay buffer is whatever the live framework (or a one-shot parser
    like `scripts/parse_transcript.py`) produced — the driver here only
    reads it and writes hunches.jsonl to output_dir (or replay_dir if
    output_dir is not set).

    Args:
      replay_dir: an existing `.hunch/replay/`-style directory.
      critic: any `Critic` protocol implementation.
      trigger_config: v1 trigger knobs. Default = production cadence.
      critic_config: extra config passed to `critic.init()`. `replay_dir`
        is always injected automatically.
      on_log: optional log sink; called once per tick / warning.
      max_events: cap on events consumed (for smoke tests / partial runs).
      overwrite_hunches: if True, delete any existing hunches.jsonl before
        starting. Default False — refuses if a populated hunches.jsonl is
        already present, so a bad re-run doesn't silently duplicate.
      output_dir: if set, write hunches.jsonl here instead of replay_dir.
      min_tick_interval_s: minimum wall-clock seconds between ticks.
        If a tick finishes faster, the driver sleeps the remainder.
        Useful for rate-limiting API calls on quota-limited accounts.

    Returns a `ReplayResult` summarizing the run.
    """
    cfg = trigger_config or TriggerV1Config()
    replay_dir = Path(replay_dir)

    trigger_events = load_trigger_events(replay_dir)
    trigger_events = synthesize_claude_stopped(trigger_events)

    hunches_dir = Path(output_dir) if output_dir is not None else replay_dir
    hunches_dir.mkdir(parents=True, exist_ok=True)
    hunches_path = hunches_dir / "hunches.jsonl"

    cp_path = hunches_dir / CHECKPOINT_FILENAME
    cp = read_checkpoint(cp_path)

    start_index = 0
    state = TriggerV1State()
    result = ReplayResult()
    last_sim_now = 0.0
    bookmark_pre_event = 0
    tick_counter_init = 0

    if cp is not None:
        start_index = cp.events_consumed
        state = trigger_state_from_checkpoint(cp)
        result.ticks_fired = cp.ticks_fired
        result.hunches_emitted = cp.hunches_emitted
        tick_counter_init = cp.tick_counter
        last_sim_now = cp.last_sim_now
        bookmark_pre_event = cp.bookmark_pre_event
        if on_log:
            on_log(
                f"[replay] resuming from checkpoint: "
                f"events={cp.events_consumed} ticks={cp.ticks_fired}"
            )
    elif hunches_path.exists() and hunches_path.stat().st_size > 0:
        if overwrite_hunches:
            hunches_path.unlink()
        else:
            raise RuntimeError(
                f"{hunches_path} already exists and is non-empty, "
                f"but no checkpoint.json found for resume. "
                f"To start fresh, delete it:  rm {hunches_path}"
            )

    init_config = {"replay_dir": str(replay_dir), **(critic_config or {})}
    critic.init(init_config)
    hunches_writer = HunchesWriter(hunches_path=hunches_path)

    ctx = _Ctx(
        tick_counter=tick_counter_init,
        critic=critic,
        hunches_writer=hunches_writer,
        hunch_filter=hunch_filter,
        result=result,
        on_log=on_log,
        min_tick_interval_s=min_tick_interval_s,
    )

    absolute_index = start_index
    events_this_run = 0
    try:
        for i, te in enumerate(trigger_events):
            if i < start_index:
                continue
            if max_events is not None and events_this_run >= max_events:
                break
            ticks_before = result.ticks_fired
            state, last_sim_now = _drive_one_event(
                ctx=ctx,
                state=state,
                cfg=cfg,
                event_index=i,
                etype=te.type,
                ts_raw=te.timestamp,
                bookmark_pre_event=bookmark_pre_event,
                bookmark_now=te.tick_seq,
                last_sim_now=last_sim_now,
            )
            bookmark_pre_event = te.tick_seq
            absolute_index = i + 1
            events_this_run += 1
            if result.ticks_fired > ticks_before:
                write_checkpoint(cp_path, checkpoint_from_trigger_state(
                    state,
                    events_consumed=absolute_index,
                    ticks_fired=result.ticks_fired,
                    hunches_emitted=result.hunches_emitted,
                    tick_counter=ctx.tick_counter,
                    last_sim_now=last_sim_now,
                    bookmark_pre_event=bookmark_pre_event,
                ))
    finally:
        critic.shutdown()
        write_checkpoint(cp_path, checkpoint_from_trigger_state(
            state,
            events_consumed=absolute_index,
            ticks_fired=result.ticks_fired,
            hunches_emitted=result.hunches_emitted,
            tick_counter=ctx.tick_counter,
            last_sim_now=last_sim_now,
            bookmark_pre_event=bookmark_pre_event,
        ))

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    """Mutable state passed to fire helpers (keeps signatures short)."""
    tick_counter: int
    critic: Critic
    hunches_writer: HunchesWriter
    hunch_filter: HunchFilter | None
    result: ReplayResult
    on_log: Callable[[str], None] | None
    min_tick_interval_s: float = 0.0


def _drive_one_event(
    ctx: _Ctx,
    state: TriggerV1State,
    cfg: TriggerV1Config,
    event_index: int,
    etype: str,
    ts_raw: str,
    bookmark_pre_event: int,
    bookmark_now: int,
    last_sim_now: float,
) -> tuple[TriggerV1State, float]:
    """Process one event: clamp sim_now, evaluate the trigger, observe
    into state.

    Both callers are expected to have already assigned `bookmark_now`
    (via writer.tick_seq++ or by reading the loaded event's tick_seq).
    """
    parsed_ts = _parse_ts(ts_raw)
    if parsed_ts is None:
        sim_now = last_sim_now
    elif parsed_ts < last_sim_now:
        ctx.result.backward_ts_warnings += 1
        if ctx.on_log is not None:
            ctx.on_log(
                f"[replay] warning: event {event_index} ts {ts_raw!r} is "
                f"before sim_now={last_sim_now}; clamping"
            )
        sim_now = last_sim_now
    else:
        sim_now = parsed_ts

    last_sim_now = sim_now
    ctx.result.events_consumed += 1

    fire = decide_tick_v1(state, sim_now, bookmark_now, etype, cfg)
    if fire is not None:
        state = _fire_tick(
            ctx=ctx,
            state=state,
            now=sim_now,
            bookmark_now=bookmark_now,
            event_index=event_index,
            ts_raw_for_record=ts_raw,
        )

    state = observe_event_v1(state, etype, sim_now)
    return state, last_sim_now


def _fire_tick(
    ctx: _Ctx,
    state: TriggerV1State,
    now: float,
    bookmark_now: int,
    event_index: int,
    ts_raw_for_record: str,
) -> TriggerV1State:
    """Run one critic tick, persist hunches, update result + log. Returns
    the new trigger state (with last_tick_ts / bookmark updated and
    in_flight cleared)."""
    ctx.tick_counter += 1
    tick_id = f"t-{ctx.tick_counter:04d}"
    bookmark_prev = state.last_tick_bookmark
    state = mark_tick_started_v1(state, now, bookmark_now)
    t0 = _time.monotonic()
    try:
        hunches = ctx.critic.tick(
            tick_id=tick_id,
            bookmark_prev=bookmark_prev,
            bookmark_now=bookmark_now,
        )
    finally:
        state = mark_tick_finished_v1(state)
    elapsed = _time.monotonic() - t0

    ctx.result.ticks_fired += 1
    emitted = _persist_hunches(
        hunches, ctx.hunches_writer, ts_raw_for_record, ctx.tick_counter,
        bookmark_prev=bookmark_prev, bookmark_now=bookmark_now,
        hunch_filter=ctx.hunch_filter,
    )
    ctx.result.hunches_emitted += emitted
    ctx.result.tick_log.append({
        "tick_id": tick_id,
        "sim_now": now,
        "event_index": event_index,
        "bookmark_prev": bookmark_prev,
        "bookmark_now": bookmark_now,
        "hunch_count": len(hunches),
        "elapsed_s": round(elapsed, 1),
    })
    if ctx.on_log is not None:
        ctx.on_log(
            f"[replay] {tick_id} @ event {event_index} "
            f"(bookmark {bookmark_prev}→{bookmark_now}) "
            f"hunches={len(hunches)} ({elapsed:.1f}s)"
        )

    if ctx.min_tick_interval_s > 0:
        sleep_s = ctx.min_tick_interval_s - elapsed
        if sleep_s > 0:
            if ctx.on_log is not None:
                ctx.on_log(f"[rate-limit] sleeping {sleep_s:.0f}s")
            _time.sleep(sleep_s)

    return state



def _persist_hunches(
    hunches: list[Hunch],
    writer: HunchesWriter,
    ts: str,
    tick_num: int,
    *,
    bookmark_prev: int,
    bookmark_now: int,
    hunch_filter: HunchFilter | None = None,
) -> int:
    """Append hunches to hunches.jsonl, applying filter if provided.

    Returns the number of hunches that passed the filter (emitted)."""
    resolved_ts = ts or _dt.datetime.now(_dt.timezone.utc).isoformat()

    hunch_ids = [writer.allocate_id() for _ in hunches]

    if hunch_filter is not None:
        results = hunch_filter.filter_batch(
            hunches, bookmark_prev, bookmark_now, hunch_ids=hunch_ids,
        )
    else:
        from hunch.filter import FilterResult
        results = [FilterResult(hunch=h, passed=True) for h in hunches]

    emitted = 0
    for fr, hid in zip(results, hunch_ids):
        if fr.passed:
            writer.write_emit(
                hunch=fr.hunch,
                hunch_id=hid,
                ts=resolved_ts,
                emitted_by_tick=tick_num,
                bookmark_prev=bookmark_prev,
                bookmark_now=bookmark_now,
            )
            emitted += 1
        else:
            writer.write_filtered(
                hunch=fr.hunch,
                hunch_id=hid,
                ts=resolved_ts,
                emitted_by_tick=tick_num,
                bookmark_prev=bookmark_prev,
                bookmark_now=bookmark_now,
                filter_type=fr.filter_type,
                filter_reason=fr.reason,
                duplicate_of=fr.duplicate_of,
            )
    return emitted


def _check_replay_dir_empty(replay_dir: Path) -> None:
    """Refuse to write into a non-empty replay buffer.

    Appending to existing `conversation.jsonl` / `hunches.jsonl` silently
    doubles events and collides hunch ids. Catch that at the door.
    """
    candidates = [
        replay_dir / "conversation.jsonl",
        replay_dir / "artifacts.jsonl",
        replay_dir / "hunches.jsonl",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            raise RuntimeError(
                f"replay_dir {replay_dir} already contains {p.name}; "
                "refusing to clobber. Remove the directory first."
            )
    artifacts_sub = replay_dir / "artifacts"
    if artifacts_sub.exists() and any(artifacts_sub.iterdir()):
        raise RuntimeError(
            f"replay_dir {replay_dir} already contains artifacts/; "
            "refusing to clobber. Remove the directory first."
        )


