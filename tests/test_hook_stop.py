"""Tests for `hunch/hook/stop.py` — the Stop hook handler."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.hook.stop import handle_stop, _last_tick_seq
from hunch.journal.append import append_json_line


def _write_conversation(path: Path, entries: list[dict]) -> None:
    for entry in entries:
        append_json_line(path, entry)


def test_handle_stop_appends_claude_stopped(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    conv = replay_dir / "conversation.jsonl"
    _write_conversation(conv, [
        {"tick_seq": 1, "type": "assistant_text", "timestamp": "2026-04-14T12:00:00Z"},
        {"tick_seq": 2, "type": "assistant_text", "timestamp": "2026-04-14T12:01:00Z"},
    ])

    rc = handle_stop(replay_dir, now_iso="2026-04-14T12:05:00Z")
    assert rc == 0

    lines = [json.loads(l) for l in conv.read_text().splitlines() if l.strip()]
    assert len(lines) == 3
    last = lines[-1]
    assert last["type"] == "claude_stopped"
    assert last["tick_seq"] == 3
    assert last["timestamp"] == "2026-04-14T12:05:00Z"


def test_handle_stop_no_conversation_file(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    assert handle_stop(replay_dir) == 0


def test_handle_stop_empty_conversation_file(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "conversation.jsonl").write_text("")
    assert handle_stop(replay_dir) == 0


def test_last_tick_seq_reads_last_line(tmp_path):
    path = tmp_path / "conversation.jsonl"
    _write_conversation(path, [
        {"tick_seq": 1, "type": "a", "timestamp": "t"},
        {"tick_seq": 5, "type": "b", "timestamp": "t"},
        {"tick_seq": 10, "type": "c", "timestamp": "t"},
    ])
    assert _last_tick_seq(path) == 10


def test_handle_stop_never_crashes(tmp_path):
    replay_dir = tmp_path / "nonexistent"
    assert handle_stop(replay_dir) == 0
