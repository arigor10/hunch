"""Tests for `hunch/hook/stop.py` — the deliver-or-rest Stop hook."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.hook.stop import handle_stop, _last_tick_seq
from hunch.journal.append import append_json_line
from hunch.journal.feedback import FeedbackWriter
from hunch.journal.hunches import HunchesWriter, read_current_hunches


def _write_conversation(path: Path, entries: list[dict]) -> None:
    for entry in entries:
        append_json_line(path, entry)


def _emit(writer: HunchesWriter, smell: str, description: str = "") -> str:
    hid = writer.allocate_id()
    writer.write_emit(
        hunch=Hunch(smell=smell, description=description, triggering_refs=TriggeringRefs()),
        hunch_id=hid,
        ts="2026-04-14T12:00:00Z",
        emitted_by_tick=1,
        bookmark_prev=0,
        bookmark_now=1,
    )
    return hid


# ---------------------------------------------------------------------------
# Rest branch: nothing to deliver → append claude_stopped
# ---------------------------------------------------------------------------

def test_handle_stop_appends_claude_stopped_when_nothing_to_deliver(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    conv = replay_dir / "conversation.jsonl"
    _write_conversation(conv, [
        {"tick_seq": 1, "type": "assistant_text", "timestamp": "2026-04-14T12:00:00Z"},
        {"tick_seq": 2, "type": "assistant_text", "timestamp": "2026-04-14T12:01:00Z"},
    ])

    result = handle_stop(replay_dir, now_iso="2026-04-14T12:05:00Z")
    assert result.stdout == ""

    lines = [json.loads(l) for l in conv.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    last = lines[-1]
    assert last["type"] == "claude_stopped"
    assert last["tick_seq"] == 3
    assert last["timestamp"] == "2026-04-14T12:05:00Z"


def test_handle_stop_does_not_deliver_unapproved_hunch(tmp_path):
    # A pending hunch with no "good" label is not deliverable → Claude rests.
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    conv = replay_dir / "conversation.jsonl"
    _write_conversation(conv, [
        {"tick_seq": 1, "type": "assistant_text", "timestamp": "2026-04-14T12:00:00Z"},
    ])
    _emit(HunchesWriter(hunches_path=replay_dir / "hunches.jsonl"), "unlabelled smell")

    result = handle_stop(replay_dir, now_iso="2026-04-14T12:05:00Z")
    assert result.stdout == ""
    lines = [json.loads(l) for l in conv.read_text().splitlines() if l.strip()]
    assert lines[-1]["type"] == "claude_stopped"


# ---------------------------------------------------------------------------
# Deliver branch: an approved hunch is waiting
# ---------------------------------------------------------------------------

def test_handle_stop_delivers_approved_hunch_and_keeps_turn_going(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    conv = replay_dir / "conversation.jsonl"
    _write_conversation(conv, [
        {"tick_seq": 1, "type": "assistant_text", "timestamp": "2026-04-14T12:00:00Z"},
    ])
    hw = HunchesWriter(hunches_path=replay_dir / "hunches.jsonl")
    hid = _emit(hw, "ordering inconsistency", "rows out of order")
    FeedbackWriter(feedback_path=replay_dir / "feedback.jsonl").write_explicit(
        hid, "good", "2026-04-14T12:02:00Z"
    )

    result = handle_stop(replay_dir, now_iso="2026-04-14T12:05:00Z")

    # additionalContext payload carrying the hunch (with its id marker).
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert hid in ctx
    assert "ordering inconsistency" in ctx

    # Hunch marked surfaced by hook:stop.
    recs = {r.hunch_id: r for r in read_current_hunches(replay_dir / "hunches.jsonl")}
    assert recs[hid].status == "surfaced"
    assert recs[hid].history[-1]["by"] == "hook:stop"

    # NO claude_stopped appended — Claude is continuing, not resting.
    lines = [json.loads(l) for l in conv.read_text().splitlines() if l.strip()]
    assert all(e["type"] != "claude_stopped" for e in lines)


# ---------------------------------------------------------------------------
# Robustness — never crash Claude Code
# ---------------------------------------------------------------------------

def test_handle_stop_no_conversation_file(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    assert handle_stop(replay_dir).stdout == ""


def test_handle_stop_empty_conversation_file(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "conversation.jsonl").write_text("")
    assert handle_stop(replay_dir).stdout == ""


def test_handle_stop_never_crashes(tmp_path):
    replay_dir = tmp_path / "nonexistent"
    assert handle_stop(replay_dir).stdout == ""


def test_last_tick_seq_reads_last_line(tmp_path):
    path = tmp_path / "conversation.jsonl"
    _write_conversation(path, [
        {"tick_seq": 1, "type": "a", "timestamp": "t"},
        {"tick_seq": 5, "type": "b", "timestamp": "t"},
        {"tick_seq": 10, "type": "c", "timestamp": "t"},
    ])
    assert _last_tick_seq(path) == 10
