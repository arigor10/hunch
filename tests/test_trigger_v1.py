"""Tests for Trigger v1 policy (silence + debounce + max-interval + turn-end).

See hunch/trigger.py — decide_tick_v1, TriggerV1State, TriggerV1Config.
"""

from __future__ import annotations

from hunch.trigger import (
    FIRE_EXCLUSIVE,
    FIRE_INCLUSIVE,
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
    assert decide_tick_v1(state, now=1000.0, current_bookmark=5,
                          current_event_type="figure", config=CFG) is None


def test_no_fire_if_no_growth():
    state = TriggerV1State(last_tick_bookmark=5, has_ticked=True)
    assert decide_tick_v1(state, now=1e6, current_bookmark=5,
                          current_event_type="figure", config=CFG) is None


def test_no_fire_on_user_text():
    state = TriggerV1State(last_assistant_ts=0.0)  # first tick, no debounce
    assert decide_tick_v1(state, now=5000.0, current_bookmark=10,
                          current_event_type="user_text", config=CFG) is None


# ---------------------------------------------------------------------------
# Debounce
# ---------------------------------------------------------------------------

def test_debounce_blocks_fire_too_soon():
    state = TriggerV1State(
        last_tick_ts=100.0, last_tick_bookmark=3, has_ticked=True,
        last_assistant_ts=50.0,
    )
    # Silence elapsed (200-50=150 > 30) but debounce blocks (200-100=100 < 300)
    assert decide_tick_v1(state, now=200.0, current_bookmark=5,
                          current_event_type="tool_error", config=CFG) is None


def test_first_tick_not_debounced():
    # has_ticked=False → never fired → no debounce; silence fires.
    state = TriggerV1State(last_assistant_ts=10.0)
    assert decide_tick_v1(state, now=50.0, current_bookmark=3,
                          current_event_type="tool_error", config=CFG) == FIRE_INCLUSIVE


# ---------------------------------------------------------------------------
# Fire conditions
# ---------------------------------------------------------------------------

def test_silence_fires_once_debounce_elapsed():
    state = TriggerV1State(
        last_tick_ts=0.0, last_tick_bookmark=1, has_ticked=True,
        last_assistant_ts=50.0,
    )
    # Silence elapsed (400-50=350 > 30) AND debounce elapsed (400-0=400 > 300)
    assert decide_tick_v1(state, now=400.0, current_bookmark=2,
                          current_event_type="tool_error", config=CFG) == FIRE_INCLUSIVE


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
                          current_event_type="assistant_text", config=CFG) == FIRE_INCLUSIVE


def test_silence_fires_on_non_assistant_event():
    state = TriggerV1State(
        last_tick_ts=50.0,
        last_tick_bookmark=1,
        last_assistant_ts=50.0,
        has_ticked=True,
    )
    assert decide_tick_v1(state, now=400.0, current_bookmark=5,
                          current_event_type="tool_error", config=CFG) == FIRE_INCLUSIVE


def test_silence_does_not_fire_on_fresh_assistant_text():
    state = TriggerV1State(
        last_tick_ts=0.0,
        last_tick_bookmark=1,
        last_assistant_ts=100.0,
        has_ticked=True,
    )
    assert decide_tick_v1(state, now=400.0, current_bookmark=5,
                          current_event_type="assistant_text", config=CFG) is None


def test_silence_not_yet_elapsed():
    state = TriggerV1State(
        last_tick_ts=0.0,
        last_tick_bookmark=1,
        last_assistant_ts=380.0,
        has_ticked=True,
    )
    assert decide_tick_v1(state, now=400.0, current_bookmark=5,
                          current_event_type="tool_error", config=CFG) is None


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


def test_observe_event_tracks_last_event_type():
    s = TriggerV1State()
    s2 = observe_event_v1(s, "assistant_text", 100.0)
    assert s2.last_event_type == "assistant_text"
    s3 = observe_event_v1(s2, "user_text", 200.0)
    assert s3.last_event_type == "user_text"
    s4 = observe_event_v1(s3, "artifact_write", 300.0)
    assert s4.last_event_type == "artifact_write"


# ---------------------------------------------------------------------------
# Claude-stopped mode (fire_on_turn_end + silence)
# ---------------------------------------------------------------------------

TURN_CFG = TriggerV1Config(
    silence_s=30.0, min_debounce_s=300.0, max_interval_s=600.0,
    fire_on_turn_end=True,
)


def test_claude_stopped_fires_inclusive():
    state = TriggerV1State(last_assistant_ts=50.0)
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=TURN_CFG) == FIRE_INCLUSIVE


def test_claude_stopped_skips_non_claude_stopped_event():
    # user_text should NOT fire in turn-end mode (only claude_stopped does)
    state = TriggerV1State(last_assistant_ts=50.0)
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="user_text", config=TURN_CFG) is None


def test_claude_stopped_ignored_in_turn_end_mode_for_non_claude_stopped():
    # In turn-end mode, assistant_text does NOT trigger — only claude_stopped does
    state = TriggerV1State(last_assistant_ts=50.0)
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="assistant_text", config=TURN_CFG) is None


def test_claude_stopped_respects_debounce():
    state = TriggerV1State(
        last_assistant_ts=50.0,
        last_tick_ts=400.0, last_tick_bookmark=3, has_ticked=True,
    )
    # 100s since last tick, debounce is 300s → blocked
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=TURN_CFG) is None


def test_claude_stopped_fires_after_debounce():
    state = TriggerV1State(
        last_assistant_ts=50.0,
        last_tick_ts=100.0, last_tick_bookmark=3, has_ticked=True,
    )
    # 400s since last tick, debounce is 300s → fires
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=TURN_CFG) == FIRE_INCLUSIVE


def test_claude_stopped_first_tick_no_debounce():
    state = TriggerV1State(last_assistant_ts=10.0)
    assert decide_tick_v1(state, now=50.0, current_bookmark=3,
                          current_event_type="claude_stopped", config=TURN_CFG) == FIRE_INCLUSIVE


def test_claude_stopped_no_fire_if_in_flight():
    state = TriggerV1State(last_assistant_ts=50.0, in_flight=True)
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=TURN_CFG) is None


def test_claude_stopped_no_fire_if_no_growth():
    state = TriggerV1State(
        last_assistant_ts=50.0,
        last_tick_bookmark=5, has_ticked=True,
    )
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=TURN_CFG) is None


def test_claude_stopped_fires_regardless_of_last_event_type():
    state = TriggerV1State(
        last_assistant_ts=50.0,
        last_event_type="tool_error",
    )
    assert decide_tick_v1(state, now=500.0, current_bookmark=5,
                          current_event_type="claude_stopped", config=TURN_CFG) == FIRE_INCLUSIVE
