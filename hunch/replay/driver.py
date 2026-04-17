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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hunch.capture.writer import ReplayBufferWriter
from hunch.critic.protocol import Critic, Hunch
from hunch.journal.hunches import HunchesWriter
from hunch.parse.transcript import Event, parse_whole_file
from hunch.replay.loader import load_trigger_events
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
    virtual_ticks_fired: int = 0
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
    allow_existing: bool = False,
) -> ReplayResult:
    """Drive a Critic through a pre-parsed event stream, writing a
    fresh replay buffer as we go.

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
        artifacts.jsonl, artifacts/, and hunches.jsonl — same layout
        as a live `hunch run` produces.
      critic: any `Critic` protocol implementation (stub, sonnet, …).
      trigger_config: v1 trigger knobs. Default = production cadence.
      critic_config: extra config passed to `critic.init()`. `replay_dir`
        is always injected automatically.
      on_log: optional log sink; called once per tick / purge / warning.
      max_events: cap on events consumed (for smoke tests / partial runs).
      allow_existing: if False (default), refuse when `replay_dir` already
        contains buffer data from a prior run. Appending to an existing
        buffer silently doubles events and collides hunch ids.

    Returns a `ReplayResult` summarizing the run.
    """
    cfg = trigger_config or TriggerV1Config()
    replay_dir = Path(replay_dir)
    replay_dir.mkdir(parents=True, exist_ok=True)
    _check_replay_dir_empty(replay_dir, allow_existing)

    writer = ReplayBufferWriter(replay_dir=replay_dir)
    hunches_writer = HunchesWriter(hunches_path=replay_dir / "hunches.jsonl")

    init_config = {"replay_dir": str(replay_dir), **(critic_config or {})}
    critic.init(init_config)

    state = TriggerV1State()
    result = ReplayResult()
    ctx = _Ctx(
        tick_counter=0,
        critic=critic,
        hunches_writer=hunches_writer,
        result=result,
        on_log=on_log,
    )
    last_sim_now = 0.0

    def log(msg: str) -> None:
        if on_log is not None:
            on_log(msg)

    # Events are assumed pre-sorted by caller convention. `parse_whole_file`
    # returns events in file-order (monotonic for a single Claude session).
    # Non-monotonic jumps are handled downstream by clamping sim_now.
    try:
        for i, event in enumerate(events):
            if max_events is not None and i >= max_events:
                break
            etype = event.get("type", "")
            ts_raw = event.get("timestamp", "")
            bookmark_pre_event = writer.tick_seq

            # Write first so that current_bookmark reflects the event
            # that's about to be considered for triggering.
            writer.append_events([event], project_roots)
            current_bookmark = writer.tick_seq

            state, last_sim_now = _drive_one_event(
                ctx=ctx,
                state=state,
                cfg=cfg,
                event_index=i,
                etype=etype,
                ts_raw=ts_raw,
                bookmark_pre_event=bookmark_pre_event,
                current_bookmark=current_bookmark,
                last_sim_now=last_sim_now,
            )
    finally:
        critic.shutdown()

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
    allow_existing: bool = False,
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
        allow_existing=allow_existing,
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
) -> ReplayResult:
    """Drive a Critic over an already-populated replay buffer.

    Read-only on conversation.jsonl / artifacts.jsonl / artifacts/. The
    replay buffer is whatever the live framework (or a one-shot parser
    like `scripts/parse_transcript.py`) produced — the driver here only
    reads it and appends hunches.jsonl alongside.

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

    Returns a `ReplayResult` summarizing the run.
    """
    cfg = trigger_config or TriggerV1Config()
    replay_dir = Path(replay_dir)

    trigger_events = load_trigger_events(replay_dir)

    hunches_path = replay_dir / "hunches.jsonl"
    if hunches_path.exists() and hunches_path.stat().st_size > 0:
        if overwrite_hunches:
            hunches_path.unlink()
        else:
            raise RuntimeError(
                f"{hunches_path} already exists and is non-empty; "
                "pass overwrite_hunches=True to replace it."
            )

    hunches_writer = HunchesWriter(hunches_path=hunches_path)
    init_config = {"replay_dir": str(replay_dir), **(critic_config or {})}
    critic.init(init_config)

    state = TriggerV1State()
    result = ReplayResult()
    ctx = _Ctx(
        tick_counter=0,
        critic=critic,
        hunches_writer=hunches_writer,
        result=result,
        on_log=on_log,
    )
    last_sim_now = 0.0
    # Bookmarks are already assigned in conversation.jsonl's `tick_seq`;
    # the bookmark before the very first event is 0.
    bookmark_pre_event = 0

    try:
        for i, te in enumerate(trigger_events):
            if max_events is not None and i >= max_events:
                break
            state, last_sim_now = _drive_one_event(
                ctx=ctx,
                state=state,
                cfg=cfg,
                event_index=i,
                etype=te.type,
                ts_raw=te.timestamp,
                bookmark_pre_event=bookmark_pre_event,
                current_bookmark=te.tick_seq,
                last_sim_now=last_sim_now,
            )
            bookmark_pre_event = te.tick_seq
    finally:
        critic.shutdown()

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
    result: ReplayResult
    on_log: Callable[[str], None] | None


def _drive_one_event(
    ctx: _Ctx,
    state: TriggerV1State,
    cfg: TriggerV1Config,
    event_index: int,
    etype: str,
    ts_raw: str,
    bookmark_pre_event: int,
    current_bookmark: int,
    last_sim_now: float,
) -> tuple[TriggerV1State, float]:
    """Process one event: clamp sim_now, inject virtual ticks into the gap,
    evaluate the event-driven trigger, and observe it into state.

    Shared between `run_replay` (events-in, writer-backed) and
    `run_replay_from_dir` (loaded-from-replay-dir, read-only). Both
    callers are expected to have already assigned `current_bookmark`
    (via writer.tick_seq++ or by reading the loaded event's tick_seq).
    """
    parsed_ts = _parse_ts(ts_raw)
    if parsed_ts is None:
        # Keep sim_now monotone; fall back to the previous value.
        sim_now = last_sim_now
    elif parsed_ts < last_sim_now:
        # Non-monotonic — clamp to last_sim_now to avoid negative
        # deltas in trigger math.
        ctx.result.backward_ts_warnings += 1
        if ctx.on_log is not None:
            ctx.on_log(
                f"[replay] warning: event {event_index} ts {ts_raw!r} is "
                f"before sim_now={last_sim_now}; clamping"
            )
        sim_now = last_sim_now
    else:
        sim_now = parsed_ts

    # Inject virtual ticks in the gap (last_sim_now, sim_now] — moments
    # when the live loop would have fired on silence or max_interval
    # even though no event arrived. Without this the offline cadence
    # drifts from live whenever long idle gaps fall between events.
    while True:
        vt = _next_virtual_tick_time(
            state, last_sim_now, sim_now, bookmark_pre_event, cfg,
        )
        if vt is None:
            break
        state = _fire_tick(
            ctx=ctx,
            state=state,
            now=vt,
            current_bookmark=bookmark_pre_event,
            event_index=event_index,
            ts_raw_for_record="",
            is_virtual=True,
        )
        last_sim_now = vt

    last_sim_now = sim_now
    ctx.result.events_consumed += 1

    if decide_tick_v1(state, sim_now, current_bookmark, etype, cfg):
        state = _fire_tick(
            ctx=ctx,
            state=state,
            now=sim_now,
            current_bookmark=current_bookmark,
            event_index=event_index,
            ts_raw_for_record=ts_raw,
            is_virtual=False,
        )

    state = observe_event_v1(state, etype, sim_now)
    return state, last_sim_now


def _fire_tick(
    ctx: _Ctx,
    state: TriggerV1State,
    now: float,
    current_bookmark: int,
    event_index: int,
    ts_raw_for_record: str,
    is_virtual: bool,
) -> TriggerV1State:
    """Run one critic tick, persist hunches, update result + log. Returns
    the new trigger state (with last_tick_ts / bookmark updated and
    in_flight cleared)."""
    ctx.tick_counter += 1
    tick_id = f"t-{ctx.tick_counter:04d}"
    prev_bookmark = state.last_tick_bookmark
    state = mark_tick_started_v1(state, now, current_bookmark)
    try:
        hunches = ctx.critic.tick(
            tick_id=tick_id,
            bookmark_prev=prev_bookmark,
            bookmark_now=current_bookmark,
        )
    finally:
        state = mark_tick_finished_v1(state)

    ctx.result.ticks_fired += 1
    if is_virtual:
        ctx.result.virtual_ticks_fired += 1
    _persist_hunches(hunches, ctx.hunches_writer, ts_raw_for_record, ctx.tick_counter)
    ctx.result.hunches_emitted += len(hunches)
    ctx.result.tick_log.append({
        "tick_id": tick_id,
        "sim_now": now,
        "event_index": event_index,
        "bookmark_prev": prev_bookmark,
        "bookmark_now": current_bookmark,
        "hunch_count": len(hunches),
        "virtual": is_virtual,
    })
    if ctx.on_log is not None:
        kind = "virtual " if is_virtual else ""
        ctx.on_log(
            f"[replay] {kind}{tick_id} @ event {event_index} "
            f"(bookmark {prev_bookmark}→{current_bookmark}) "
            f"hunches={len(hunches)}"
        )
    return state


def _next_virtual_tick_time(
    state: TriggerV1State,
    gap_start: float,
    gap_end: float,
    current_bookmark: int,
    cfg: TriggerV1Config,
) -> float | None:
    """Earliest moment in `(gap_start, gap_end]` at which the v1 policy
    would have fired on silence or max-interval alone (no event arrived).

    Returns the fire time, or None if no virtual tick fires in this gap.
    Candidates:
      - `last_assistant_ts + silence_s` — monologue-end timer. Also
        requires min_debounce_s elapsed since the last tick.
      - `last_tick_ts + max_interval_s` — forced fire for long runs.
        Only applies when we've already ticked at least once.

    Shared policy rules still hold: no-fire-if-in-flight, no-fire-if-no-
    new-content. Hot-event and user-text cases are event-driven, not
    time-driven, so they're absent here.
    """
    if state.in_flight:
        return None
    if current_bookmark <= state.last_tick_bookmark:
        return None

    candidates: list[float] = []

    if state.last_assistant_ts > 0:
        # Silence fires when BOTH silence_s has elapsed since the last
        # assistant utterance AND min_debounce_s has elapsed since the
        # last tick. If silence is ready but debounce isn't, the live
        # poll loop waits for debounce — so fire at max(silence, debounce).
        silence_ready = state.last_assistant_ts + cfg.silence_s
        if state.has_ticked:
            silence_fire_at = max(
                silence_ready, state.last_tick_ts + cfg.min_debounce_s,
            )
        else:
            silence_fire_at = silence_ready
        if gap_start < silence_fire_at <= gap_end:
            candidates.append(silence_fire_at)

    if state.has_ticked:
        max_time = state.last_tick_ts + cfg.max_interval_s
        if gap_start < max_time <= gap_end:
            candidates.append(max_time)

    return min(candidates) if candidates else None


def _persist_hunches(
    hunches: list[Hunch],
    writer: HunchesWriter,
    ts: str,
    tick_num: int,
) -> None:
    """Append each hunch as an emit event in hunches.jsonl, mirroring live."""
    for hunch in hunches:
        hid = writer.allocate_id()
        writer.write_emit(
            hunch=hunch,
            hunch_id=hid,
            ts=ts or _dt.datetime.now(_dt.timezone.utc).isoformat(),
            emitted_by_tick=tick_num,
        )


def _check_replay_dir_empty(replay_dir: Path, allow_existing: bool) -> None:
    """Refuse to write into a non-empty replay buffer.

    Appending to existing `conversation.jsonl` / `hunches.jsonl` silently
    doubles events and collides hunch ids. Catch that at the door. Callers
    who really want to append (not a v1 use case) set allow_existing=True.
    """
    if allow_existing:
        return
    candidates = [
        replay_dir / "conversation.jsonl",
        replay_dir / "artifacts.jsonl",
        replay_dir / "hunches.jsonl",
    ]
    for p in candidates:
        if p.exists() and p.stat().st_size > 0:
            raise RuntimeError(
                f"replay_dir {replay_dir} already contains {p.name}; "
                "refusing to clobber. Remove the directory or pass "
                "allow_existing=True."
            )
    artifacts_sub = replay_dir / "artifacts"
    if artifacts_sub.exists() and any(artifacts_sub.iterdir()):
        raise RuntimeError(
            f"replay_dir {replay_dir} already contains artifacts/; "
            "refusing to clobber. Remove the directory or pass "
            "allow_existing=True."
        )


