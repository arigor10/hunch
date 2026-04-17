"""Trigger: decides when the Critic ticks.

v0 policy (per framework_v0.md §2 Trigger):
  - Time-based. Every `interval_s` seconds (default 10s), fire a tick
    if the replay buffer has grown since the last tick.
  - If there are no new events, skip — don't bother the Critic.
  - At most one in-flight tick at a time. v0 calls Critic.tick
    synchronously from the loop thread, so that invariant is enforced
    for free; the explicit `in_flight` flag stays here so
    future-async implementations can reuse the same policy.

The policy is a pure function (`decide_tick`) so tests don't need a
real clock. `run_loop` composes the policy with a clock, a bookmark
supplier, and a Critic into a simple synchronous loop — good enough
for v0 and easy to swap for something fancier later.

Bookmarks are integers — the replay buffer's monotonic `tick_seq`.
The Trigger only needs a single number to decide "has anything
happened since last tick"; the Critic itself handles the delta read
(framework_v0.md §Invariant 3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from hunch.critic import Critic


# ---------------------------------------------------------------------------
# Policy (pure functions — easy to test)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TriggerState:
    """State the trigger carries between decisions.

    Immutable so tests can freely share references without mutation
    side effects, and so the `decide` / `mark_*` functions compose
    cleanly without in-place surprises.
    """
    last_tick_start_ts: float = 0.0
    last_tick_bookmark: int = 0
    in_flight: bool = False


def decide_tick(
    state: TriggerState,
    now: float,
    current_bookmark: int,
    interval_s: float,
) -> bool:
    """Return True iff the loop should fire a tick right now.

    Rules, in order:
      - Already in flight → no (prevent overlap).
      - Interval not yet elapsed since last tick start → no (debounce).
      - No new events since last tick → no (nothing to say).
      - Otherwise → yes.
    """
    if state.in_flight:
        return False
    if now - state.last_tick_start_ts < interval_s:
        return False
    if current_bookmark <= state.last_tick_bookmark:
        return False
    return True


def mark_tick_started(
    state: TriggerState,
    now: float,
    bookmark: int,
) -> TriggerState:
    """State update when a tick begins."""
    return replace(
        state,
        last_tick_start_ts=now,
        last_tick_bookmark=bookmark,
        in_flight=True,
    )


def mark_tick_finished(state: TriggerState) -> TriggerState:
    """State update when a tick ends (success OR failure)."""
    return replace(state, in_flight=False)


# ---------------------------------------------------------------------------
# Loop runner
# ---------------------------------------------------------------------------

@dataclass
class TriggerLoop:
    """Synchronous tick loop. Composes policy + clock + critic + bookmarks.

    Args:
      critic: the Critic to tick.
      bookmark_fn: returns the current replay-buffer tick_seq. Called
        on every iteration; must be cheap (e.g. a counter read, not a
        file scan).
      on_tick_result: optional callback invoked with the Hunch list the
        Critic returned. Wire this to the journal (HunchesWriter) when
        the framework assembles itself end-to-end.
      interval_s: minimum seconds between tick starts.
      poll_s: how often the loop wakes up to check. Must be ≤
        interval_s; smaller values give more responsive firing at the
        cost of CPU.
      clock: `time.monotonic`-compatible callable. Injectable for tests.
      sleep: `time.sleep`-compatible callable. Injectable for tests.

    Usage:
        loop = TriggerLoop(critic=c, bookmark_fn=lambda: writer.tick_seq)
        loop.run()   # blocks until `stop()` is called from another thread
    """
    critic: Critic
    bookmark_fn: Callable[[], int]
    on_tick_result: Callable[[list[Any]], None] | None = None
    interval_s: float = 10.0
    poll_s: float = 1.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    state: TriggerState = field(default_factory=TriggerState)
    _tick_counter: int = 0
    _stopped: bool = False

    def stop(self) -> None:
        """Request the loop to exit at the next iteration boundary."""
        self._stopped = True

    def step(self) -> bool:
        """One loop iteration. Returns True iff a tick actually fired.

        Separated from `run()` so tests can drive the loop
        deterministically without touching sleep / threads.
        """
        now = self.clock()
        bookmark = self.bookmark_fn()
        if not decide_tick(self.state, now, bookmark, self.interval_s):
            return False

        # Fire.
        prev_bookmark = self.state.last_tick_bookmark
        self.state = mark_tick_started(self.state, now, bookmark)
        self._tick_counter += 1
        tick_id = f"t-{self._tick_counter:04d}"

        try:
            hunches = self.critic.tick(
                tick_id=tick_id,
                bookmark_prev=prev_bookmark,
                bookmark_now=bookmark,
            )
        finally:
            self.state = mark_tick_finished(self.state)

        if self.on_tick_result is not None:
            self.on_tick_result(hunches)
        return True

    def run(self) -> None:
        """Blocking loop. Stops when `stop()` is called."""
        while not self._stopped:
            self.step()
            if self._stopped:
                break
            self.sleep(self.poll_s)


# ---------------------------------------------------------------------------
# Trigger v1 policy (shared by live + offline replay)
# ---------------------------------------------------------------------------
#
# v1 adds three knobs over v0's single interval:
#   silence_s       — fire when Claude has been quiet this long.
#   min_debounce_s  — never fire more often than this.
#   max_interval_s  — if nothing else fires, force one every this long.
# And it treats `artifact_write` / `artifact_edit` / `figure` as
# "hot events" — high-signal moments that short-circuit the silence
# wait (once min_debounce has elapsed).
#
# The design is shared across live and offline: offline feeds sim_now
# from event timestamps, live from wall clock. Same decide function.
# See docs/unified_replay_mode.md (agentic_research_critic repo) §1.


HOT_EVENT_TYPES: frozenset[str] = frozenset({
    "artifact_write",
    "artifact_edit",
    "figure",
})


@dataclass(frozen=True)
class TriggerV1Config:
    """Knobs for the v1 policy. Defaults are the production cadence
    proposed in docs/unified_replay_mode.md."""
    silence_s: float = 30.0
    min_debounce_s: float = 300.0
    max_interval_s: float = 600.0


@dataclass(frozen=True)
class TriggerV1State:
    """Immutable trigger state for v1.

    `last_assistant_ts` is the timestamp of the most recent
    `assistant_text` event the trigger has seen, used for silence
    detection. `last_tick_ts` and `last_tick_bookmark` mirror v0's
    meaning. `in_flight` exists for future-async loops.
    """
    last_tick_ts: float = 0.0
    last_tick_bookmark: int = 0
    last_assistant_ts: float = 0.0
    has_ticked: bool = False
    in_flight: bool = False


def decide_tick_v1(
    state: TriggerV1State,
    now: float,
    current_bookmark: int,
    current_event_type: str | None,
    config: TriggerV1Config,
) -> bool:
    """Return True iff a tick should fire at `now`.

    Called after appending the current event to the buffer (so
    `current_bookmark` reflects its inclusion), with the *pre-event*
    `state` — i.e. last_assistant_ts is the previous assistant_text's
    timestamp, not the current one if this event is itself assistant_text.

    Fire rules (in order of precedence — any one says yes):
      1. Max-interval override. Once min_debounce_s has elapsed, if
         `max_interval_s` has also elapsed since the last tick, fire.
         (Covers long agentic monologues where "end" never comes.)
      2. Hot event, once min_debounce_s has elapsed.
      3. Silence: elapsed since last assistant_text exceeds silence_s,
         AND current event isn't itself an assistant_text (Claude still
         talking) or a user_text (user driving). Min-debounce applies.

    Hard skips (return False regardless):
      - In flight.
      - No new content: current_bookmark <= last_tick_bookmark.
      - Current event is user_text (user utterances introduce direction,
        not work-to-critique).
    """
    if state.in_flight:
        return False
    if current_bookmark <= state.last_tick_bookmark:
        return False
    if current_event_type == "user_text":
        return False

    # Debounce since the last tick. First tick is unconstrained so the
    # policy actually fires on a fresh buffer.
    if state.has_ticked:
        dt_since_tick = max(0.0, now - state.last_tick_ts)
        if dt_since_tick < config.min_debounce_s:
            return False
        if dt_since_tick >= config.max_interval_s:
            return True
    # (If we've never ticked, dt_since_tick is effectively infinite —
    # no debounce applies, any positive fire condition suffices.)

    if current_event_type in HOT_EVENT_TYPES:
        return True

    # Silence rule only applies when the current event isn't itself an
    # assistant utterance (because that means Claude is still talking
    # — silence hasn't begun). assistant_text arrivals rely on the
    # next non-assistant event (or max_interval) to fire a tick.
    if current_event_type != "assistant_text":
        if state.last_assistant_ts > 0:
            silence_dt = max(0.0, now - state.last_assistant_ts)
            if silence_dt >= config.silence_s:
                return True

    return False


def mark_tick_started_v1(
    state: TriggerV1State,
    now: float,
    bookmark: int,
) -> TriggerV1State:
    """State update when a v1 tick begins."""
    return replace(
        state,
        last_tick_ts=now,
        last_tick_bookmark=bookmark,
        has_ticked=True,
        in_flight=True,
    )


def mark_tick_finished_v1(state: TriggerV1State) -> TriggerV1State:
    """State update when a v1 tick ends (success OR failure)."""
    return replace(state, in_flight=False)


def observe_event_v1(
    state: TriggerV1State,
    event_type: str,
    event_ts: float,
) -> TriggerV1State:
    """State update for an event appended to the buffer.

    Only `assistant_text` events advance `last_assistant_ts`; other
    types leave it untouched. Call AFTER `decide_tick_v1` so the
    silence check sees the pre-event state.
    """
    if event_type == "assistant_text":
        return replace(state, last_assistant_ts=event_ts)
    return state
