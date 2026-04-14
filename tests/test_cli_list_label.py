"""Tests for `hunch list` and `hunch label` subcommands, plus the
`read_labeled_hunch_ids` helper they depend on.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from hunch.cli import main as cli_main
from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.journal.feedback import FeedbackWriter, read_labeled_hunch_ids
from hunch.journal.hunches import HunchesWriter


# ---------------------------------------------------------------------------
# read_labeled_hunch_ids
# ---------------------------------------------------------------------------

def test_read_labeled_returns_empty_when_missing(tmp_path):
    assert read_labeled_hunch_ids(tmp_path / "nope.jsonl") == {}


def test_read_labeled_returns_only_explicit(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    w = FeedbackWriter(feedback_path=fb)
    w.write_explicit("h-0001", "good", "2026-04-14T12:00:00Z")
    w.write_implicit("h-0002", "some reply text", "2026-04-14T12:01:00Z")
    labels = read_labeled_hunch_ids(fb)
    assert labels == {"h-0001": "good"}


def test_read_labeled_latest_wins(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    w = FeedbackWriter(feedback_path=fb)
    w.write_explicit("h-0001", "skip", "2026-04-14T12:00:00Z")
    w.write_explicit("h-0001", "good", "2026-04-14T12:05:00Z")
    labels = read_labeled_hunch_ids(fb)
    assert labels == {"h-0001": "good"}


def test_read_labeled_skips_malformed_lines(tmp_path):
    fb = tmp_path / "feedback.jsonl"
    fb.write_text('not json\n{"channel": "explicit", "hunch_id": "h-1", "label": "good"}\n')
    labels = read_labeled_hunch_ids(fb)
    assert labels == {"h-1": "good"}


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------

def _emit(writer: HunchesWriter, smell: str, description: str = "") -> str:
    hid = writer.allocate_id()
    writer.write_emit(
        hunch=Hunch(
            smell=smell,
            description=description,
            triggering_refs=TriggeringRefs(),
        ),
        hunch_id=hid,
        ts="2026-04-14T12:00:00Z",
        emitted_by_tick=1,
    )
    return hid


def _run_cli(argv, capsys):
    rc = cli_main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# `hunch list`
# ---------------------------------------------------------------------------

def test_list_when_no_replay_dir_says_so(tmp_path, capsys):
    rc, out, err = _run_cli(
        ["list", "--replay-dir", str(tmp_path / "replay")], capsys
    )
    assert rc == 0
    assert "no hunches yet" in out


def test_list_when_replay_exists_but_empty(tmp_path, capsys):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("")
    rc, out, err = _run_cli(["list", "--replay-dir", str(replay)], capsys)
    assert rc == 0
    assert "no hunches emitted" in out


def test_list_shows_pending_hunches(tmp_path, capsys):
    replay = tmp_path / "replay"
    replay.mkdir()
    w = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(w, "smell A", "description A")
    _emit(w, "smell B", "description B")

    rc, out, err = _run_cli(["list", "--replay-dir", str(replay)], capsys)
    assert rc == 0
    assert "h-0001" in out
    assert "smell A" in out
    assert "description A" in out
    assert "h-0002" in out
    assert "smell B" in out


def test_list_hides_labeled_by_default(tmp_path, capsys):
    replay = tmp_path / "replay"
    replay.mkdir()
    w = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(w, "smell A")
    _emit(w, "smell B")

    fb = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fb.write_explicit("h-0001", "good", "2026-04-14T12:05:00Z")

    rc, out, err = _run_cli(["list", "--replay-dir", str(replay)], capsys)
    assert rc == 0
    assert "h-0001" not in out
    assert "h-0002" in out


def test_list_all_shows_labeled(tmp_path, capsys):
    replay = tmp_path / "replay"
    replay.mkdir()
    w = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(w, "smell A")

    fb = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fb.write_explicit("h-0001", "bad", "2026-04-14T12:05:00Z")

    rc, out, err = _run_cli(
        ["list", "--replay-dir", str(replay), "--all"], capsys
    )
    assert rc == 0
    assert "h-0001" in out
    assert "[bad]" in out


# ---------------------------------------------------------------------------
# `hunch label`
# ---------------------------------------------------------------------------

def test_label_writes_feedback_line(tmp_path, capsys):
    replay = tmp_path / "replay"
    replay.mkdir()
    w = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(w, "smell A")

    rc, out, err = _run_cli(
        ["label", "h-0001", "good", "--replay-dir", str(replay)], capsys
    )
    assert rc == 0
    assert "labeled h-0001 as good" in out

    fb_lines = (replay / "feedback.jsonl").read_text().splitlines()
    assert len(fb_lines) == 1
    d = json.loads(fb_lines[0])
    assert d["channel"] == "explicit"
    assert d["hunch_id"] == "h-0001"
    assert d["label"] == "good"


def test_label_rejects_unknown_hunch_id(tmp_path, capsys):
    replay = tmp_path / "replay"
    replay.mkdir()
    w = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(w, "smell A")

    rc, out, err = _run_cli(
        ["label", "h-9999", "good", "--replay-dir", str(replay)], capsys
    )
    assert rc == 1
    assert "unknown hunch id" in err
    assert not (replay / "feedback.jsonl").exists()


def test_label_with_no_hunches_file_fails(tmp_path, capsys):
    rc, out, err = _run_cli(
        ["label", "h-0001", "good", "--replay-dir", str(tmp_path / "empty")],
        capsys,
    )
    assert rc == 1
    assert "does not exist" in err


def test_label_invalid_label_rejected_by_argparse(tmp_path, capsys):
    with pytest.raises(SystemExit):
        _run_cli(
            ["label", "h-0001", "meh", "--replay-dir", str(tmp_path)],
            capsys,
        )
