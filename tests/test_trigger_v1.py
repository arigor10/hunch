"""Tests for Trigger v1 policy (claude-stopped + debounce).

See hunch/trigger.py — decide_tick_v1, TriggerV1State, TriggerV1Config.
"""

from __future__ import annotations

from hunch.trigger import (
    FIRE_INCLUSIVE,
    TriggerV1Config,
    TriggerV1State,
    decide_tick_v1,
    mark_tick_finished_v1,
    mark_tick_started_v1,
    observe_event_v1,
)


CFG = TriggerV1Config(min_debounce_s=300.0)


# ---------------------------------------------------------------------------
# Hard skips
# ---------------------------------------------------------------------------

def test_no_fire_if_in_flight():
    state = TriggerV1State(in_flight=True)
    assert decide_tick_v1(state, now=1000.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=CFG) is None


def test_no_fire_if_no_growth():
    state = TriggerV1State(last_tick_bookmark=5, has_ticked=True)
    assert decide_tick_v1(state, now=1e6, current_bookmark=5,
                          current_event_type="claude_stopped", config=CFG) is None


def test_no_fire_on_non_claude_stopped():
    state = TriggerV1State()
    assert decide_tick_v1(state, now=5000.0, current_bookmark=10,
                          current_event_type="user_text", config=CFG) is None
    assert decide_tick_v1(state, now=5000.0, current_bookmark=10,
                          current_event_type="assistant_text", config=CFG) is None
    assert decide_tick_v1(state, now=5000.0, current_bookmark=10,
                          current_event_type="tool_error", config=CFG) is None
    assert decide_tick_v1(state, now=5000.0, current_bookmark=10,
                          current_event_type="artifact_write", config=CFG) is None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

def test_debounce_blocks_fire_too_soon():
    state = TriggerV1State(
        last_tick_ts=400.0, last_tick_bookmark=3, has_ticked=True,
    )
    # 100s since last tick, debounce is 300s → blocked
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=CFG) is None


def test_first_tick_not_debounced():
    state = TriggerV1State()
    assert decide_tick_v1(state, now=50.0, current_bookmark=3,
                          current_event_type="claude_stopped", config=CFG) == FIRE_INCLUSIVE


# ---------------------------------------------------------------------------
# Fire conditions
# ---------------------------------------------------------------------------

def test_claude_stopped_fires_after_debounce():
    state = TriggerV1State(
        last_tick_ts=100.0, last_tick_bookmark=3, has_ticked=True,
    )
    # 400s since last tick, debounce is 300s → fires
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=CFG) == FIRE_INCLUSIVE


def test_claude_stopped_fires_inclusive():
    state = TriggerV1State()
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=CFG) == FIRE_INCLUSIVE


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


def test_observe_event_is_identity():
    s = TriggerV1State()
    s2 = observe_event_v1(s, "user_text", 500.0)
    assert s2 == s
    s3 = observe_event_v1(s, "assistant_text", 500.0)
    assert s3 == s
