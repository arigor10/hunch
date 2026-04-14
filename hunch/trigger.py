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
