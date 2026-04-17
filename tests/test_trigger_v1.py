"""Tests for Trigger v1 policy (silence + debounce + max-interval + hot).

See hunch/trigger.py — decide_tick_v1, TriggerV1State, TriggerV1Config.
"""

from __future__ import annotations

from hunch.trigger import (
    TriggerV1Config,
    TriggerV1State,
    decide_tick_v1,
    mark_tick_finished_v1,
    mark_tick_started_v1,
    observe_event_v1,
)


CFG = TriggerV1Config(silence_s=30.0, min_debounce_s=300.0, max_interval_s=600.0)


# ---------------------------------------------------------------------------
# Hard skips
# ---------------------------------------------------------------------------

def test_no_fire_if_in_flight():
    state = TriggerV1State(in_flight=True)
    assert not decide_tick_v1(state, now=1000.0, current_bookmark=5,
                              current_event_type="figure", config=CFG)


def test_no_fire_if_no_growth():
    state = TriggerV1State(last_tick_bookmark=5, has_ticked=True)
    assert not decide_tick_v1(state, now=1e6, current_bookmark=5,
                              current_event_type="figure", config=CFG)


def test_no_fire_on_user_text():
    state = TriggerV1State(last_assistant_ts=0.0)  # first tick, no debounce
    assert not decide_tick_v1(state, now=5000.0, current_bookmark=10,
                              current_event_type="user_text", config=CFG)


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

def test_debounce_blocks_hot_event_too_soon():
    state = TriggerV1State(
        last_tick_ts=100.0, last_tick_bookmark=3, has_ticked=True,
    )
    # Hot event at now=200 (100s after last tick), min_debounce=300 → no
    assert not decide_tick_v1(state, now=200.0, current_bookmark=5,
                              current_event_type="artifact_write", config=CFG)


def test_first_tick_not_debounced():
    # has_ticked=False → never fired → no debounce; hot event fires.
    state = TriggerV1State()
    assert decide_tick_v1(state, now=50.0, current_bookmark=3,
                          current_event_type="artifact_write", config=CFG)


# ---------------------------------------------------------------------------
# Fire conditions
# ---------------------------------------------------------------------------

def test_hot_event_fires_once_debounce_elapsed():
    state = TriggerV1State(
        last_tick_ts=0.0, last_tick_bookmark=1, has_ticked=True,
    )
    assert decide_tick_v1(state, now=400.0, current_bookmark=2,
                          current_event_type="artifact_write", config=CFG)
    assert decide_tick_v1(state, now=400.0, current_bookmark=2,
                          current_event_type="artifact_edit", config=CFG)
    assert decide_tick_v1(state, now=400.0, current_bookmark=2,
                          current_event_type="figure", config=CFG)


def test_max_interval_forces_fire_even_without_hot_or_silence():
    # assistant is still talking (silence hasn't elapsed), no hot event,
    # but max_interval has elapsed since last tick → must fire.
    state = TriggerV1State(
        last_tick_ts=0.0,
        last_tick_bookmark=1,
        last_assistant_ts=590.0,  # silence < 30s at now=600
        has_ticked=True,
    )
    assert decide_tick_v1(state, now=601.0, current_bookmark=5,
                          current_event_type="assistant_text", config=CFG)


def test_silence_fires_on_non_assistant_event():
    # Previous assistant spoke at t=50, now it's t=400 (350s silence),
    # min_debounce elapsed, current event is a tool_error (not hot,
    # not assistant, not user).
    state = TriggerV1State(
        last_tick_ts=50.0,
        last_tick_bookmark=1,
        last_assistant_ts=50.0,
        has_ticked=True,
    )
    assert decide_tick_v1(state, now=400.0, current_bookmark=5,
                          current_event_type="tool_error", config=CFG)


def test_silence_does_not_fire_on_fresh_assistant_text():
    # Claude is still talking — the current event is assistant_text.
    # Even if the previous assistant event was >30s ago, we shouldn't
    # treat THIS as the silence boundary.
    state = TriggerV1State(
        last_tick_ts=0.0,
        last_tick_bookmark=1,
        last_assistant_ts=100.0,
        has_ticked=True,
    )
    # 400s after last tick (debounce OK), 300s after last assistant (silence
    # trigger would say yes), but we should NOT fire because current event
    # is assistant_text — still talking.
    assert not decide_tick_v1(state, now=400.0, current_bookmark=5,
                              current_event_type="assistant_text", config=CFG)


def test_silence_not_yet_elapsed():
    # Previous assistant at t=380, now t=400 → only 20s of silence.
    state = TriggerV1State(
        last_tick_ts=0.0,
        last_tick_bookmark=1,
        last_assistant_ts=380.0,
        has_ticked=True,
    )
    assert not decide_tick_v1(state, now=400.0, current_bookmark=5,
                              current_event_type="tool_error", config=CFG)


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def test_mark_tick_started_sets_fields():
    s = TriggerV1State()
    s = mark_tick_started_v1(s, now=42.0, bookmark=7)
    assert s.last_tick_ts == 42.0
    assert s.last_tick_bookmark == 7
    assert s.has_ticked is True
    assert s.in_flight is True


def test_mark_tick_finished_clears_in_flight():
    s = TriggerV1State(in_flight=True, last_tick_bookmark=7)
    s = mark_tick_finished_v1(s)
    assert s.in_flight is False
    assert s.last_tick_bookmark == 7  # preserved


def test_observe_event_updates_last_assistant_ts_only_on_assistant():
    s = TriggerV1State(last_assistant_ts=10.0)
    s2 = observe_event_v1(s, "user_text", 500.0)
    assert s2.last_assistant_ts == 10.0
    s3 = observe_event_v1(s, "assistant_text", 500.0)
    assert s3.last_assistant_ts == 500.0
    s4 = observe_event_v1(s, "artifact_write", 500.0)
    assert s4.last_assistant_ts == 10.0
