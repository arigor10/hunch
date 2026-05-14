"""Tests for hunch.mine.renderer (delegates to hunch.render)."""

import pytest

from hunch.mine.renderer import render_chunk


def test_user_text_event():
    events = [{"tick_seq": 42, "type": "user_text", "text": "What's happening?"}]
    result = render_chunk(events)
    assert "**USER** (seq 42): What's happening?" in result


def test_assistant_text_event():
    events = [{"tick_seq": 43, "type": "assistant_text", "text": "Let me check."}]
    result = render_chunk(events)
    assert "**CLAUDE** (seq 43): Let me check." in result


def test_artifact_write_no_snapshot():
    events = [{"tick_seq": 10, "type": "artifact_write", "path": "docs/plan.md"}]
    result = render_chunk(events)
    assert "[ARTIFACT WRITE] (seq 10): docs/plan.md" in result
    assert "Content unavailable" in result


def test_artifact_edit():
    events = [{
        "tick_seq": 11,
        "type": "artifact_edit",
        "path": "docs/plan.md",
        "diff": {"old_string": "alpha", "new_string": "beta"},
    }]
    result = render_chunk(events)
    assert "[ARTIFACT EDIT] (seq 11): docs/plan.md" in result
    assert "'alpha' -> 'beta'" in result


def test_artifact_edit_skipped():
    events = [{
        "tick_seq": 12,
        "type": "artifact_edit",
        "path": "docs/plan.md",
        "skipped_reason": "file too large",
    }]
    result = render_chunk(events)
    assert "skipped: file too large" in result
    assert "(seq 12)" in result


def test_tool_error():
    events = [{
        "tick_seq": 20,
        "type": "tool_error",
        "tool_name": "Read",
        "error": "File not found",
    }]
    result = render_chunk(events)
    assert "[TOOL ERROR (Read)] (seq 20): File not found" in result


def test_figure():
    events = [{"tick_seq": 30, "type": "figure", "command": "plt.show()"}]
    result = render_chunk(events)
    assert "[FIGURE] (seq 30): plt.show()" in result


def test_empty_text_skipped():
    events = [{"tick_seq": 1, "type": "user_text", "text": ""}]
    result = render_chunk(events)
    assert result == ""


def test_unknown_type_skipped():
    events = [{"tick_seq": 1, "type": "some_unknown_type"}]
    result = render_chunk(events)
    assert result == ""


def test_multiple_events():
    events = [
        {"tick_seq": 1, "type": "user_text", "text": "Hello"},
        {"tick_seq": 2, "type": "assistant_text", "text": "Hi there"},
        {"tick_seq": 3, "type": "user_text", "text": "What's wrong here?"},
    ]
    result = render_chunk(events)
    lines = [l for l in result.strip().split("\n") if l.strip()]
    assert len(lines) == 3
    assert "**USER** (seq 1)" in lines[0]
    assert "**CLAUDE** (seq 2)" in lines[1]
    assert "**USER** (seq 3)" in lines[2]
