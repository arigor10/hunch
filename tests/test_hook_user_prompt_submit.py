"""Tests for the UserPromptSubmit hook handler."""

from __future__ import annotations

import json
from pathlib import Path

from hunch.critic.protocol import Hunch, TriggeringRefs
from hunch.hook.user_prompt_submit import handle_user_prompt_submit
from hunch.journal.feedback import FeedbackWriter
from hunch.journal.hunches import HunchesWriter, read_current_hunches


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
        bookmark_prev=0,
        bookmark_now=1,
    )
    return hid


def _label_good(replay: Path, hunch_id: str) -> None:
    fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fw.write_explicit(hunch_id=hunch_id, label="good", ts="2026-04-14T12:01:00Z")


def test_empty_replay_dir_returns_empty_continue(tmp_path):
    result = handle_user_prompt_submit(b"{}", tmp_path / "replay")
    assert result.stdout == ""
    assert result.exit_code == 0


def test_no_pending_hunches_returns_empty_continue(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("")  # file exists, empty
    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""


def test_pending_but_unlabeled_hunch_not_injected(tmp_path):
    """A pending hunch without a 'good' label is NOT injected."""
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    _emit(writer, "calibration drift")

    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""


def test_good_labeled_hunch_is_injected_and_marked_surfaced(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"

    writer = HunchesWriter(hunches_path=hunches_path)
    hid = _emit(writer, "calibration drift", "3x discrepancy between runs.")
    _label_good(replay, hid)

    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")

    assert result.stdout != ""
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert hid in ctx
    assert "calibration drift" in ctx
    assert "<hunch-injection>" in ctx

    records = read_current_hunches(hunches_path)
    assert len(records) == 1
    assert records[0].status == "surfaced"
    assert records[0].history[0]["by"] == "hook:user_prompt_submit"


def test_bad_labeled_hunch_not_injected(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    hid = _emit(writer, "false alarm")
    fw = FeedbackWriter(feedback_path=replay / "feedback.jsonl")
    fw.write_explicit(hunch_id=hid, label="bad", ts="2026-04-14T12:01:00Z")

    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""


def test_surfaced_hunches_do_not_re_surface(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"
    writer = HunchesWriter(hunches_path=hunches_path)
    hid = _emit(writer, "smell A")
    _label_good(replay, hid)

    handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")
    result2 = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:06:00Z")

    assert result2.stdout == ""


def test_new_good_hunch_after_surfacing_still_fires(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    hunches_path = replay / "hunches.jsonl"
    writer = HunchesWriter(hunches_path=hunches_path)

    hid_a = _emit(writer, "smell A")
    _label_good(replay, hid_a)
    handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:05:00Z")

    hid_b = _emit(writer, "smell B")
    _label_good(replay, hid_b)
    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:06:00Z")

    assert result.stdout != ""
    payload = json.loads(result.stdout)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "smell B" in ctx
    assert "smell A" not in ctx


def test_malformed_hunches_file_does_not_crash(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("not valid json\n{broken\n")
    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""
    assert result.exit_code == 0


def test_exception_in_handler_returns_empty_continue(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(
        "hunch.hook.user_prompt_submit.read_current_hunches", _boom
    )

    replay = tmp_path / "replay"
    replay.mkdir()
    (replay / "hunches.jsonl").write_text("")

    result = handle_user_prompt_submit(b"{}", replay)
    assert result.stdout == ""
    assert result.exit_code == 0


def test_injection_framing_is_information_not_command(tmp_path):
    replay = tmp_path / "replay"
    replay.mkdir()
    writer = HunchesWriter(hunches_path=replay / "hunches.jsonl")
    hid = _emit(writer, "some smell")
    _label_good(replay, hid)

    result = handle_user_prompt_submit(b"{}", replay, now_iso="2026-04-14T12:00:00Z")
    ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]

    assert "not instructions for you" in ctx
    assert "Scientist" in ctx
