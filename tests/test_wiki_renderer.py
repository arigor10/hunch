"""Tests for wiki_renderer — current_block.md rendering."""

import json
from pathlib import Path

import pytest

from hunch.critic.wiki_renderer import (
    read_events_in_range,
    render_current_block,
)


@pytest.fixture
def replay_dir(tmp_path):
    """Create a replay dir with conversation.jsonl and artifacts/."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return tmp_path


def _write_events(replay_dir: Path, events: list[dict]) -> None:
    conv = replay_dir / "conversation.jsonl"
    with open(conv, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _write_snapshot(replay_dir: Path, name: str, content: str) -> None:
    (replay_dir / "artifacts" / name).write_text(content)


# ---------------------------------------------------------------------------
# read_events_in_range
# ---------------------------------------------------------------------------

def test_read_events_basic(replay_dir):
    events = [
        {"tick_seq": 1, "type": "user_text", "timestamp": "2026-01-01T00:00:01Z", "text": "hello"},
        {"tick_seq": 2, "type": "assistant_text", "timestamp": "2026-01-01T00:00:02Z", "text": "hi"},
        {"tick_seq": 3, "type": "user_text", "timestamp": "2026-01-01T00:00:03Z", "text": "bye"},
    ]
    _write_events(replay_dir, events)
    result = read_events_in_range(replay_dir / "conversation.jsonl", 1, 3)
    assert len(result) == 2
    assert result[0]["tick_seq"] == 2
    assert result[1]["tick_seq"] == 3


def test_read_events_empty_range(replay_dir):
    events = [
        {"tick_seq": 1, "type": "user_text", "timestamp": "2026-01-01T00:00:01Z", "text": "hello"},
    ]
    _write_events(replay_dir, events)
    result = read_events_in_range(replay_dir / "conversation.jsonl", 1, 1)
    assert result == []


def test_read_events_full_range(replay_dir):
    events = [
        {"tick_seq": i, "type": "user_text", "timestamp": f"2026-01-01T00:00:{i:02d}Z", "text": f"msg {i}"}
        for i in range(1, 6)
    ]
    _write_events(replay_dir, events)
    result = read_events_in_range(replay_dir / "conversation.jsonl", 0, 5)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# render_current_block
# ---------------------------------------------------------------------------

def test_render_user_text(replay_dir):
    events = [
        {"tick_seq": 5, "type": "user_text", "timestamp": "2026-01-01T10:00:00Z", "text": "Let's try layer 14"},
    ]
    block = render_current_block(events, "t-0003", 4, 5, replay_dir / "artifacts")
    assert "**USER:** Let's try layer 14" in block
    assert "t-0003" in block
    assert "Replay turns 5-5" in block


def test_render_assistant_text(replay_dir):
    events = [
        {"tick_seq": 6, "type": "assistant_text", "timestamp": "2026-01-01T10:01:00Z", "text": "Running experiment..."},
    ]
    block = render_current_block(events, "t-0004", 5, 6, replay_dir / "artifacts")
    assert "**CLAUDE:** Running experiment..." in block


def test_render_artifact_write(replay_dir):
    _write_snapshot(replay_dir, "results_exp.md__snap1", "# Results\nRotation at 311 degrees")
    events = [
        {
            "tick_seq": 7, "type": "artifact_write",
            "timestamp": "2026-01-01T10:02:00Z",
            "path": "results/exp.md",
            "snapshot": "results_exp.md__snap1",
            "content_hash": "abc123",
        },
    ]
    block = render_current_block(events, "t-0005", 6, 7, replay_dir / "artifacts")
    assert "[ARTIFACT WRITE: results/exp.md]" in block
    assert "Rotation at 311 degrees" in block
    assert "[Content (33 chars):]" in block


def test_render_artifact_write_truncation(replay_dir):
    big_content = "x" * 15_000
    _write_snapshot(replay_dir, "big__snap", big_content)
    events = [
        {
            "tick_seq": 8, "type": "artifact_write",
            "timestamp": "2026-01-01T10:03:00Z",
            "path": "big.md",
            "snapshot": "big__snap",
            "content_hash": "def456",
        },
    ]
    block = render_current_block(events, "t-0006", 7, 8, replay_dir / "artifacts")
    assert "[... truncated, 15000 total chars]" in block
    assert "[Content (15000 chars):]" in block


def test_render_artifact_edit(replay_dir):
    events = [
        {
            "tick_seq": 9, "type": "artifact_edit",
            "timestamp": "2026-01-01T10:04:00Z",
            "path": "docs/plan.md",
            "snapshot": "snap2",
            "content_hash": "ghi789",
            "diff": {"old_string": "step 1", "new_string": "step 1 (done)"},
        },
    ]
    block = render_current_block(events, "t-0007", 8, 9, replay_dir / "artifacts")
    assert "[ARTIFACT EDIT: docs/plan.md]" in block
    assert "[Changed: 'step 1' -> 'step 1 (done)']" in block


def test_render_artifact_edit_skipped(replay_dir):
    events = [
        {
            "tick_seq": 10, "type": "artifact_edit",
            "timestamp": "2026-01-01T10:05:00Z",
            "path": "docs/plan.md",
            "skipped_reason": "edit_before_known_base",
            "old_string": "x", "new_string": "y",
        },
    ]
    block = render_current_block(events, "t-0008", 9, 10, replay_dir / "artifacts")
    assert "[ARTIFACT EDIT (skipped: edit_before_known_base): docs/plan.md]" in block


def test_render_tool_error(replay_dir):
    events = [
        {
            "tick_seq": 11, "type": "tool_error",
            "timestamp": "2026-01-01T10:06:00Z",
            "tool_name": "Bash",
            "error": "command not found",
        },
    ]
    block = render_current_block(events, "t-0009", 10, 11, replay_dir / "artifacts")
    assert "[TOOL ERROR (Bash): command not found]" in block


def test_render_empty_block(replay_dir):
    block = render_current_block([], "t-0010", 20, 25, replay_dir / "artifacts")
    assert "(no events in this block)" in block
    assert "t-0010" in block


def test_render_mixed_block(replay_dir):
    _write_snapshot(replay_dir, "snap_write", "content here")
    events = [
        {"tick_seq": 1, "type": "user_text", "timestamp": "2026-01-01T10:00:00Z", "text": "Do it"},
        {"tick_seq": 2, "type": "assistant_text", "timestamp": "2026-01-01T10:00:05Z", "text": "On it"},
        {
            "tick_seq": 3, "type": "artifact_write", "timestamp": "2026-01-01T10:00:10Z",
            "path": "out.md", "snapshot": "snap_write", "content_hash": "x",
        },
        {
            "tick_seq": 4, "type": "artifact_edit", "timestamp": "2026-01-01T10:00:15Z",
            "path": "out.md", "snapshot": "snap2", "content_hash": "y",
            "diff": {"old_string": "old", "new_string": "new"},
        },
    ]
    block = render_current_block(events, "t-0001", 0, 4, replay_dir / "artifacts")
    assert "**USER:** Do it" in block
    assert "**CLAUDE:** On it" in block
    assert "[ARTIFACT WRITE: out.md]" in block
    assert "[ARTIFACT EDIT: out.md]" in block
    assert "Replay turns 1-4" in block


def test_header_timestamps(replay_dir):
    events = [
        {"tick_seq": 1, "type": "user_text", "timestamp": "2026-03-15T14:22:00Z", "text": "start"},
        {"tick_seq": 5, "type": "user_text", "timestamp": "2026-03-15T14:58:00Z", "text": "end"},
    ]
    block = render_current_block(events, "t-0042", 0, 5, replay_dir / "artifacts")
    assert "2026-03-15 14:22:00" in block
    assert "2026-03-15 14:58:00" in block
