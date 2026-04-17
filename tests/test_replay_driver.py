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


def test_hot_event_fires_first_tick(tmp_path: Path):
    # One artifact_write on a fresh state → should fire immediately.
    project_root = "/tmp/proj"
    events = [
        _user(0.0, "please write results.md"),
        _asst(1.0, "writing now"),
        _write(2.0, f"{project_root}/results.md", "# Results\n"),
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[project_root],
        replay_dir=tmp_path / "replay",
        critic=critic,
    )
    assert result.ticks_fired == 1
    assert critic.tick_log[0]["tick_id"] == "t-0001"


def test_user_text_never_fires(tmp_path: Path):
    cfg = TriggerV1Config(silence_s=0.0, min_debounce_s=0.0, max_interval_s=0.0)
    events = [_user(0.0, "hi"), _user(1000.0, "anyone home?")]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=cfg,
    )
    assert result.ticks_fired == 0
    assert critic.tick_log == []


def test_debounce_blocks_rapid_hot_events(tmp_path: Path):
    # Two hot events 60s apart, min_debounce=300s → only the first fires.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=300.0, max_interval_s=1e9)
    proj = "/tmp/proj"
    events = [
        _write(0.0, f"{proj}/a.md", "a"),
        _write(60.0, f"{proj}/b.md", "b"),
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


def test_max_interval_forces_fire_during_long_silence(tmp_path: Path):
    # After a tick fires on a hot event, a LONG run of assistant_text
    # with no hot events and no non-assistant boundary — virtual ticks
    # must fire during gaps where live would have fired on silence/
    # max_interval alone.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=120.0)
    proj = "/tmp/proj"
    events = [
        _write(0.0, f"{proj}/start.md", "s"),          # t1 fires (hot)
        _asst(10.0, "thinking"),
        _asst(30.0, "still thinking"),
        _asst(60.0, "deeper"),                          # virtual silence fire at t=60
        _asst(90.0, "keep going"),
        _asst(130.0, "almost there"),                   # virtual silence fire at t=120
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=cfg,
    )
    assert result.ticks_fired == 3
    assert result.virtual_ticks_fired == 2


def test_hunches_persist_to_hunches_jsonl(tmp_path: Path):
    # Fire on the first hot event, emit one hunch, verify hunches.jsonl.
    proj = "/tmp/proj"
    events = [_write(0.0, f"{proj}/r.md", "# R")]
    critic = _RecordingCritic(emit_at={1})
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
    )
    assert result.hunches_emitted == 1
    hunches_file = tmp_path / "replay" / "hunches.jsonl"
    records = [json.loads(line) for line in hunches_file.read_text().splitlines()]
    assert len(records) == 1
    assert records[0]["type"] == "emit"
    assert records[0]["hunch_id"] == "h-0001"
    assert records[0]["smell"] == "smell at t-0001"
    # bookmark_prev/bookmark_now identify the Critic's window for
    # this tick — offline evaluators (novelty, duplicate) rely on
    # these to pull the same slice the Critic "saw".
    assert records[0]["bookmark_prev"] == critic.tick_log[-1]["prev"]
    assert records[0]["bookmark_now"] == critic.tick_log[-1]["now"]


def test_persisted_hunch_bookmarks_match_tick_window(tmp_path: Path):
    # Emit hunches at several ticks across an event stream and verify
    # the bookmark fields in hunches.jsonl align with the tick log.
    proj = "/tmp/proj"
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=120.0)
    events = [
        _write(0.0, f"{proj}/a.md", "a"),      # hot → tick 1
        _asst(10.0, "thinking"),
        _asst(65.0, "more"),                   # silence → tick 2
        _write(140.0, f"{proj}/b.md", "b"),    # hot → tick 3 (first passing debounce)
    ]
    critic = _RecordingCritic(emit_at={1, 2, 3})
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic, trigger_config=cfg,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "replay" / "hunches.jsonl").read_text().splitlines()
    ]
    # One hunch per emit_at tick, in emission order.
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
    # critic.init received the replay_dir in its config
    assert critic.init_config is not None
    assert Path(critic.init_config["replay_dir"]).resolve() == replay_dir.resolve()


def test_backward_timestamp_is_clamped_and_counted(tmp_path: Path):
    # Second event has a ts earlier than first — driver clamps sim_now
    # and bumps the warning counter.
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "late"),
        _write(50.0, f"{proj}/x.md", "x"),  # ts goes backward
    ]
    critic = _RecordingCritic()
    logs: list[str] = []
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        on_log=logs.append,
    )
    assert result.backward_ts_warnings == 1
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
# Virtual tick injection (offline/live cadence parity)
# ---------------------------------------------------------------------------

def test_virtual_silence_tick_fires_in_gap(tmp_path: Path):
    # Hot event at t=0 fires t1. Then a long event-less gap followed by a
    # tool_error much later. silence_s=30 elapses in the gap — a live loop
    # polling every second would have fired on silence. Virtual tick should
    # fire at last_assistant_ts + silence_s (but last_assistant_ts=0 since
    # the only prior event was a write). Use an asst event to set it.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=10_000.0)
    proj = "/tmp/proj"
    events = [
        _write(0.0, f"{proj}/a.md", "a"),     # t1 fires (hot)
        _asst(10.0, "thinking"),              # sets last_assistant_ts=10
        # Long gap to t=500 — live would have fired at t=70 (silence=30
        # after assistant=10, but debounce needs 60s after last_tick_ts=0,
        # so actually fires at t=60). Next event at t=500 is a tool_error.
        {"type": "tool_error", "timestamp": _ts(500.0), "message": "boom"},
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=cfg,
    )
    # Expect: t1 (hot at 0), virtual silence tick during (10, 500], then
    # the event at 500 may or may not fire depending on debounce.
    assert result.virtual_ticks_fired >= 1
    # The first virtual tick's sim_now should be silence-time, not event-time.
    virtual = [t for t in critic.tick_log]
    # Driver log has sim_now / virtual flag
    vticks = [t for t in result.tick_log if t["virtual"]]
    assert len(vticks) >= 1
    # First virtual tick fires at last_assistant_ts (10) + silence_s (30) = 40,
    # but also needs min_debounce_s (60) since last_tick_ts=0, so fires at 60.
    assert vticks[0]["sim_now"] == pytest.approx(60.0)


def test_virtual_max_interval_tick_fires_in_gap(tmp_path: Path):
    # After a hot-event tick, a non-hot event grows the bookmark, then
    # a long idle gap follows. silence can't fire (no assistant activity
    # so last_assistant_ts=0, guard skips it). max_interval must force
    # a virtual tick during the gap.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=200.0)
    proj = "/tmp/proj"
    events = [
        _write(0.0, f"{proj}/a.md", "a"),     # t1 fires at 0 (hot)
        # Tool error at t=5 grows bookmark but is not a hot event (and
        # silence can't elapse because there was no assistant_text yet).
        {"type": "tool_error", "timestamp": _ts(5.0), "message": "boom"},
        # Large gap — a live loop polling every second would have fired
        # at last_tick_ts (0) + max_interval_s (200) = t=200.
        {"type": "tool_error", "timestamp": _ts(1000.0), "message": "still boom"},
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=cfg,
    )
    vticks = [t for t in result.tick_log if t["virtual"]]
    assert len(vticks) >= 1
    assert vticks[0]["sim_now"] == pytest.approx(200.0)


def test_virtual_tick_respects_no_new_content(tmp_path: Path):
    # Virtual candidates are skipped when no new events have been appended
    # since the last tick — there's nothing new to critique.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=200.0)
    proj = "/tmp/proj"
    events = [
        _write(0.0, f"{proj}/a.md", "a"),     # t1 fires (hot) at bookmark 1
        # The next event arrives at t=1000 but before it's appended, the
        # driver checks the gap (0, 1000]. current_bookmark is still 1 at
        # that point (equal to last_tick_bookmark), so virtual ticks are
        # suppressed — nothing to tick on.
        _write(1000.0, f"{proj}/b.md", "b"),
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=cfg,
    )
    # Our driver checks current_bookmark <= last_tick_bookmark inside
    # _next_virtual_tick_time. Bookmark after t1 == 1, and pre-second-event
    # bookmark is still 1, so virtual tick is suppressed. But after the
    # second event appends (bookmark=2), the event-driven path can still
    # fire. So we expect 1 event-driven, 0 virtual, despite the gap being
    # way past max_interval.
    #
    # Rationale: max_interval in live mode is tested against a ticking
    # clock while events accumulate in parallel. In replay, if there are
    # no new events, there's nothing for the critic to say. The live loop
    # would also skip under `bookmark_now <= last_tick_bookmark`.
    assert result.virtual_ticks_fired == 0


# ---------------------------------------------------------------------------
# Refuse to clobber an existing replay_dir
# ---------------------------------------------------------------------------

def test_refuse_to_clobber_populated_replay_dir(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    events = [_write(0.0, f"{proj}/a.md", "a")]
    critic = _RecordingCritic()
    # First run succeeds.
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=replay_dir, critic=critic,
    )
    # Second run without allow_existing must refuse.
    with pytest.raises(RuntimeError, match="refusing to clobber"):
        run_replay(
            events=events, project_roots=[proj],
            replay_dir=replay_dir, critic=_RecordingCritic(),
        )


def test_allow_existing_permits_second_run(tmp_path: Path):
    proj = "/tmp/proj"
    replay_dir = tmp_path / "replay"
    events = [_write(0.0, f"{proj}/a.md", "a")]
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=replay_dir, critic=_RecordingCritic(),
    )
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=replay_dir, critic=_RecordingCritic(),
        allow_existing=True,
    )
    assert result.events_consumed == 1


def test_empty_replay_dir_is_fine(tmp_path: Path):
    # Directory exists but is empty — not a prior run. Must be allowed.
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
