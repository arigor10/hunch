"""Tests for hunch.trigger.

Covers:
  - Policy decisions: debounce by interval, skip when no new events,
    skip when in-flight.
  - TriggerLoop.step fires the right tick ids / bookmarks and
    forwards the Critic's result to the on_tick_result callback.
  - run() exits when stop() is called (via a fake sleep that triggers
    the stop).
  - in_flight is restored even when the Critic raises.
"""

from __future__ import annotations

from typing import Any

import pytest

from hunch.critic import Hunch, StubCritic, TriggeringRefs
from hunch.trigger import (
    TriggerLoop,
    TriggerState,
    decide_tick,
    mark_tick_finished,
    mark_tick_started,
)


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------

def test_decide_tick_fires_when_interval_elapsed_and_new_events():
    s = TriggerState(last_tick_start_ts=0.0, last_tick_bookmark=5)
    assert decide_tick(s, now=10.0, current_bookmark=6, interval_s=10.0)


def test_decide_tick_skips_when_interval_not_elapsed():
    s = TriggerState(last_tick_start_ts=0.0, last_tick_bookmark=5)
    assert not decide_tick(s, now=5.0, current_bookmark=10, interval_s=10.0)


def test_decide_tick_skips_when_bookmark_unchanged():
    s = TriggerState(last_tick_start_ts=0.0, last_tick_bookmark=5)
    assert not decide_tick(s, now=100.0, current_bookmark=5, interval_s=10.0)


def test_decide_tick_skips_when_bookmark_regressed():
    # Shouldn't happen, but if the supplier glitches the policy still says no.
    s = TriggerState(last_tick_start_ts=0.0, last_tick_bookmark=5)
    assert not decide_tick(s, now=100.0, current_bookmark=3, interval_s=10.0)


def test_decide_tick_skips_when_in_flight():
    s = TriggerState(
        last_tick_start_ts=0.0, last_tick_bookmark=5, in_flight=True
    )
    assert not decide_tick(s, now=100.0, current_bookmark=10, interval_s=10.0)


def test_mark_tick_started_and_finished_transitions():
    s = TriggerState()
    s = mark_tick_started(s, now=5.0, bookmark=3)
    assert s.in_flight is True
    assert s.last_tick_start_ts == 5.0
    assert s.last_tick_bookmark == 3
    s = mark_tick_finished(s)
    assert s.in_flight is False
    assert s.last_tick_bookmark == 3  # preserved


# ---------------------------------------------------------------------------
# TriggerLoop.step
# ---------------------------------------------------------------------------

class _FakeClock:
    """Injectable clock — tests advance time explicitly."""
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def test_step_does_not_fire_before_interval(tmp_path):
    clock = _FakeClock(start=0.0)
    bookmark = {"v": 0}
    results: list[Any] = []

    c = StubCritic()
    c.init({})
    loop = TriggerLoop(
        critic=c,
        bookmark_fn=lambda: bookmark["v"],
        on_tick_result=lambda hs, bp, bn: results.append((hs, bp, bn)),
        interval_s=10.0,
        clock=clock,
        sleep=lambda _: None,
    )
    bookmark["v"] = 5
    clock.advance(1.0)
    assert loop.step() is False
    assert c.tick_log == []
    assert results == []


def test_step_does_not_fire_without_new_events():
    clock = _FakeClock(start=100.0)
    bookmark = {"v": 0}
    c = StubCritic()
    c.init({})
    loop = TriggerLoop(
        critic=c,
        bookmark_fn=lambda: bookmark["v"],
        interval_s=10.0,
        clock=clock,
        sleep=lambda _: None,
    )
    # Interval elapsed, but bookmark is 0 == last_tick_bookmark.
    assert loop.step() is False
    assert c.tick_log == []


def test_step_fires_with_right_bookmarks_and_tick_id():
    clock = _FakeClock(start=0.0)
    bookmark = {"v": 0}
    results: list[Any] = []

    c = StubCritic()
    c.init({})
    loop = TriggerLoop(
        critic=c,
        bookmark_fn=lambda: bookmark["v"],
        on_tick_result=lambda hs, bp, bn: results.append((hs, bp, bn)),
        interval_s=10.0,
        clock=clock,
        sleep=lambda _: None,
    )
    # First tick: now=11, bookmark=3 → fire.
    clock.advance(11.0)
    bookmark["v"] = 3
    assert loop.step() is True
    assert c.tick_log[-1] == {
        "tick_id": "t-0001",
        "bookmark_prev": 0,
        "bookmark_now": 3,
    }
    # StubCritic returns no hunches; callback sees bookmarks anyway.
    assert results == [([], 0, 3)]

    # Second tick: advance more, bookmark grows.
    clock.advance(11.0)
    bookmark["v"] = 8
    assert loop.step() is True
    assert c.tick_log[-1] == {
        "tick_id": "t-0002",
        "bookmark_prev": 3,
        "bookmark_now": 8,
    }
    assert results[-1] == ([], 3, 8)


def test_step_clears_in_flight_even_if_critic_raises():
    clock = _FakeClock(start=100.0)

    class _BoomCritic:
        def init(self, config): pass
        def tick(self, tick_id, bookmark_prev, bookmark_now):
            raise RuntimeError("boom")
        def shutdown(self): pass

    loop = TriggerLoop(
        critic=_BoomCritic(),
        bookmark_fn=lambda: 5,
        interval_s=10.0,
        clock=clock,
        sleep=lambda _: None,
    )
    with pytest.raises(RuntimeError, match="boom"):
        loop.step()
    # in_flight must be cleared so the next step can proceed.
    assert loop.state.in_flight is False


def test_step_forwards_hunches_to_callback():
    clock = _FakeClock(start=100.0)
    hunches = [
        Hunch(smell="s", description="d", triggering_refs=TriggeringRefs()),
    ]

    class _HunchCritic:
        def init(self, config): pass
        def tick(self, tick_id, bookmark_prev, bookmark_now):
            return list(hunches)
        def shutdown(self): pass

    received: list[Any] = []
    loop = TriggerLoop(
        critic=_HunchCritic(),
        bookmark_fn=lambda: 5,
        on_tick_result=lambda hs, bp, bn: received.append((hs, bp, bn)),
        interval_s=10.0,
        clock=clock,
        sleep=lambda _: None,
    )
    assert loop.step() is True
    # Callback gets the hunch list AND the bookmark window — offline
    # evaluators rely on both to pull the Critic's context slice.
    assert received == [(hunches, 0, 5)]


# ---------------------------------------------------------------------------
# TriggerLoop.run + stop()
# ---------------------------------------------------------------------------

def test_run_exits_when_stopped():
    clock = _FakeClock(start=0.0)
    bookmark = {"v": 0}
    c = StubCritic()
    c.init({})

    # Build a sleep that advances the clock and stops after N calls.
    call_count = {"n": 0}
    loop_holder: dict[str, TriggerLoop] = {}

    def fake_sleep(s: float) -> None:
        clock.advance(s)
        call_count["n"] += 1
        bookmark["v"] += 1
        if call_count["n"] >= 3:
            loop_holder["loop"].stop()

    loop = TriggerLoop(
        critic=c,
        bookmark_fn=lambda: bookmark["v"],
        interval_s=0.0,  # fire every chance we get
        poll_s=1.0,
        clock=clock,
        sleep=fake_sleep,
    )
    loop_holder["loop"] = loop
    loop.run()

    # We should have fired a handful of ticks before stop().
    assert len(c.tick_log) >= 1
