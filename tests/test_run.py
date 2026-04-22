"""Tests for `hunch/run.py` — the framework loop wiring.

The loop is deliberately small: its job is to glue capture + trigger
+ critic + journal together without losing events. These tests
exercise the wiring, not the component internals (those have their
own test files).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.critic.stub import StubCritic
from hunch.journal.append import append_json_line
from hunch.run import (
    RunConfig,
    Runner,
    _project_dir_for_cwd,
    find_latest_transcript,
)
from hunch.trigger import TriggerV1Config


# Zeroed-out trigger config for tests: fires on every claude_stopped
# event (turn-end mode) with no debounce requirements.
_TEST_TRIGGER = TriggerV1Config(
    silence_s=0.0,
    min_debounce_s=0.0,
    max_interval_s=1e9,
    fire_on_turn_end=True,
)


# ---------------------------------------------------------------------------
# Minimal transcript builder — mirror of the shape the parser accepts.
# ---------------------------------------------------------------------------

def _write_transcript(path: Path, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _append_transcript(path: Path, records: list[dict]) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _text_record(uuid: str, text: str, role: str = "assistant") -> dict:
    """Build a minimal Claude Code transcript line that parses to a
    text event. Matches the shape in `parse/transcript.py`."""
    return {
        "uuid": uuid,
        "timestamp": "2026-04-14T12:00:00.000Z",
        "type": role,
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
        "cwd": "/tmp/not-used-in-test",
    }


def _inject_claude_stopped(runner: Runner) -> None:
    """Simulate the Stop hook: append a claude_stopped event to
    conversation.jsonl with tick_seq one past the writer's current."""
    tick_seq = runner.writer.tick_seq + 1
    append_json_line(runner.writer.conversation_path, {
        "tick_seq": tick_seq,
        "type": "claude_stopped",
        "timestamp": "2026-04-14T12:05:00Z",
    })


# ---------------------------------------------------------------------------
# Transcript discovery
# ---------------------------------------------------------------------------

def test_project_dir_encoding():
    d = _project_dir_for_cwd(Path("/home/me/my_repo"))
    assert d.name == "-home-me-my-repo"


def test_find_latest_transcript_returns_none_when_no_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert find_latest_transcript(Path("/nonexistent/path")) is None


def test_find_latest_transcript_picks_most_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    project_dir = _project_dir_for_cwd(tmp_path / "project")
    project_dir.mkdir(parents=True)
    older = project_dir / "a.jsonl"
    newer = project_dir / "b.jsonl"
    older.write_text("{}\n")
    newer.write_text("{}\n")
    import os
    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    assert find_latest_transcript(tmp_path / "project") == newer


# ---------------------------------------------------------------------------
# Runner wiring
# ---------------------------------------------------------------------------

def test_runner_raises_if_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = RunConfig(cwd=tmp_path / "project")
    with pytest.raises(RuntimeError, match="no transcript found"):
        Runner(config=config)


def test_runner_step_captures_events_to_replay_buffer(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [_text_record("u1", "hello")])

    config = RunConfig(
        cwd=tmp_path,
        transcript_path=transcript,
        replay_dir=tmp_path / "replay",
        poll_s=0.0,
        trigger_config=_TEST_TRIGGER,
    )
    runner = Runner(config=config)
    runner.step_once()

    conversation = (tmp_path / "replay" / "conversation.jsonl").read_text()
    assert "hello" in conversation


def test_runner_fires_tick_on_claude_stopped(tmp_path):
    """assistant text → Stop hook → step_once picks up claude_stopped
    and fires the critic."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [
        _text_record("u1", "first", role="assistant"),
    ])

    class FakeCritic(StubCritic):
        def tick(self, tick_id, bookmark_prev, bookmark_now):
            super().tick(tick_id, bookmark_prev, bookmark_now)
            return [
                Hunch(
                    smell="calibration drift",
                    description="calibration runs look inconsistent across chunks.",
                    triggering_refs=TriggeringRefs(chunks=["c-1"], artifacts=[]),
                )
            ]

    config = RunConfig(
        cwd=tmp_path,
        transcript_path=transcript,
        replay_dir=tmp_path / "replay",
        poll_s=0.0,
        critic_factory=FakeCritic,
        trigger_config=_TEST_TRIGGER,
    )
    runner = Runner(config=config)
    runner.step_once()  # processes assistant text, no tick yet
    assert runner._tick_counter == 0

    _inject_claude_stopped(runner)
    runner.step_once()  # picks up claude_stopped from conversation.jsonl

    assert runner._tick_counter == 1
    hunches_path = tmp_path / "replay" / "hunches.jsonl"
    assert hunches_path.exists()
    lines = [L for L in hunches_path.read_text().splitlines() if L.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["type"] == "emit"
    assert record["smell"] == "calibration drift"
    assert record["hunch_id"] == "h-0001"


def test_runner_picks_up_appended_transcript_lines(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [_text_record("u1", "first")])

    config = RunConfig(
        cwd=tmp_path,
        transcript_path=transcript,
        replay_dir=tmp_path / "replay",
        poll_s=0.0,
        trigger_config=_TEST_TRIGGER,
    )
    runner = Runner(config=config)
    runner.step_once()
    bookmark_after_first = runner.writer.tick_seq

    _append_transcript(transcript, [_text_record("u2", "second")])
    runner.step_once()

    conversation = (tmp_path / "replay" / "conversation.jsonl").read_text()
    assert "first" in conversation
    assert "second" in conversation
    assert runner.writer.tick_seq > bookmark_after_first


def test_runner_does_not_tick_when_no_hook_events(tmp_path):
    """Without a claude_stopped event, no ticks fire."""
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [
        _text_record("u1", "first", role="assistant"),
    ])

    config = RunConfig(
        cwd=tmp_path,
        transcript_path=transcript,
        replay_dir=tmp_path / "replay",
        poll_s=0.0,
        trigger_config=_TEST_TRIGGER,
    )
    runner = Runner(config=config)
    runner.step_once()  # assistant text
    runner.step_once()  # nothing new
    assert runner._tick_counter == 0


def test_runner_calls_critic_init_exactly_once(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(transcript, [_text_record("u1", "first")])

    config = RunConfig(
        cwd=tmp_path,
        transcript_path=transcript,
        replay_dir=tmp_path / "replay",
    )
    runner = Runner(config=config)
    assert runner.critic.initialized is True
    assert runner.critic.config["replay_dir"] == str(tmp_path / "replay")
