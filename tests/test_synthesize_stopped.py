"""Tests for `synthesize_claude_stopped` in the replay loader."""

from __future__ import annotations

from hunch.replay.loader import TriggerEvent, synthesize_claude_stopped


def _te(tick_seq: int, type: str, ts: str = "2026-04-14T12:00:00Z") -> TriggerEvent:
    return TriggerEvent(tick_seq=tick_seq, type=type, timestamp=ts)


def test_empty_list():
    assert synthesize_claude_stopped([]) == []


def test_no_speaker_change():
    events = [
        _te(1, "assistant_text"),
        _te(2, "assistant_text"),
        _te(3, "assistant_text"),
    ]
    result = synthesize_claude_stopped(events)
    assert len(result) == 3
    assert all(e.type == "assistant_text" for e in result)


def test_inserts_at_speaker_boundary():
    events = [
        _te(1, "assistant_text", "2026-04-14T12:00:00Z"),
        _te(2, "user_text", "2026-04-14T12:05:00Z"),
    ]
    result = synthesize_claude_stopped(events)
    assert len(result) == 3
    assert result[0].type == "assistant_text"
    assert result[1].type == "claude_stopped"
    assert result[1].tick_seq == 1  # same as preceding assistant
    assert result[1].timestamp == "2026-04-14T12:00:00Z"
    assert result[2].type == "user_text"


def test_no_insert_user_after_user():
    events = [
        _te(1, "user_text"),
        _te(2, "user_text"),
    ]
    result = synthesize_claude_stopped(events)
    assert len(result) == 2


def test_artifact_write_counts_as_assistant():
    events = [
        _te(1, "artifact_write"),
        _te(2, "user_text"),
    ]
    result = synthesize_claude_stopped(events)
    assert len(result) == 3
    assert result[1].type == "claude_stopped"


def test_multiple_boundaries():
    events = [
        _te(1, "assistant_text"),
        _te(2, "user_text"),
        _te(3, "assistant_text"),
        _te(4, "artifact_edit"),
        _te(5, "user_text"),
    ]
    result = synthesize_claude_stopped(events)
    types = [e.type for e in result]
    assert types == [
        "assistant_text",
        "claude_stopped",
        "user_text",
        "assistant_text",
        "artifact_edit",
        "claude_stopped",
        "user_text",
    ]


def test_existing_claude_stopped_not_duplicated():
    """If stream already has claude_stopped, don't double-insert."""
    events = [
        _te(1, "assistant_text"),
        _te(2, "claude_stopped"),
        _te(3, "user_text"),
    ]
    result = synthesize_claude_stopped(events)
    types = [e.type for e in result]
    assert types.count("claude_stopped") == 1
