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


def test_first_tick_fires_on_silence(tmp_path: Path):
    # Assistant speaks, then silence elapses → first tick fires.
    project_root = "/tmp/proj"
    events = [
        _asst(100.0, "writing now"),
        _write(135.0, f"{project_root}/results.md", "# Results\n"),
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


def test_debounce_blocks_rapid_fires(tmp_path: Path):
    # Two silence-eligible events close together, min_debounce=300s → only the first fires.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=300.0, max_interval_s=1e9)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _write(135.0, f"{proj}/a.md", "a"),    # silence fires (first tick)
        _asst(136.0, "more thinking"),
        _write(200.0, f"{proj}/b.md", "b"),   # debounce blocks (200-≈130=70 < 300)
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


def test_virtual_ticks_fire_during_long_monologue(tmp_path: Path):
    # During a long assistant monologue, virtual ticks fire in the gaps
    # between events when a live loop would have fired on silence.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=120.0)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "start"),
        _asst(145.0, "thinking"),
        _asst(165.0, "still thinking"),
        _asst(195.0, "deeper"),
        _asst(225.0, "keep going"),
        _asst(265.0, "almost there"),
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
        trigger_config=cfg,
    )
    # Virtual ticks fire at silence boundaries during the monologue.
    assert result.ticks_fired >= 2
    assert result.virtual_ticks_fired >= 2


def test_hunches_persist_to_hunches_jsonl(tmp_path: Path):
    # Fire on silence, emit one hunch, verify hunches.jsonl.
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "thinking"),
        _write(135.0, f"{proj}/r.md", "# R"),
    ]
    critic = _RecordingCritic(emit_at={1})
    result = run_replay(
        events=events,
        project_roots=[proj],
        replay_dir=tmp_path / "replay",
        critic=critic,
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
    # bookmark_prev/bookmark_now identify the Critic's window for
    # this tick — offline evaluators (novelty, duplicate) rely on
    # these to pull the same slice the Critic "saw".
    assert records[0]["bookmark_prev"] == critic.tick_log[-1]["prev"]
    assert records[0]["bookmark_now"] == critic.tick_log[-1]["now"]


def test_persisted_hunch_bookmarks_match_tick_window(tmp_path: Path):
    # Emit hunches at several ticks across an event stream and verify
    # the bookmark fields in hunches.jsonl align with the tick log.
    proj = "/tmp/proj"
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=0.0, max_interval_s=1e9)
    events = [
        _asst(100.0, "thinking"),
        _write(135.0, f"{proj}/a.md", "a"),     # silence → tick
        _asst(136.0, "more"),
        _write(170.0, f"{proj}/b.md", "b"),    # silence → tick
        _asst(171.0, "final"),
        _write(205.0, f"{proj}/c.md", "c"),    # silence → tick
    ]
    critic = _RecordingCritic(emit_at={1, 2, 3})
    run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic, trigger_config=cfg,
    )
    records = [
        json.loads(line)
        for line in (tmp_path / "replay" / "hunches.jsonl").read_text().splitlines()
        if line.strip() and json.loads(line).get("type") != "meta"
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
    # After initial virtual tick, assistant speaks and a long event-less
    # gap follows. silence_s=30 elapses in the gap — a live loop polling
    # every second would have fired on silence. Virtual tick should fire.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=60.0, max_interval_s=10_000.0)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "start"),
        _asst(145.0, "thinking"),              # sets last_assistant_ts=145
        # Long gap to t=600 — virtual tick fires in gap.
        {"type": "tool_error", "timestamp": _ts(600.0), "message": "boom"},
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=cfg,
    )
    assert result.virtual_ticks_fired >= 1


def test_virtual_max_interval_tick_fires_in_gap(tmp_path: Path):
    # After a silence-based virtual tick, max_interval forces another
    # virtual tick when silence_s is large enough that silence fires later.
    cfg = TriggerV1Config(silence_s=200.0, min_debounce_s=60.0, max_interval_s=120.0)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "start"),
        # Virtual silence tick fires at 100+200=300 (first tick, no debounce).
        _asst(301.0, "more work"),
        {"type": "tool_error", "timestamp": _ts(302.0), "message": "bump"},
        # Gap to 1000: silence_ready=301+200=501, fire_at=max(501,300+60)=501.
        # max_interval=300+120=420. 420 < 501 → max_interval fires first.
        {"type": "tool_error", "timestamp": _ts(1000.0), "message": "still boom"},
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=cfg,
    )
    vticks = [t for t in result.tick_log if t["virtual"]]
    assert len(vticks) >= 2
    max_int_vtick = [v for v in vticks if v["sim_now"] == pytest.approx(420.0)]
    assert len(max_int_vtick) == 1


def test_virtual_tick_respects_no_new_content(tmp_path: Path):
    # After a virtual silence tick consumes all bookmarks, virtual ticks
    # are suppressed in the following gap because bookmark hasn't grown.
    cfg = TriggerV1Config(silence_s=30.0, min_debounce_s=0.0, max_interval_s=200.0)
    proj = "/tmp/proj"
    events = [
        _asst(100.0, "start"),
        # Virtual silence tick fires at 130 (first tick), consuming bm=1.
        # No more events until 2000 — bm still 1 = last_tick_bm → suppressed.
        _write(2000.0, f"{proj}/b.md", "b"),
    ]
    critic = _RecordingCritic()
    result = run_replay(
        events=events, project_roots=[proj],
        replay_dir=tmp_path / "replay", critic=critic,
        trigger_config=cfg,
    )
    # One virtual tick at 130 (initial silence). No more virtual ticks
    # because bookmark_pre_event=1 = last_tick_bm after the first tick.
    vticks = [t for t in result.tick_log if t["virtual"]]
    assert len(vticks) == 1
    assert vticks[0]["sim_now"] == pytest.approx(130.0)


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
