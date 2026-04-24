"""Integration tests for hunch.replay.driver.run_replay."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.replay import run_replay
from hunch.trigger import TriggerV1Config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(epoch_s: float) -> str:
    return _dt.datetime.fromtimestamp(epoch_s, tz=_dt.timezone.utc).isoformat()


def _user(epoch_s: float, text: str) -> dict:
    return {"type": "user_text", "timestamp": _ts(epoch_s), "text": text}


def _asst(epoch_s: float, text: str) -> dict:
    return {"type": "assistant_text", "timestamp": _ts(epoch_s), "text": text}


def _write(epoch_s: float, path: str, content: str) -> dict:
    return {
        "type": "artifact_write",
        "timestamp": _ts(epoch_s),
        "path": path,
        "content": content,
    }


class _RecordingCritic:
    """A Critic that records every tick call and optionally emits hunches."""

    def __init__(self, emit_at: set[int] | None = None) -> None:
        self.emit_at = emit_at or set()
        self.tick_log: list[dict[str, Any]] = []
        self.init_config: dict[str, Any] | None = None
        self.shutdown_count = 0

    def init(self, config: dict[str, Any]) -> None:
        self.init_config = dict(config)

    def tick(self, tick_id: str, bookmark_prev: int, bookmark_now: int) -> list[Hunch]:
        self.tick_log.append(
            {"tick_id": tick_id, "prev": bookmark_prev, "now": bookmark_now}
        )
        tick_num = int(tick_id.split("-")[-1])
        if tick_num in self.emit_at:
            return [
                Hunch(
                    smell=f"smell at {tick_id}",
                    description=f"description at {tick_id}",
                    triggering_refs=TriggeringRefs(),
                )
            ]
        return []

    def shutdown(self) -> None:
        self.shutdown_count += 1


# No-debounce config: fires on every claude_stopped event.
NO_DEBOUNCE = TriggerV1Config(min_debounce_s=0.0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_empty_event_stream_fires_nothing(tmp_path: Path):
    critic = _RecordingCritic()
    result = run_replay(
        events=[],
        project_roots=["/tmp/proj"],
        replay_dir=tmp_path / "replay",
        critic=critic,
    )
    assert result.events_consumed == 0
    assert result.ticks_fired == 0
    assert critic.tick_log == []
    assert critic.shutdown_count == 1


def test_first_tick_fires_on_turn_boundary(tmp_path: Path):
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "writing now"),
        _write(135.0, f"{proj}/results.md", "# Results\n"),
        _user(200.0, "looks good"),
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=NO_DEBOUNCE,
    )
    assert result.ticks_fired == 1
    assert critic.tick_log[0]["tick_id"] == "t-0001"


def test_no_fire_without_turn_boundary(tmp_path: Path):
    # Pure assistant monologue — no user_text, no claude_stopped synthesized.
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _asst(200.0, "still thinking"),
        _write(300.0, f"{proj}/a.md", "a"),
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=NO_DEBOUNCE,
    )
    assert result.ticks_fired == 0


def test_debounce_blocks_rapid_fires(tmp_path: Path):
    cfg = TriggerV1Config(min_debounce_s=300.0)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _user(135.0, "ok"),           # claude_stopped → first tick
        _asst(136.0, "more thinking"),
        _user(200.0, "what now?"),    # claude_stopped → debounce blocks (65s < 300s)
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=cfg,
    )
    assert result.ticks_fired == 1


def test_debounce_allows_after_elapsed(tmp_path: Path):
    cfg = TriggerV1Config(min_debounce_s=300.0)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _user(135.0, "ok"),           # claude_stopped @ 100 → first tick
        _asst(500.0, "more thinking"),
        _user(600.0, "what now?"),    # claude_stopped @ 500 → 400s > 300s → fires
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=cfg,
    )
    assert result.ticks_fired == 2


def test_hunches_persist_to_hunches_jsonl(tmp_path: Path):
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _user(135.0, "ok"),
    ]
    critic = _RecordingCritic(emit_at={1})
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=NO_DEBOUNCE,
    )
    assert result.hunches_emitted == 1
    hunches_file = tmp_path / "replay" / "hunches.jsonl"
    records = [
        json.loads(line) for line in hunches_file.read_text().splitlines()
        if line.strip() and json.loads(line).get("type") != "meta"
    ]
    assert len(records) == 1
    assert records[0]["type"] == "emit"
    assert records[0]["hunch_id"] == "h-0001"
    assert records[0]["smell"] == "smell at t-0001"
    assert records[0]["bookmark_prev"] == critic.tick_log[-1]["prev"]
    assert records[0]["bookmark_now"] == critic.tick_log[-1]["now"]


def test_persisted_hunch_bookmarks_match_tick_window(tmp_path: Path):
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _user(135.0, "ok"),
        _asst(136.0, "more"),
        _user(170.0, "then what?"),
        _asst(171.0, "final"),
        _user(205.0, "done"),
    ]
    critic = _RecordingCritic(emit_at={1, 2, 3})
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=NO_DEBOUNCE,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "replay" / "hunches.jsonl").read_text().splitlines()
        if line.strip() and json.loads(line).get("type") != "meta"
    ]
    assert [r["hunch_id"] for r in records] == ["h-0001", "h-0002", "h-0003"]
    for rec, logged in zip(records, critic.tick_log):
        assert rec["bookmark_prev"] == logged["prev"]
        assert rec["bookmark_now"] == logged["now"]
        assert rec["bookmark_now"] >= rec["bookmark_prev"]


def test_replay_dir_gets_full_layout(tmp_path: Path):
    proj = "/tmp/proj"
    events = [
        _user(0.0, "start"),
        _asst(1.0, "ok"),
        _write(2.0, f"{proj}/x.md", "x"),
    ]
    critic = _RecordingCritic()
    replay_dir = tmp_path / "replay"
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=replay_dir, critic=critic,
    )
    assert (replay_dir / "conversation.jsonl").exists()
    assert (replay_dir / "artifacts.jsonl").exists()
    assert (replay_dir / "artifacts").is_dir()
    assert any((replay_dir / "artifacts").iterdir())
    assert critic.init_config is not None
    assert Path(critic.init_config["replay_dir"]).resolve() == replay_dir.resolve()


def test_backward_timestamp_is_clamped_and_counted(tmp_path: Path):
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "late"),
        _write(50.0, f"{proj}/x.md", "x"),  # ts goes backward
        _user(200.0, "ok"),
    ]
    critic = _RecordingCritic()
    logs: list[str] = []
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        on_log=logs.append, trigger_config=NO_DEBOUNCE,
    )
    assert result.backward_ts_warnings >= 1
    assert any("warning" in m.lower() for m in logs)


def test_max_events_cap(tmp_path: Path):
    proj = "/tmp/proj"
    events = [_write(float(i), f"{proj}/f{i}.md", "x") for i in range(5)]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        max_events=2,
    )
    assert result.events_consumed == 2


# ---------------------------------------------------------------------------
# Refuse to clobber an existing replay_dir
# ---------------------------------------------------------------------------

def test_refuse_to_clobber_populated_replay_dir(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    events = [_write(0.0, f"{proj}/a.md", "a")]
    critic = _RecordingCritic()
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=replay_dir, critic=critic,
    )
    with pytest.raises(RuntimeError, match="refusing to clobber"):
        run_replay(
            events=events, project_roots=[proj],
            replay_dir=replay_dir, critic=_RecordingCritic(),
        )


def test_empty_replay_dir_is_fine(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    events = [_write(0.0, f"{proj}/a.md", "a")]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=replay_dir, critic=critic,
    )
    assert result.events_consumed == 1
