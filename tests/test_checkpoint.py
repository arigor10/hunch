"""Tests for checkpoint/resume mechanism."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from hunch.checkpoint import (
    CHECKPOINT_FILENAME,
    Checkpoint,
    checkpoint_from_trigger_state,
    read_checkpoint,
    trigger_state_from_checkpoint,
    write_checkpoint,
)
from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.trigger import TriggerV1State


# ---------------------------------------------------------------------------
# Unit tests: round-trip
# ---------------------------------------------------------------------------


def test_write_read_roundtrip(tmp_path: Path):
    cp = Checkpoint(
        events_consumed=42,
        ticks_fired=3,
        hunches_emitted=2,
        tick_counter=3,
        last_tick_ts=100.0,
        last_tick_bookmark=40,
        has_ticked=True,
        last_sim_now=110.0,
        bookmark_pre_event=41,
    )
    path = tmp_path / CHECKPOINT_FILENAME
    write_checkpoint(path, cp)
    loaded = read_checkpoint(path)
    assert loaded is not None
    assert loaded.events_consumed == 42
    assert loaded.ticks_fired == 3
    assert loaded.last_tick_ts == 100.0
    assert loaded.has_ticked is True


def test_read_missing_returns_none(tmp_path: Path):
    assert read_checkpoint(tmp_path / "nope.json") is None


def test_read_corrupt_returns_none(tmp_path: Path):
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text("not json")
    assert read_checkpoint(path) is None


def test_read_wrong_version_returns_none(tmp_path: Path):
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text(json.dumps({"version": 999}))
    assert read_checkpoint(path) is None


def test_trigger_state_roundtrip():
    state = TriggerV1State(
        last_tick_ts=50.0,
        last_tick_bookmark=10,
        has_ticked=True,
    )
    cp = checkpoint_from_trigger_state(state, events_consumed=100)
    restored = trigger_state_from_checkpoint(cp)
    assert restored.last_tick_ts == 50.0
    assert restored.last_tick_bookmark == 10
    assert restored.has_ticked is True
    assert restored.in_flight is False


# ---------------------------------------------------------------------------
# Integration test: offline resume
# ---------------------------------------------------------------------------


def _hunch(smell: str, desc: str) -> Hunch:
    return Hunch(smell=smell, description=desc)


def test_offline_resume_produces_same_result(tmp_path: Path):
    """Run replay halfway, resume, verify total matches a full run."""
    from hunch.replay import run_replay, run_replay_from_dir
    from hunch.trigger import TriggerV1Config
    from hunch.critic.stub import StubCritic

    tick_ids_full: list[str] = []
    tick_ids_resumed: list[str] = []

    class _CountingCritic:
        def __init__(self, log_to: list[str]):
            self._log_to = log_to

        def init(self, config):
            pass

        def shutdown(self):
            pass

        def tick(self, tick_id, bookmark_prev, bookmark_now):
            self._log_to.append(tick_id)
            return [_hunch(f"concern-{tick_id}", f"desc for {tick_id}")]

    # Build replay dir with 5 turns → 4 ticks in turn-end mode
    replay_dir = tmp_path / "replay"
    base_ts = _dt.datetime(2026, 1, 1, 0, 0, tzinfo=_dt.timezone.utc)
    events = []
    for i in range(5):
        events.append({
            "type": "user_text",
            "timestamp": (base_ts + _dt.timedelta(minutes=i * 10)).isoformat(),
            "text": f"user msg {i}",
        })
        events.append({
            "type": "assistant_text",
            "timestamp": (base_ts + _dt.timedelta(minutes=i * 10 + 1)).isoformat(),
            "text": f"assistant msg {i}",
        })
    # Final user message to trigger last turn-end
    events.append({
        "type": "user_text",
        "timestamp": (base_ts + _dt.timedelta(minutes=50)).isoformat(),
        "text": "final",
    })

    run_replay(
        events=events, project_roots=["/tmp"],
        replay_dir=replay_dir, critic=StubCritic(),
    )
    (replay_dir / "hunches.jsonl").unlink(missing_ok=True)

    cfg = TriggerV1Config(
        min_debounce_s=30.0,
    )

    # Full run
    full_output = tmp_path / "full"
    full_result = run_replay_from_dir(
        replay_dir=replay_dir,
        critic=_CountingCritic(tick_ids_full),
        trigger_config=cfg,
        output_dir=full_output,
    )
    assert full_result.ticks_fired >= 3

    # Partial run: stop after 2 ticks via max_events that captures ~2 ticks
    partial_output = tmp_path / "partial"
    partial_tick_ids: list[str] = []
    # Use max_events to stop mid-run. With 11 events and turn-end mode,
    # we need enough events to fire 2 ticks. Each tick fires at a user_text
    # following assistant_text, so after ~5 trigger events we get 2 ticks.
    run_replay_from_dir(
        replay_dir=replay_dir,
        critic=_CountingCritic(partial_tick_ids),
        trigger_config=cfg,
        output_dir=partial_output,
        max_events=7,
    )
    assert len(partial_tick_ids) >= 1
    partial_ticks = len(partial_tick_ids)

    # Verify checkpoint exists
    cp_path = partial_output / CHECKPOINT_FILENAME
    assert cp_path.exists()
    cp = read_checkpoint(cp_path)
    assert cp is not None
    assert cp.ticks_fired == partial_ticks

    # Resume
    resume_tick_ids: list[str] = []
    resume_result = run_replay_from_dir(
        replay_dir=replay_dir,
        critic=_CountingCritic(resume_tick_ids),
        trigger_config=cfg,
        output_dir=partial_output,
    )

    # Total ticks should match full run
    assert resume_result.ticks_fired == full_result.ticks_fired
    # Resume should not have re-fired the partial ticks
    assert len(resume_tick_ids) == full_result.ticks_fired - partial_ticks

    # Verify hunches.jsonl content matches: same number of hunches,
    # same smells (order may differ in tick_id suffix due to counter reset)
    full_hunches = _read_hunches(full_output / "hunches.jsonl")
    resumed_hunches = _read_hunches(partial_output / "hunches.jsonl")
    assert len(resumed_hunches) == len(full_hunches)
    full_smells = sorted(h["smell"] for h in full_hunches if h["type"] == "emit")
    resumed_smells = sorted(h["smell"] for h in resumed_hunches if h["type"] == "emit")
    assert full_smells == resumed_smells


def test_offline_checkpoint_written_after_each_tick(tmp_path: Path):
    """Checkpoint is updated after each tick, not just at the end."""
    from hunch.replay import run_replay, run_replay_from_dir
    from hunch.trigger import TriggerV1Config
    from hunch.critic.stub import StubCritic

    tick_count = 0
    checkpoints_seen: list[int] = []

    class _CheckpointSniffingCritic:
        def __init__(self, output_dir: Path):
            self._output_dir = output_dir

        def init(self, config):
            pass

        def shutdown(self):
            pass

        def tick(self, tick_id, bookmark_prev, bookmark_now):
            nonlocal tick_count
            tick_count += 1
            # Check checkpoint state BEFORE this tick's checkpoint is written
            cp = read_checkpoint(self._output_dir / CHECKPOINT_FILENAME)
            if cp is not None:
                checkpoints_seen.append(cp.ticks_fired)
            else:
                checkpoints_seen.append(0)
            return [_hunch(f"c-{tick_count}", "desc")]

    replay_dir = tmp_path / "replay"
    base_ts = _dt.datetime(2026, 1, 1, 0, 0, tzinfo=_dt.timezone.utc)
    events = []
    for i in range(4):
        events.append({
            "type": "user_text",
            "timestamp": (base_ts + _dt.timedelta(minutes=i * 10)).isoformat(),
            "text": f"u{i}",
        })
        events.append({
            "type": "assistant_text",
            "timestamp": (base_ts + _dt.timedelta(minutes=i * 10 + 1)).isoformat(),
            "text": f"a{i}",
        })
    events.append({
        "type": "user_text",
        "timestamp": (base_ts + _dt.timedelta(minutes=40)).isoformat(),
        "text": "final",
    })

    run_replay(
        events=events, project_roots=["/tmp"],
        replay_dir=replay_dir, critic=StubCritic(),
    )
    (replay_dir / "hunches.jsonl").unlink(missing_ok=True)

    output_dir = tmp_path / "output"
    cfg = TriggerV1Config(
        min_debounce_s=30.0,
    )
    run_replay_from_dir(
        replay_dir=replay_dir,
        critic=_CheckpointSniffingCritic(output_dir),
        trigger_config=cfg,
        output_dir=output_dir,
    )

    # First tick sees no checkpoint (0); subsequent ticks see prior tick count
    assert checkpoints_seen[0] == 0
    for i in range(1, len(checkpoints_seen)):
        assert checkpoints_seen[i] == i


def _read_hunches(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().strip().splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Integration test: online resume
# ---------------------------------------------------------------------------


def test_online_resume_restores_state(tmp_path: Path):
    """Runner.__post_init__ restores all critical state from checkpoint."""
    from hunch.run import RunConfig, Runner
    from hunch.trigger import TriggerV1Config
    from hunch.critic.stub import StubCritic
    from hunch.journal.append import append_json_line

    transcript = tmp_path / "transcript.jsonl"
    replay_dir = tmp_path / "replay"

    trigger_cfg = TriggerV1Config(
        min_debounce_s=0.0,
    )

    def _text_record(uuid, text, role="assistant"):
        return {
            "uuid": uuid,
            "timestamp": "2026-04-14T12:00:00.000Z",
            "type": role,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
            "cwd": str(tmp_path),
        }

    # Write initial transcript
    with open(transcript, "w") as f:
        f.write(json.dumps(_text_record("u1", "hello", "human")) + "\n")
        f.write(json.dumps(_text_record("a1", "hi there", "assistant")) + "\n")

    class _OneCritic(StubCritic):
        calls: list[str] = []

        def tick(self, tick_id, bookmark_prev, bookmark_now):
            _OneCritic.calls.append(tick_id)
            return [_hunch("test smell", "desc")]

    config = RunConfig(
        cwd=tmp_path,
        transcript_path=transcript,
        replay_dir=replay_dir,
        poll_s=0.0,
        critic_factory=_OneCritic,
        trigger_config=trigger_cfg,
        filter_enabled=False,
    )

    # First run: process events, inject claude_stopped, fire tick
    runner1 = Runner(config=config)
    runner1.step_once()

    append_json_line(runner1.writer.conversation_path, {
        "tick_seq": runner1.writer.tick_seq + 1,
        "type": "claude_stopped",
        "timestamp": "2026-04-14T12:05:00Z",
    })
    runner1.step_once()
    assert runner1._tick_counter == 1

    # Verify checkpoint was written
    cp_path = replay_dir / CHECKPOINT_FILENAME
    assert cp_path.exists()
    cp = read_checkpoint(cp_path)
    assert cp is not None
    assert cp.tick_counter == 1

    # Simulate restart: new Runner on same replay_dir
    _OneCritic.calls = []
    runner2 = Runner(config=config)

    assert runner2._tick_counter == 1
    assert runner2.parser_state.line_offset == runner1.parser_state.line_offset
    # writer.tick_seq is derived from conversation.jsonl line count, which
    # includes the hook-injected claude_stopped event — so it's >= runner1's
    conv_lines = len((replay_dir / "conversation.jsonl").read_text().strip().splitlines())
    assert runner2.writer.tick_seq == conv_lines
    assert runner2.trigger_state.has_ticked is True
    assert runner2.trigger_state.last_tick_bookmark == runner1.trigger_state.last_tick_bookmark
    assert runner2._hunches_emitted == 1

    # Step should not re-fire (no new transcript lines)
    runner2.step_once()
    assert runner2._tick_counter == 1
    assert len(_OneCritic.calls) == 0


def test_offline_resume_with_no_checkpoint_but_hunches_refuses(tmp_path: Path):
    """If hunches.jsonl exists but no checkpoint.json, refuse (ambiguous state)."""
    from hunch.replay import run_replay, run_replay_from_dir
    from hunch.trigger import TriggerV1Config
    from hunch.critic.stub import StubCritic

    replay_dir = tmp_path / "replay"
    base_ts = _dt.datetime(2026, 1, 1, 0, 0, tzinfo=_dt.timezone.utc)
    events = [
        {"type": "user_text", "timestamp": base_ts.isoformat(), "text": "hi"},
        {"type": "assistant_text", "timestamp": (base_ts + _dt.timedelta(minutes=1)).isoformat(), "text": "hello"},
    ]
    run_replay(
        events=events, project_roots=["/tmp"],
        replay_dir=replay_dir, critic=StubCritic(),
    )
    (replay_dir / "hunches.jsonl").unlink(missing_ok=True)

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    # Create hunches.jsonl without checkpoint
    (output_dir / "hunches.jsonl").write_text('{"type":"emit"}\n')

    with pytest.raises(RuntimeError, match="no checkpoint.json found"):
        run_replay_from_dir(
            replay_dir=replay_dir,
            critic=StubCritic(),
            output_dir=output_dir,
        )
