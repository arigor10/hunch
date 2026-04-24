"""Integration tests for hunch.replay.driver.run_replay_from_dir +
hunch.replay.loader.load_trigger_events.

The "from-dir" mode drives the Critic over an already-populated
replay buffer — read-only on conversation.jsonl / artifacts.jsonl /
artifacts/, appending only hunches.jsonl.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.replay import (
    load_trigger_events,
    run_replay,
    run_replay_from_dir,
)
from hunch.trigger import TriggerV1Config


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

NO_DEBOUNCE = TriggerV1Config(min_debounce_s=0.0)


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
                    description=f"desc at {tick_id}",
                    triggering_refs=TriggeringRefs(),
                )
            ]
        return []

    def shutdown(self) -> None:
        self.shutdown_count += 1


def _populate(replay_dir: Path, events: list[dict], project_roots: list[str]) -> None:
    """Run events through the full `run_replay` pipeline to populate the
    replay dir. Then delete the hunches.jsonl that `run_replay` produced
    (we're testing the from-dir path against a virgin hunches state)."""
    run_replay(
        events=events, project_roots=project_roots,
        replay_dir=replay_dir, critic=_RecordingCritic(),
    )
    (replay_dir / "hunches.jsonl").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def test_loader_reads_tick_seq_in_order(tmp_path: Path):
    proj = "/tmp/proj"
    events = [
        _user(0.0, "hi"),
        _asst(1.0, "hello"),
        _write(2.0, f"{proj}/a.md", "# A"),
    ]
    replay_dir = tmp_path / "replay"
    _populate(replay_dir, events, [proj])

    loaded = load_trigger_events(replay_dir)
    assert [e.tick_seq for e in loaded] == [1, 2, 3]
    assert [e.type for e in loaded] == ["user_text", "assistant_text", "artifact_write"]
    assert loaded[0].timestamp.startswith("2026") or loaded[0].timestamp.startswith("1970")


def test_loader_missing_conversation_jsonl_raises(tmp_path: Path):
    replay_dir = tmp_path / "fresh"
    replay_dir.mkdir()
    with pytest.raises(FileNotFoundError, match="conversation.jsonl"):
        load_trigger_events(replay_dir)


def test_loader_rejects_non_monotonic_tick_seq(tmp_path: Path):
    replay_dir = tmp_path / "bad"
    replay_dir.mkdir()
    (replay_dir / "conversation.jsonl").write_text(
        json.dumps({"tick_seq": 2, "type": "user_text", "timestamp": "x"}) + "\n"
        + json.dumps({"tick_seq": 1, "type": "user_text", "timestamp": "y"}) + "\n"
    )
    with pytest.raises(ValueError, match="not.*greater"):
        load_trigger_events(replay_dir)


def test_loader_rejects_malformed_json(tmp_path: Path):
    replay_dir = tmp_path / "bad"
    replay_dir.mkdir()
    (replay_dir / "conversation.jsonl").write_text("not json\n")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_trigger_events(replay_dir)


# ---------------------------------------------------------------------------
# run_replay_from_dir
# ---------------------------------------------------------------------------

def test_from_dir_fires_same_ticks_as_events_in_mode(tmp_path: Path):
    # Same event stream, two code paths: events-in vs loaded-from-dir.
    # The tick pattern MUST match.
    proj = "/tmp/proj"
    events = [
        _asst(10.0, "thinking"),
        _user(60.0, "ok"),
        _asst(130.0, "more"),
        _user(200.0, "next"),
    ]

    # Path A: events-in
    dir_a = tmp_path / "a"
    critic_a = _RecordingCritic()
    result_a = run_replay(
        events=events, project_roots=[proj],
        replay_dir=dir_a, critic=critic_a, trigger_config=NO_DEBOUNCE,
    )

    # Path B: populate dir_b, then from-dir
    dir_b = tmp_path / "b"
    _populate(dir_b, events, [proj])
    critic_b = _RecordingCritic()
    result_b = run_replay_from_dir(
        replay_dir=dir_b, critic=critic_b, trigger_config=NO_DEBOUNCE,
    )

    assert result_a.ticks_fired == result_b.ticks_fired
    assert [t["prev"] for t in critic_a.tick_log] == [t["prev"] for t in critic_b.tick_log]
    assert [t["now"] for t in critic_a.tick_log] == [t["now"] for t in critic_b.tick_log]


def test_from_dir_refuses_existing_hunches(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    _populate(replay_dir, [_write(0.0, f"{proj}/a.md", "a")], [proj])
    (replay_dir / "hunches.jsonl").write_text(
        json.dumps({"type": "emit", "hunch_id": "h-0001"}) + "\n"
    )
    with pytest.raises(RuntimeError, match="already exists"):
        run_replay_from_dir(
            replay_dir=replay_dir, critic=_RecordingCritic(),
        )


def test_from_dir_overwrite_hunches_replaces_file(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    _populate(replay_dir, [
        _asst(100.0, "start"),
        _write(135.0, f"{proj}/a.md", "a"),
        _user(200.0, "ok"),
    ], [proj])
    (replay_dir / "hunches.jsonl").write_text(
        json.dumps({"type": "emit", "hunch_id": "h-9999"}) + "\n"
    )
    critic = _RecordingCritic(emit_at={1})
    result = run_replay_from_dir(
        replay_dir=replay_dir, critic=critic, overwrite_hunches=True,
        trigger_config=NO_DEBOUNCE,
    )
    records = [
        json.loads(line)
        for line in (replay_dir / "hunches.jsonl").read_text().splitlines()
        if line.strip() and json.loads(line).get("type") != "meta"
    ]
    assert all(r["hunch_id"] != "h-9999" for r in records)
    assert result.hunches_emitted >= 1


def test_from_dir_is_read_only_on_conversation_and_artifacts(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    events = [
        _asst(10.0, "thinking"),
        _write(20.0, f"{proj}/a.md", "a"),
        _user(60.0, "ok"),
        _asst(70.0, "more"),
        _write(120.0, f"{proj}/b.md", "b"),
        _user(200.0, "done"),
    ]
    _populate(replay_dir, events, [proj])

    conv_before = (replay_dir / "conversation.jsonl").read_bytes()
    arts_before = (replay_dir / "artifacts.jsonl").read_bytes()
    artifacts_listing_before = sorted(
        p.name for p in (replay_dir / "artifacts").iterdir()
    )

    run_replay_from_dir(
        replay_dir=replay_dir, critic=_RecordingCritic(),
        trigger_config=NO_DEBOUNCE,
    )

    assert (replay_dir / "conversation.jsonl").read_bytes() == conv_before
    assert (replay_dir / "artifacts.jsonl").read_bytes() == arts_before
    assert sorted(
        p.name for p in (replay_dir / "artifacts").iterdir()
    ) == artifacts_listing_before


def test_from_dir_passes_replay_dir_to_critic_init(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    _populate(replay_dir, [_write(0.0, f"{proj}/a.md", "a")], [proj])
    critic = _RecordingCritic()
    run_replay_from_dir(replay_dir=replay_dir, critic=critic)
    assert critic.init_config is not None
    assert Path(critic.init_config["replay_dir"]).resolve() == replay_dir.resolve()


def test_from_dir_respects_max_events(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    events = [_write(float(i), f"{proj}/f{i}.md", str(i)) for i in range(5)]
    _populate(replay_dir, events, [proj])
    result = run_replay_from_dir(
        replay_dir=replay_dir, critic=_RecordingCritic(), max_events=2,
    )
    assert result.events_consumed == 2
